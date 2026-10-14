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


def managed_handler(handler: object, client: str) -> bool:
    """Recognize our shell command, independently of user additions to its matcher group."""
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    if command.startswith("powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "):
        try:
            command = base64.b64decode(command.rsplit(" ", 1)[1], validate=True).decode("utf-16le")
        except (ValueError, UnicodeError):
            return False
    marker = f"# pair-desk:{client}:hook"
    return command.startswith(marker + "\n") or command.endswith(" " + marker)


def managed_hook(group: object, client: str) -> bool:
    if not isinstance(group, dict):
        return False
    hooks = group.get("hooks")
    return isinstance(hooks, list) and len(hooks) == 1 and managed_handler(hooks[0], client)


def remove_managed_groups(groups: list, client: str) -> list:
    """Remove only our handlers, keeping sibling user handlers and matcher settings."""
    kept = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            kept.append(group)
            continue
        handlers = [h for h in group["hooks"] if not managed_handler(h, client)]
        if handlers == group["hooks"]:
            kept.append(group)
        elif handlers:
            kept.append({**group, "hooks": handlers})
    return kept
