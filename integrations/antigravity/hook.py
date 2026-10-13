"""Translate Antigravity's native camelCase hook payloads to Pair Desk's hook contract.

https://antigravity.google/docs/hooks/ defines workspacePaths, conversationId and
injectSteps. Reuse the existing desk context/cursors, including multi-workspace sessions.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def run_hook(event: str, data_dir: Path) -> int:
    steps = []
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("expected an object")
        workspaces = payload.get("workspacePaths", [])
        session = payload.get("conversationId")
        if not isinstance(workspaces, list) or not isinstance(session, str) or not session:
            raise ValueError("expected workspacePaths and conversationId")
        if not (data_dir / "desk.sqlite").is_file():
            print("{}")
            return 0
        from pair_desk.cli import cmd_hook
        from pair_desk.context import resolve_project
        inner = "session-start" if event == "antigravity-session-start" else "user-prompt-submit"
        seen = set()
        for workspace in workspaces:
            if not isinstance(workspace, str) or not Path(workspace).is_absolute():
                continue
            project = resolve_project(data_dir, workspace)
            if not project or project in seen:
                continue
            seen.add(project)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                cmd_hook(SimpleNamespace(event=inner), data_dir,
                         {"cwd": workspace, "session_id": f"antigravity:{session}:{project}"})
            if not output.getvalue().strip():
                continue
            result = json.loads(output.getvalue())
            context = result.get("hookSpecificOutput", {}).get("additionalContext")
            if context:
                message = (f"Pair Desk workspace {workspace}; pass project={project} explicitly to MCP tools.\n"
                           + context)
                steps.append({"systemMessage": {"systemMessage": message}})
    except Exception:  # A failed local desk must not fail the agent's invocation.
        pass
    print(json.dumps({"injectSteps": steps} if steps else {}))
    return 0
