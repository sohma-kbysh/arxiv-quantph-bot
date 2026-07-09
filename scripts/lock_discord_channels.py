#!/usr/bin/env python3
"""Lock Discord text channels for announcement-only use.

Dry-run by default. The script denies message/thread sending for @everyone
while allowing reactions, preserving unrelated existing overwrites.

Examples:

    export DISCORD_BOT_TOKEN="actual Discord Bot Token"
    export DISCORD_GUILD_ID="123456789012345678"
    python3 scripts/lock_discord_channels.py --names fault-tolerant-computation quantum-error-correction-code
    python3 scripts/lock_discord_channels.py --names fault-tolerant-computation quantum-error-correction-code --apply

Or with explicit IDs:

    export DISCORD_LOCK_CHANNEL_IDS="111111111111111111,222222222222222222"
    python3 scripts/lock_discord_channels.py --channel-id 333333333333333333 --apply
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

# Discord permission bits.
ADD_REACTIONS = 1 << 6
SEND_MESSAGES = 1 << 11
SEND_TTS_MESSAGES = 1 << 12
ATTACH_FILES = 1 << 15
CREATE_PUBLIC_THREADS = 1 << 35
CREATE_PRIVATE_THREADS = 1 << 36
SEND_MESSAGES_IN_THREADS = 1 << 38

DENY_MESSAGE_BITS = (
    SEND_MESSAGES
    | SEND_TTS_MESSAGES
    | ATTACH_FILES
    | CREATE_PUBLIC_THREADS
    | CREATE_PRIVATE_THREADS
    | SEND_MESSAGES_IN_THREADS
)

TEXT_CHANNEL_TYPES = {0, 5, 10, 11, 12, 15}


def discord_request(method: str, path: str, token: str,
                    payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "arxiv-quantph-bot-channel-lock/1.0",
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


def parse_ids(values: list[str] | None, env_name: str) -> list[str]:
    raw: list[str] = []
    if values:
        raw.extend(values)
    raw.extend(os.environ.get(env_name, "").split(","))
    ids = [item.strip() for item in raw if item.strip()]
    return list(dict.fromkeys(ids))


def validate_id(value: str, label: str) -> None:
    if value.startswith(("http://", "https://")):
        raise SystemExit(
            f"{label} must be a numeric Discord ID, not a URL.")
    if not re.fullmatch(r"\d{17,20}", value):
        raise SystemExit(
            f"{label} should look like a 17-20 digit Discord ID: {value}")


def get_guild_channels(token: str, guild_id: str) -> list[dict[str, Any]]:
    return discord_request("GET", f"/guilds/{guild_id}/channels", token)


def target_channels(args: argparse.Namespace) -> list[dict[str, Any]]:
    ids = parse_ids(args.channel_id, "DISCORD_LOCK_CHANNEL_IDS")
    exclude_ids = set(parse_ids(args.exclude_channel_id,
                                "DISCORD_LOCK_EXCLUDE_CHANNEL_IDS"))
    for channel_id in ids:
        validate_id(channel_id, "channel ID")
    for channel_id in exclude_ids:
        validate_id(channel_id, "excluded channel ID")

    by_id = {
        channel_id: {
            "id": channel_id,
            "name": channel_id,
            "permission_overwrites": [],
        }
        for channel_id in ids
    }

    requested_names = set(args.names or [])
    excluded_names = set(args.exclude_names or [])
    if requested_names or args.all_text_channels or excluded_names:
        if not args.guild_id:
            raise SystemExit(
                "Set DISCORD_GUILD_ID or pass --guild-id when selecting by name.")
        validate_id(args.guild_id, "guild ID")
        channels = get_guild_channels(args.token, args.guild_id)
        for channel in channels:
            if channel.get("type") not in TEXT_CHANNEL_TYPES:
                continue
            name = channel.get("name", "")
            channel_id = channel["id"]
            if channel_id in exclude_ids or name in excluded_names:
                continue
            if args.all_text_channels or name in requested_names:
                by_id[channel_id] = channel

        missing = sorted(requested_names - {c.get("name") for c in by_id.values()})
        if missing:
            raise SystemExit(f"Channel name(s) not found: {', '.join(missing)}")

    return sorted(by_id.values(), key=lambda c: c.get("position", 0))


def everyone_overwrite(channel: dict[str, Any], guild_id: str) -> dict[str, Any]:
    for overwrite in channel.get("permission_overwrites", []):
        if overwrite.get("id") == guild_id and overwrite.get("type") == 0:
            return overwrite
    return {"id": guild_id, "type": 0, "allow": "0", "deny": "0"}


def merged_permissions(overwrite: dict[str, Any]) -> tuple[int, int]:
    allow = int(overwrite.get("allow") or 0)
    deny = int(overwrite.get("deny") or 0)
    allow |= ADD_REACTIONS
    allow &= ~DENY_MESSAGE_BITS
    deny |= DENY_MESSAGE_BITS
    deny &= ~ADD_REACTIONS
    return allow, deny


def put_everyone_overwrite(channel_id: str, guild_id: str, token: str,
                           allow: int, deny: int) -> None:
    payload = {
        "type": 0,
        "allow": str(allow),
        "deny": str(deny),
    }
    quoted = urllib.parse.quote(guild_id)
    discord_request("PUT", f"/channels/{channel_id}/permissions/{quoted}",
                    token, payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deny message sending and allow reactions for @everyone.")
    parser.add_argument("--token", default=os.environ.get("DISCORD_BOT_TOKEN"),
                        help="Discord bot token, or DISCORD_BOT_TOKEN env var.")
    parser.add_argument("--guild-id", default=os.environ.get("DISCORD_GUILD_ID"),
                        help="Discord guild/server ID, or DISCORD_GUILD_ID env var.")
    parser.add_argument("--channel-id", action="append",
                        help="Target channel ID. May be repeated. Env fallback: DISCORD_LOCK_CHANNEL_IDS.")
    parser.add_argument("--names", nargs="+",
                        help="Target channel names. Requires --guild-id.")
    parser.add_argument("--all-text-channels", action="store_true",
                        help="Target all text/news/thread/forum channels in the guild. Use excludes to keep open channels.")
    parser.add_argument("--exclude-channel-id", action="append",
                        help="Channel ID to skip. Env fallback: DISCORD_LOCK_EXCLUDE_CHANNEL_IDS.")
    parser.add_argument("--exclude-names", nargs="+", default=[],
                        help="Channel names to skip, e.g. random.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually update Discord. Omit for dry-run.")
    parser.add_argument("--sleep", type=float, default=0.35,
                        help="Delay between updates to be gentle with rate limits.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token:
        raise SystemExit("Set DISCORD_BOT_TOKEN or pass --token.")
    if args.token == "...":
        raise SystemExit(
            "DISCORD_BOT_TOKEN is still '...'. Set it to an actual Discord bot token.")
    if not args.guild_id:
        raise SystemExit(
            "Set DISCORD_GUILD_ID or pass --guild-id. This is also the @everyone role ID.")

    validate_id(args.guild_id, "guild ID")
    channels = target_channels(args)
    if not channels:
        raise SystemExit(
            "No channels selected. Pass --names, --channel-id, or --all-text-channels.")

    action = "apply" if args.apply else "dry-run"
    print(f"[summary] action={action} targets={len(channels)}")
    for channel in channels:
        overwrite = everyone_overwrite(channel, args.guild_id)
        old_allow = int(overwrite.get("allow") or 0)
        old_deny = int(overwrite.get("deny") or 0)
        new_allow, new_deny = merged_permissions(overwrite)
        name = channel.get("name") or channel["id"]
        print(
            f"[target] #{name} {channel['id']} "
            f"allow {old_allow}->{new_allow} deny {old_deny}->{new_deny}")
        if args.apply:
            put_everyone_overwrite(
                channel["id"], args.guild_id, args.token, new_allow, new_deny)
            time.sleep(args.sleep)

    if not args.apply:
        print("[summary] no Discord changes made; rerun with --apply to update")


if __name__ == "__main__":
    main()
