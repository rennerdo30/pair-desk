"""Telling agent sessions about the owner's activity.

Two paths read the same change feed (store.read_changes / Store.owner_events):

1. The UserPromptSubmit hook (`desk.py hook user-prompt-submit`) adds a short block to the next
   prompt: every owner comment, verdict, new report and status change since this session last
   looked. The cursor is kept per session (and per project, for sessions that have none yet) in
   `notify-cursors.json` in the data folder.
2. The MCP server pushes the same events into a running session as Claude Code channel messages
   (`notifications/claude/channel`), which reach even an idle session. Channels are a research
   preview: the session has to be started with
   `claude --dangerously-load-development-channels plugin:agent-pair-programming@agent-pair-programming`.
   The server detects that flag on its ancestor process (or `PAIR_DESK_CHANNEL=on|off`). While it
   pushes, it keeps a heartbeat file under `channels/` keyed by the Claude process id; the hook of
   the same session sees it and stays quiet, so nothing is announced twice.

Which event kinds notify is a per-project setting (`notify` on the project).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

CURSOR_FILE = "notify-cursors.json"
CHANNEL_DIR = "channels"
CHANNEL_ENV = "PAIR_DESK_CHANNEL"          # on | off | auto (default)
CHANNEL_FLAGS = ("--channels", "--dangerously-load-development-channels")
PLUGIN_NAME = "agent-pair-programming"
CHANNEL_FRESH_SECONDS = 30                  # a heartbeat older than this means no live channel
SESSION_TTL = _dt.timedelta(days=14)         # forget session cursors not used for this long
MAX_LINES = 12
MAX_ANCESTORS = 8
# agent-bridge sets these on every delegated job (subagent) it starts; top-level sessions have neither.
BRIDGE_JOB_ENV = ("AGENT_BRIDGE_JOB_ID", "AGENT_BRIDGE_PARENT_JOB")


def delegated_job(env: dict | None = None) -> str | None:
    """The agent-bridge job this process belongs to, or None in a top-level session. Delegated jobs
    work from their brief: they get no desk triage prompt and no owner activity, so only the session
    that delegated them reacts to the owner."""
    env = os.environ if env is None else env
    for name in BRIDGE_JOB_ENV:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def receives_activity(lead: str | None, client: str) -> bool:
    """Whether a top-level session of `client` gets owner activity: every client when the project
    names no lead, else only the lead client."""
    return not lead or lead == client


# ---------------------------------------------------------------------------------------- text

def _ago(iso: str, now: _dt.datetime | None = None) -> str:
    try:
        t = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return ""
    s = ((now or _dt.datetime.now(_dt.timezone.utc)) - t).total_seconds()
    if s < 45:
        return "just now"
    if s < 5400:
        return f"{max(1, round(s / 60))} min ago"
    if s < 86400 * 2:
        return f"{round(s / 3600)} h ago"
    return f"{round(s / 86400)} days ago"


def _snippet(text: str | None, limit: int = 140) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def format_event(e: dict, now: _dt.datetime | None = None) -> str:
    """One line for an owner event, e.g. `MG-4 owner commented (2 min ago): "..."`."""
    when = _ago(e.get("at", ""), now)
    head = f"{e['issue']}"
    t = e.get("type")
    if t == "comment":
        return f'{head} {e["actor"]} commented ({when}): "{_snippet(e.get("text"))}"'
    if t == "verdict":
        verdict = "PASSED" if e.get("verdict") == "passed" else "STILL BROKEN"
        text = _snippet(e.get("text"))
        return f"{head} {e['actor']}: {verdict} ({when})" + (f': "{text}"' if text else "")
    if t == "report":
        return f'{head} new report from {e.get("source") or e["actor"]} ({when}): {_snippet(e.get("title"), 100)}'
    if t == "status":
        return f"{head} {e['actor']} moved it {e.get('from')} -> {e.get('to')} ({when}): {_snippet(e.get('title'), 80)}"
    if t == "plan":
        what = []
        if e.get("to"):
            what.append(f"marked step {e.get('step')} {e['to']}")
        if e.get("note"):
            what.append(f'noted on step {e.get("step")}: "{_snippet(e["note"], 100)}"')
        if not what:
            what.append(f"changed step {e.get('step')}")
        return f"{head} {e['actor']} {' and '.join(what)} ({when})"
    if t == "merged":
        return f"{head} {e['actor']} merged {e.get('source_issue')} into it ({when})"
    if t == "parent":
        parent = e.get("parent")
        return f"{head} {e['actor']} " + (f"made it part of {parent}" if parent else "removed its parent") + f" ({when})"
    return f"{head} {e.get('actor')} {t} ({when})"


def format_block(project_name: str, events: list[dict], more: int, since: str | None = None) -> str:
    """The prompt context block: at most MAX_LINES lines, newest first, with issue ids."""
    lines = [f"Pair Desk ({project_name}): owner activity since this session last looked, newest first:"]
    room = MAX_LINES - 2
    shown = events[:room]
    lines += [f"- {format_event(e)}" for e in shown]
    rest = more + len(events) - len(shown)
    if rest > 0:
        lines.append(f"- … and {rest} more" + (f" (list_issues since={since})" if since else ""))
    return "\n".join(lines)


def channel_message(e: dict, project: str) -> dict:
    """The params of one `notifications/claude/channel` message for an owner event."""
    line = format_event(e)
    if e.get("type") in ("comment", "verdict") and e.get("text"):
        body = " ".join(e["text"].split())
        if len(body) > 140:
            line += f"\n\nFull text: {body[:1500]}"
    line += f"\n\nOpen it with the pair-desk get_issue tool (id {e['issue']})."
    meta = {"issue": e["issue"], "event": e.get("type", ""), "project": project}
    return {"content": line, "meta": meta}


# ------------------------------------------------------------------------------------- cursors

def _cursor_path(data_dir: Path) -> Path:
    return Path(data_dir) / CURSOR_FILE


def load_cursors(data_dir: Path) -> dict:
    try:
        data = json.loads(_cursor_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sessions", {})
    data.setdefault("projects", {})
    return data


def save_cursors(data_dir: Path, data: dict) -> None:
    now = _dt.datetime.now(_dt.timezone.utc)
    keep = {}
    for sid, entry in data.get("sessions", {}).items():
        try:
            seen = _dt.datetime.fromisoformat(str(entry.get("at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if now - seen <= SESSION_TTL:
            keep[sid] = entry
    data["sessions"] = keep
    path = _cursor_path(data_dir)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_seen(data_dir: Path, slug: str, session_id: str | None, cursor: dict) -> None:
    data = load_cursors(data_dir)
    entry = {"project": slug, "cursor": cursor, "at": _stamp()}
    if session_id:
        data["sessions"][session_id] = entry
    data["projects"][slug] = entry
    save_cursors(data_dir, data)


def session_cursor(data_dir: Path, slug: str, session_id: str | None) -> dict | None:
    data = load_cursors(data_dir)
    entry = data["sessions"].get(session_id or "") if session_id else None
    if not entry or entry.get("project") != slug:
        entry = data["projects"].get(slug)
    return entry.get("cursor") if entry else None


def prompt_context(store, slug: str, session_id: str | None) -> str | None:
    """The UserPromptSubmit block for `slug`, advancing this session's cursor. None when there is
    nothing new, or when this session's channel already pushed it."""
    data_dir = store.data_dir
    cursor = session_cursor(data_dir, slug, session_id)
    res = store.owner_events(slug, cursor, limit=MAX_LINES)
    mark_seen(data_dir, slug, session_id, res["cursor"])
    if cursor is None or not res["events"]:
        return None
    if live_channel_for_this_session(data_dir):
        return None
    name = store.get_project(slug)["name"]
    since = res["events"][-1]["at"] if res["more"] else None
    return format_block(name, res["events"], res["more"], since)


