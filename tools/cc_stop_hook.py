#!/usr/bin/env python3
"""Claude Code Stop hook -> cc_buddy daemon over Unix socket.

Reads {session_id, transcript_path, ...} on stdin, walks the transcript
JSONL once to sum output_tokens (deduped by message.id) and collect a few
recent entries, then sends a snapshot to the daemon. Fast exit; daemon
owns the serial write.
"""
import json
import os
import socket
import sys
import traceback
from datetime import datetime, timezone

from cc_buddy_common import hint_for

SOCK = "/tmp/cc_buddy.sock"
LOG = "/tmp/cc_buddy.log"


def local_midnight_utc():
    """Return a UTC-aware datetime representing today's local midnight."""
    local_now = datetime.now().astimezone()
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc)


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} [stop] {msg}\n")
    except OSError:
        pass


def parse_transcript(path, today_cutoff):
    entries = []
    output_tokens = 0
    output_tokens_today = 0
    counted = set()
    last_assistant_text = ""
    with open(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            ts = obj.get("timestamp", "")
            if t == "assistant":
                msg = obj.get("message", {})
                mid = msg.get("id")
                if mid and mid not in counted:
                    counted.add(mid)
                    out = msg.get("usage", {}).get("output_tokens", 0)
                    output_tokens += out
                    try:
                        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if ts_dt >= today_cutoff:
                            output_tokens_today += out
                    except (ValueError, AttributeError):
                        pass
                for block in msg.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        last_assistant_text = block.get("text", "")
                    elif block.get("type") == "tool_use":
                        name = block.get("name", "tool")
                        h = hint_for(name, block.get("input", {}))
                        entries.append((ts, f"{name} {h}".rstrip()))
            elif t == "user":
                content = obj.get("message", {}).get("content")
                if isinstance(content, str):
                    entries.append((ts, content))
    return entries, output_tokens, output_tokens_today, last_assistant_text


def fmt_time(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except (ValueError, AttributeError):
        return ""


def main():
    log("---- stop hook fired ----")
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        log(f"stdin parse failed: {e}")
        return
    path = event.get("transcript_path")
    if not path or not os.path.exists(path):
        log("no transcript path")
        return

    try:
        entries, tokens, tokens_today, last_text = parse_transcript(path, local_midnight_utc())
    except Exception:
        log("parse error:\n" + traceback.format_exc())
        return

    recent = [
        f"{fmt_time(ts)} {text}".strip()[:90]
        for ts, text in entries[-5:][::-1]
    ]
    snapshot = {
        "total": 1,
        "running": 0,
        "waiting": 0,
        "msg": (last_text[:23].strip() or "done"),
        "entries": recent,
        "tokens": tokens,
        "tokens_today": tokens_today,
    }
    msg = {
        "op": "snapshot",
        "session_id": event.get("session_id", ""),
        "snapshot": snapshot,
    }
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(SOCK)
        s.sendall((json.dumps(msg) + "\n").encode())
        s.close()
        log(f"sent snapshot tokens={tokens} today={tokens_today} entries={len(recent)} msg={snapshot['msg']!r}")
    except (OSError, socket.timeout) as e:
        log(f"daemon unreachable: {e}")


if __name__ == "__main__":
    main()
