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
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


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


def parse_discord_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_time_arg(value: str, tz_name: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc)


def day_window(day: str, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.fromisoformat(day).replace(tzinfo=tz)
    return start.astimezone(timezone.utc), (
        start + timedelta(days=1)).astimezone(timezone.utc)


def channel_ids_from_args(args: argparse.Namespace) -> list[str]:
    raw: list[str] = []
    if args.all_channels:
        raw.extend(os.environ.get("DISCORD_ALL_CHANNEL_IDS", "").split(","))
    if args.channel_id:
        raw.extend(args.channel_id)
    if not args.all_channels:
        env_channels = os.environ.get("DISCORD_CHANNEL_IDS") or os.environ.get(
            "DISCORD_CHANNEL_ID", "")
        if env_channels:
            raw.extend(env_channels.split(","))
    ids = [c.strip() for c in raw if c.strip()]
    return list(dict.fromkeys(ids))


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
    parser.add_argument("--channel-id", action="append",
                        help="Discord channel ID. May be repeated. Env fallback: DISCORD_CHANNEL_IDS or DISCORD_CHANNEL_ID.")
    parser.add_argument("--all-channels", action="store_true",
                        help="Use all channel IDs from DISCORD_ALL_CHANNEL_IDS.")
    parser.add_argument("--pattern", default=DEFAULT_PATTERN,
                        help="Regex to match against message content and embeds.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximum number of recent messages to inspect.")
    parser.add_argument("--date",
                        help="Local date to match, YYYY-MM-DD. Use with --timezone.")
    parser.add_argument("--today", action="store_true",
                        help="Match today's date in --timezone.")
    parser.add_argument("--timezone", default="Asia/Tokyo",
                        help="Timezone for --date/--today. Default: Asia/Tokyo.")
    parser.add_argument("--since",
                        help="Only match messages at or after this time. Naive ISO values use --timezone.")
    parser.add_argument("--until",
                        help="Only match messages before this time. Naive ISO values use --timezone.")
    parser.add_argument("--include-human", action="store_true",
                        help="Also match human-authored messages. Default is bot/webhook only.")
    parser.add_argument("--delete", action="store_true",
                        help="Actually delete matched messages. Omit for dry-run.")
    parser.add_argument("--sleep", type=float, default=0.35,
                        help="Delay between deletes to be gentle with rate limits.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    channel_ids = channel_ids_from_args(args)
    if not args.token or not channel_ids:
        if args.all_channels:
            raise SystemExit(
                "Set DISCORD_BOT_TOKEN and DISCORD_ALL_CHANNEL_IDS for --all-channels.")
        raise SystemExit(
            "Set DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID(S), or pass --token and --channel-id.")
    if args.token == "...":
        raise SystemExit(
            "DISCORD_BOT_TOKEN is still '...'. Set it to an actual Discord bot token.")
    for channel_id in channel_ids:
        if channel_id.startswith(("http://", "https://")):
            raise SystemExit(
                "DISCORD_CHANNEL_ID must be the numeric channel ID, not a webhook URL. "
                "Enable Discord Developer Mode, right-click a channel, and Copy Channel ID.")
        if not re.fullmatch(r"\d{17,20}", channel_id):
            raise SystemExit(
                "DISCORD_CHANNEL_ID should look like a 17-20 digit Discord channel ID.")

    since = parse_time_arg(args.since, args.timezone) if args.since else None
    until = parse_time_arg(args.until, args.timezone) if args.until else None
    if args.today:
        args.date = datetime.now(ZoneInfo(args.timezone)).date().isoformat()
    if args.date:
        since, until = day_window(args.date, args.timezone)

    regex = re.compile(args.pattern, re.IGNORECASE)
    matched: list[tuple[str, dict[str, Any]]] = []
    inspected = 0
    for channel_id in channel_ids:
        for message in iter_messages(channel_id, args.token, args.limit):
            inspected += 1
            created_dt = parse_discord_time(message.get("timestamp", ""))
            if since and created_dt < since:
                break
            if until and created_dt >= until:
                continue
            if not args.include_human and not is_bot_or_webhook_message(message):
                continue
            text = message_text(message)
            if regex.search(text):
                matched.append((channel_id, message))
                author = (message.get("author") or {}).get("username", "unknown")
                created = message.get("timestamp", "")
                preview = " ".join(text.split())[:160]
                print(f"[match] channel={channel_id} {message['id']} "
                      f"{created} {author}: {preview}")

    action = "delete" if args.delete else "dry-run"
    window = ""
    if since or until:
        window = (f" since={since.isoformat() if since else '-'}"
                  f" until={until.isoformat() if until else '-'}")
    print(f"[summary] action={action} inspected={inspected} "
          f"matched={len(matched)}{window}")

    if not args.delete:
        print("[summary] no messages deleted; rerun with --delete to delete matches")
        return

    for channel_id, message in matched:
        delete_message(channel_id, message["id"], args.token)
        print(f"[deleted] channel={channel_id} {message['id']}")
        time.sleep(args.sleep)


if __name__ == "__main__":
    main()
