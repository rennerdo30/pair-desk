"""Open a build's folder or run its player on this machine.

Only ever acts on a build path the desk has stored (the project's current build or an issue's stamped build),
never on a path from a request. Open reveals the path in the OS file manager; Run starts the executable detached,
in its own folder, without a shell and without arguments. The desk server allows both only to the page it served
itself (server.py checks the client, the Origin and its per-server token); agents have no tool for them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .store import Conflict

# Executables Run starts on Windows (no scripts: nothing that needs a shell or an interpreter).
WINDOWS_RUNNABLE = (".exe", ".com")


def registered_path(build: dict) -> str:
    """Shared evidence needs an explicit local build registration before execution."""
    if build.get("sync_remote"):
        raise Conflict("this build is shared evidence; register its local path with build set before opening or running it")
    return build["path"]


def local_info(path: str | None) -> dict:
    """What this machine can do with a build path: {exists, kind: file|folder|None, open, run}. A version string or
    a relative path is not a place on disk, so it gets nothing."""
    out = {"exists": False, "kind": None, "open": False, "run": False}
    if not path:
        return out
    p = Path(path).expanduser()
    try:
        if not p.is_absolute() or not p.exists():
            return out
        is_file = p.is_file()
    except (OSError, ValueError):
        return out
    out.update(exists=True, kind="file" if is_file else "folder", open=True)
    out["run"] = is_file and runnable(p)
    return out


def runnable(p: Path) -> bool:
    if os.name == "nt":
        return p.suffix.lower() in WINDOWS_RUNNABLE
    return os.access(p, os.X_OK)


def _spawn(argv: list[str], cwd: str | None = None) -> None:
    """Start `argv` detached from the desk (no shell, no inherited console, no waiting). Tests replace it."""
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True, "cwd": cwd}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)  # noqa: S603 - a stored path, no shell


def _check(path: str | None) -> tuple[Path, dict]:
    info = local_info(path)
    if not info["exists"]:
        raise Conflict(f"the build {path!r} is not a file or folder on this machine" if path else "no build")
    return Path(path).expanduser(), info


def reveal(path: str | None) -> dict:
    """Show the build in the file manager: the file selected in its folder, or the folder itself."""
    p, info = _check(path)
    if sys.platform == "win32":
        argv = ["explorer", f"/select,{p}"] if info["kind"] == "file" else ["explorer", str(p)]
    elif sys.platform == "darwin":
        argv = ["open", "-R", str(p)] if info["kind"] == "file" else ["open", str(p)]
    else:
        argv = ["xdg-open", str(p.parent if info["kind"] == "file" else p)]
    _spawn(argv)
    return {"ok": True, "action": "open", "path": str(p), "folder": str(p.parent if info["kind"] == "file" else p)}


def run(path: str | None) -> dict:
    """Start the build's executable in its own folder."""
    p, info = _check(path)
    if info["kind"] != "file":
        raise Conflict(f"the build {str(p)!r} is a folder; there is nothing to run (Open shows it)")
    if not info["run"]:
        raise Conflict(f"the build {str(p)!r} is not an executable this desk starts")
    _spawn([str(p)], cwd=str(p.parent))
    return {"ok": True, "action": "run", "path": str(p), "started": p.name}
