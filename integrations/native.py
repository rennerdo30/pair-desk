"""JSON and shell helpers for the Antigravity and Muse integrations."""

from __future__ import annotations

import base64
import json
import os
import shlex
import stat
from pathlib import Path

from integrations.common import read_text, strip_jsonc


def check_unlinked(path: Path) -> None:
    """Do not follow a user-config symlink or Windows junction during install/uninstall."""
    for part in (path, *path.parents):
        try:
            attributes = getattr(part.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        if part.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ValueError(f"linked installation paths are not supported: {part}")


def load_object(path: Path) -> tuple[dict, bool]:
    text = read_text(path)
    if text is None:
        return {}, False
    clean, comments = strip_jsonc(text.lstrip("\ufeff"))
    value = json.loads(clean)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value, comments


def dump(value: dict) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def hook_command(prefix: list[str], event: str, client: str, windows: bool | None = None) -> str:
    """Native hooks use a shell. Quote argv literally; keep Windows paths out of its parser."""
    argv = [*prefix, "hook", event, "--client", client]
    marker = f"# pair-desk:{client}:hook"
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return shlex.join(argv) + " " + marker
    quote = lambda s: "'" + s.replace("'", "''") + "'"
    script = (marker + "\n"
              "$ProgressPreference = 'SilentlyContinue'; "
              "$OutputEncoding = [Console]::InputEncoding = [Console]::OutputEncoding = "
              "[System.Text.UTF8Encoding]::new($false); "
              "$pairDeskInput = [Console]::In.ReadToEnd(); "
              "$pairDeskInput | & " + " ".join(quote(a) for a in argv) + "; exit $LASTEXITCODE")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand " + encoded


def managed_hook(group: object, client: str) -> bool:
    """Only remove complete groups written by us; keep user hooks even in the same event."""
    if not isinstance(group, dict) or set(group) != {"hooks"}:
        return False
    hooks = group["hooks"]
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        return False
    command = hooks[0].get("command")
    if not isinstance(command, str):
        return False
    if command.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "):
        try:
            command = base64.b64decode(command.rsplit(" ", 1)[1], validate=True).decode("utf-16le")
        except (ValueError, UnicodeError):
            return False
    marker = f"# pair-desk:{client}:hook"
    return command.startswith(marker + "\n") or command.endswith(" " + marker)