# ------------------------------------------------------------------------------------ processes

def _windows_parents() -> dict[int, tuple[int, str]]:
    """pid -> (parent pid, exe name) for every process, via the Toolhelp snapshot (fast)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wintypes.HANDLE(-1).value:
        return {}
    out: dict[int, tuple[int, str]] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def ancestor_pids(limit: int = MAX_ANCESTORS) -> list[int]:
    """The parent, grandparent, ... of this process (nearest first). Empty when unknown."""
    out: list[int] = []
    try:
        if os.name == "nt":
            table = _windows_parents()
            pid = table.get(os.getpid(), (os.getppid(), ""))[0]
            while pid and pid in table and len(out) < limit and pid not in out:
                out.append(pid)
                pid = table[pid][0]
            return out
        pid = os.getppid()
        while pid > 1 and len(out) < limit:
            out.append(pid)
            if sys.platform.startswith("linux"):
                stat = Path(f"/proc/{pid}/stat").read_text()
                pid = int(stat[stat.rindex(")") + 2:].split()[1])
            else:
                pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                                         text=True, timeout=3).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        pass
    return out


def command_lines(pids: list[int]) -> dict[int, str]:
    """pid -> command line for the given processes (best effort)."""
    if not pids:
        return {}
    out: dict[int, str] = {}
    try:
        if os.name == "nt":
            flt = " OR ".join(f"ProcessId={int(p)}" for p in pids)
            script = (f'Get-CimInstance Win32_Process -Filter "{flt}" | '
                      'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }')
            res = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in res.stdout.splitlines():
                pid, _, cmd = line.partition("\t")
                if pid.strip().isdigit():
                    out[int(pid)] = cmd.strip()
        elif sys.platform.startswith("linux"):
            for p in pids:
                try:
                    out[p] = Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                except OSError:
                    pass
        else:
            for p in pids:
                res = subprocess.run(["ps", "-o", "args=", "-p", str(p)], capture_output=True, text=True, timeout=3)
                out[p] = res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def cmdline_enables_channel(cmdline: str, plugin: str = PLUGIN_NAME) -> bool:
    """Whether a Claude Code command line loads this plugin's channel (`--channels` or
    `--dangerously-load-development-channels` with an entry naming the plugin)."""
    tokens = [t.strip("\"'") for t in cmdline.split()]
    values: list[str] = []
    for n, tok in enumerate(tokens):
        flag = next((f for f in CHANNEL_FLAGS if tok.startswith(f + "=")), None)
        if flag:
            values.append(tok[len(flag) + 1:])
        elif tok in CHANNEL_FLAGS:
            for nxt in tokens[n + 1:]:
                if nxt.startswith("-"):
                    break
                values.append(nxt)
    return any(plugin in entry for v in values for entry in v.split(","))


def detect_channel() -> tuple[bool, int | None]:
    """(enabled, Claude process id) for the MCP server. `PAIR_DESK_CHANNEL=on|off` overrides the
    detection; `auto` (default) looks for the channel flag on the ancestor processes."""
    mode = os.environ.get(CHANNEL_ENV, "auto").strip().lower()
    if mode in ("off", "0", "false", "no", "hooks"):
        return False, None
    ancestors = ancestor_pids()
    cmds = command_lines(ancestors)
    for pid in ancestors:
        if cmdline_enables_channel(cmds.get(pid, "")):
            return True, pid
    if mode in ("on", "1", "true", "yes", "channel"):
        claude = next((p for p in ancestors if "claude" in cmds.get(p, "").lower()), None)
        return True, claude or (ancestors[0] if ancestors else os.getppid())
    return False, None


def channel_file(data_dir: Path, claude_pid: int) -> Path:
    return Path(data_dir) / CHANNEL_DIR / f"{int(claude_pid)}.json"


def write_heartbeat(data_dir: Path, claude_pid: int, project: str) -> None:
    f = channel_file(data_dir, claude_pid)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"project": project, "server_pid": os.getpid(), "heartbeat": time.time()}),
                   encoding="utf-8")
    os.replace(tmp, f)


def remove_heartbeat(data_dir: Path, claude_pid: int) -> None:
    try:
        channel_file(data_dir, claude_pid).unlink()
    except OSError:
        pass


def live_channel_for_this_session(data_dir: Path) -> bool:
    """Whether a pair-desk channel is pushing into the Claude process this hook runs under."""
    folder = Path(data_dir) / CHANNEL_DIR
    if not folder.is_dir():
        return False
    for pid in ancestor_pids():
        try:
            data: Any = json.loads(channel_file(data_dir, pid).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if time.time() - float(data.get("heartbeat", 0)) <= CHANNEL_FRESH_SECONDS:
            return True
    return False
