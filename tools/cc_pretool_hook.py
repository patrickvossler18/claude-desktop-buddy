#!/usr/bin/env python3
"""Claude Code PreToolUse hook -> cc_buddy daemon for device approval.

Sends the tool call to the daemon, blocks waiting for the daemon's reply
(allow / deny / timeout), and emits Claude Code's permissionDecision JSON.

On timeout, daemon-down, or any error: exits 0 with no output. That's
intentional — Claude Code falls back to its normal terminal prompt, so
the buddy is additive, never a hard dependency.
"""
import json
import socket
import sys
import uuid
from datetime import datetime

from cc_buddy_common import hint_for

SOCK = "/tmp/cc_buddy.sock"
LOG = "/tmp/cc_buddy.log"
SOCKET_TIMEOUT_S = 45  # daemon waits 30s for the device; we give margin


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} [pre] {msg}\n")
    except OSError:
        pass


def main():
    log("---- pretool hook fired ----")
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        log(f"stdin parse failed: {e}")
        return

    mode = event.get("permission_mode", "default")
    if mode in ("bypassPermissions", "acceptEdits"):
        log(f"skip: permission_mode={mode}")
        return

    tool = event.get("tool_name", "tool")
    tool_input = event.get("tool_input", {})
    rid = "req_" + uuid.uuid4().hex[:12]
    hint = hint_for(tool, tool_input)
    msg = {
        "op": "approve",
        "session_id": event.get("session_id", ""),
        "request_id": rid,
        "tool": tool,
        "hint": hint,
    }
    log(f"req {rid} tool={tool} hint={hint[:60]!r}")

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(SOCKET_TIMEOUT_S)
        s.connect(SOCK)
        s.sendall((json.dumps(msg) + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
        s.close()
    except (OSError, socket.timeout) as e:
        log(f"daemon error: {e}")
        return

    line = data.partition(b"\n")[0]
    if not line:
        log("daemon closed without reply")
        return
    try:
        resp = json.loads(line)
    except json.JSONDecodeError as e:
        log(f"bad reply: {e}")
        return

    decision = resp.get("decision", "timeout")
    log(f"resp {rid} decision={decision}")

    if decision == "allow":
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }))
    elif decision == "deny":
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "denied via Buddy stick",
            }
        }))
    # timeout / unknown -> exit 0 with no output, terminal fallback


if __name__ == "__main__":
    main()
