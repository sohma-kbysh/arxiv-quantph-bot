#!/usr/bin/env python3
"""Find or delete bot/webhook Discord messages containing arXiv URLs.

Dry-run by default. Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID, then run:

    python3 scripts/clean_discord_urls.py
    python3 scripts/clean_discord_urls.py --delete

The bot token needs access to the channel. Deleting webhook or other bot
messages generally requires the Manage Messages permission.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_BASE = "https://discord.com/api/v10"
DEFAULT_PATTERN = r"https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/\S+"


def discord_request(method: str, path: str, token: str,
                    payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "arxiv-quantph-bot-cleanup/1.0",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data, headers=headers, method=method)
    while True:
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
                if not body:
                    return None
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if exc.code == 429:
                try:
                    retry_after = json.loads(body).get("retry_after", 1.0)
                except json.JSONDecodeError:
                    retry_after = 1.0
                time.sleep(float(retry_after) + 0.1)
                continue
            print(f"[error] Discord HTTP {exc.code}: {body[:300]!r}",
                  file=sys.stderr)
            raise


def message_text(message: dict[str, Any]) -> str:
    parts = [message.get("content", "")]
    for embed in message.get("embeds", []):
        for key in ("url", "title", "description"):
            value = embed.get(key)
            if value:
                parts.append(str(value))
        for field in embed.get("fields", []):
            parts.append(str(field.get("name", "")))
            parts.append(str(field.get("value", "")))
    return "\n".join(parts)


def is_bot_or_webhook_message(message: dict[str, Any]) -> bool:
    author = message.get("author") or {}
    return bool(author.get("bot") or message.get("webhook_id"))


def iter_messages(channel_id: str, token: str, limit: int | None = None):
    before = None
    fetched = 0
    while True:
        batch_limit = 100
        if limit is not None:
            remaining = limit - fetched
            if remaining <= 0:
                return
            batch_limit = min(batch_limit, remaining)

        query = {"limit": str(batch_limit)}
        if before:
            query["before"] = before
        path = f"/channels/{channel_id}/messages?{urllib.parse.urlencode(query)}"
        messages = discord_request("GET", path, token)
        if not messages:
            return
        for message in messages:
            fetched += 1
            yield message
        before = messages[-1]["id"]


def delete_message(channel_id: str, message_id: str, token: str) -> None:
    discord_request("DELETE", f"/channels/{channel_id}/messages/{message_id}",
                    token)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find/delete bot or webhook Discord messages containing URLs.")
    parser.add_argument("--token", default=os.environ.get("DISCORD_BOT_TOKEN"),
                        help="Discord bot token, or DISCORD_BOT_TOKEN env var.")
    parser.add_argument("--channel-id",
                        default=os.environ.get("DISCORD_CHANNEL_ID"),
                        help="Discord channel ID, or DISCORD_CHANNEL_ID env var.")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN,
                        help="Regex to match against message content and embeds.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of recent messages to inspect.")
    parser.add_argument("--include-human", action="store_true",
                        help="Also match human-authored messages. Default is bot/webhook only.")
    parser.add_argument("--delete", action="store_true",
                        help="Actually delete matched messages. Omit for dry-run.")
    parser.add_argument("--sleep", type=float, default=0.35,
                        help="Delay between deletes to be gentle with rate limits.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token or not args.channel_id:
        raise SystemExit(
            "Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID, or pass --token and --channel-id.")
    if args.token == "...":
        raise SystemExit(
            "DISCORD_BOT_TOKEN is still '...'. Set it to an actual Discord bot token.")
    if args.channel_id.startswith(("http://", "https://")):
        raise SystemExit(
            "DISCORD_CHANNEL_ID must be the numeric channel ID, not a webhook URL. "
            "Enable Discord Developer Mode, right-click #general, and Copy Channel ID.")
    if not re.fullmatch(r"\d{17,20}", args.channel_id):
        raise SystemExit(
            "DISCORD_CHANNEL_ID should look like a 17-20 digit Discord channel ID.")

    regex = re.compile(args.pattern, re.IGNORECASE)
    matched: list[dict[str, Any]] = []
    inspected = 0
    for message in iter_messages(args.channel_id, args.token, args.limit):
        inspected += 1
        if not args.include_human and not is_bot_or_webhook_message(message):
            continue
        text = message_text(message)
        if regex.search(text):
            matched.append(message)
            author = (message.get("author") or {}).get("username", "unknown")
            created = message.get("timestamp", "")
            preview = " ".join(text.split())[:160]
            print(f"[match] {message['id']} {created} {author}: {preview}")

    action = "delete" if args.delete else "dry-run"
    print(f"[summary] action={action} inspected={inspected} matched={len(matched)}")

    if not args.delete:
        print("[summary] no messages deleted; rerun with --delete to delete matches")
        return

    for message in matched:
        delete_message(args.channel_id, message["id"], args.token)
        print(f"[deleted] {message['id']}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
