#!/bin/sh
# install.sh — set up cc-buddy on this Mac.
#
# What it does:
#   1. Creates a Python venv at ${CC_BUDDY_HOME:-$HOME/.local/share/cc-buddy}/venv
#   2. Installs pyserial + bleak into it
#   3. Generates wrapper scripts that invoke the venv python with our hooks
#   4. Adds Stop and PreToolUse hook entries to ~/.claude/settings.json
#
# Override the install location with CC_BUDDY_HOME, or pick a specific
# Python with CC_BUDDY_PYTHON.
#
# Idempotent: safe to re-run after a repo update.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${CC_BUDDY_HOME:-$HOME/.local/share/cc-buddy}"
VENV="$HOME_DIR/venv"
BIN_DIR="$HOME_DIR/bin"
SETTINGS="$HOME/.claude/settings.json"

PYTHON="${CC_BUDDY_PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: $PYTHON not on PATH; install Python 3.10+ or set CC_BUDDY_PYTHON" >&2
  exit 1
fi

echo "==> repo:  $REPO_ROOT"
echo "==> home:  $HOME_DIR"

mkdir -p "$BIN_DIR"

if [ ! -d "$VENV" ]; then
  echo "==> creating venv with $PYTHON ($($PYTHON --version))"
  "$PYTHON" -m venv "$VENV"
fi
echo "==> installing deps (pyserial, bleak)"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet pyserial bleak

write_wrapper() {
  name="$1"
  script="$2"
  path="$BIN_DIR/$name"
  cat > "$path" <<WRAPEOF
#!/bin/sh
exec "$VENV/bin/python" "$REPO_ROOT/tools/$script" "\$@"
WRAPEOF
  chmod +x "$path"
}

echo "==> generating wrappers in $BIN_DIR"
write_wrapper cc_buddy_daemon cc_buddy_daemon.py
write_wrapper cc_stop_hook    cc_stop_hook.py
write_wrapper cc_pretool_hook cc_pretool_hook.py

echo "==> updating $SETTINGS"
"$VENV/bin/python" - "$SETTINGS" "$BIN_DIR/cc_stop_hook" "$BIN_DIR/cc_pretool_hook" <<'PYEOF'
import json, os, sys

settings_path, stop_path, pretool_path = sys.argv[1:4]
data = {}
if os.path.exists(settings_path):
    try:
        data = json.loads(open(settings_path).read() or "{}")
    except json.JSONDecodeError:
        data = {}
hooks = data.setdefault("hooks", {})

def upsert(event, command, marker):
    arr = hooks.setdefault(event, [])
    arr[:] = [
        entry for entry in arr
        if not any(marker in h.get("command", "") for h in entry.get("hooks", []))
    ]
    arr.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": command}],
    })

upsert("Stop",       stop_path,    "cc_stop_hook")
upsert("PreToolUse", pretool_path, "cc_pretool_hook")

os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w") as f:
    json.dump(data, f, indent=2)
print(f"  Stop       -> {stop_path}")
print(f"  PreToolUse -> {pretool_path}")
PYEOF

cat <<EOF

==> install complete

next steps:

  1. Start the daemon (leave running in a terminal):

       $BIN_DIR/cc_buddy_daemon

     Or use BLE instead of USB:

       BUDDY_TRANSPORT=ble $BIN_DIR/cc_buddy_daemon

  2. Tail the unified log (optional):

       tail -f /tmp/cc_buddy.log

  3. Start a new \`claude\` session. The next assistant turn ticks the stick;
     the next tool call shows a permission prompt on it.

uninstall:
  - rm -rf $HOME_DIR
  - remove the Stop and PreToolUse entries from $SETTINGS

EOF
