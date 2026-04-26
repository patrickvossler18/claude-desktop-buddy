"""Shared helpers for the cc-buddy hooks.

Lives next to cc_stop_hook.py and cc_pretool_hook.py so both can do
`from cc_buddy_common import hint_for` (Python adds the script's
directory to sys.path automatically).
"""
import os


def hint_for(tool, tool_input):
    """Return a short human-readable hint for a tool call.

    The firmware truncates to 43 chars on display, so brevity matters
    more than completeness. Caller doesn't need to truncate.
    """
    if not isinstance(tool_input, dict):
        return ""
    if tool == "Bash":
        return _bash_hint(tool_input.get("command", ""))
    if tool == "BashOutput":
        return str(tool_input.get("bash_id", ""))
    if tool == "KillShell":
        return str(tool_input.get("shell_id", ""))
    if tool in ("Edit", "Write", "Read", "NotebookEdit"):
        return os.path.basename(tool_input.get("file_path", ""))
    if tool == "WebFetch":
        return tool_input.get("url", "")
    if tool == "WebSearch":
        return tool_input.get("query", "")
    if tool in ("Glob", "Grep"):
        return tool_input.get("pattern", "")
    if tool == "ToolSearch":
        return tool_input.get("query", "")
    if tool == "Skill":
        return tool_input.get("skill", "")
    if tool == "TaskCreate":
        return tool_input.get("subject", "")
    if tool in ("TaskUpdate", "TaskGet", "TaskOutput", "TaskStop"):
        return str(tool_input.get("taskId", ""))
    if tool == "Agent":
        return tool_input.get("description", "")
    # MCP tools or anything else: try a few likely keys before giving up.
    for key in ("subject", "name", "url", "path", "query", "description"):
        v = tool_input.get(key)
        if v:
            return str(v)
    return ""


def _bash_hint(cmd):
    """For Bash: collapse multi-line commands and trim to the first
    pipeline/chain segment so the display shows the *operative* command."""
    if not cmd:
        return ""
    cmd = " ".join(cmd.split())  # collapse newlines and runs of whitespace
    for sep in (" && ", " || ", " | ", "; "):
        if sep in cmd:
            cmd = cmd.split(sep, 1)[0]
            break
    return cmd
