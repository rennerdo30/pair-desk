"""`pair-desk install | update | uninstall`: the plan per tool, dry runs, the official Claude Code
commands, the Codex and opencode integrations, the app copy, linking, and asking. Fake `claude`,
`codex` and `opencode` executables on PATH record what would have run; HOME points at a temp
folder, so nothing outside it is touched."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from helpers import ROOT, TempDirCase

from integrations import installer

TOOLS = ("claude", "codex", "opencode")


def fake_tools(folder: Path, names=TOOLS) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        if os.name == "nt":
            (folder / f"{name}.cmd").write_text(f'@echo off\r\necho {name} %*>> "%FAKE_LOG%"\r\n', encoding="ascii")
        else:
            f = folder / name
            f.write_text(f'#!/bin/sh\necho "{name} $*" >> "$FAKE_LOG"\n', encoding="ascii")
            f.chmod(0o755)


class InstallerCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.bin = self.tmp / "fakebin"
        self.log = self.tmp / "calls.log"
        self.app = self.tmp / "app"
        self.data = self.tmp / "data"
        self.work = self.tmp / "work"  # not a git repo: no link offer
        self.work.mkdir()

    def env(self, tools=TOOLS) -> dict:
        fake_tools(self.bin, tools)
        system = ([os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")] if os.name == "nt"
                  else ["/usr/bin", "/bin"])
        env = {k: v for k, v in os.environ.items()
               if k not in ("CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PAIR_DESK_PYTHON", "PAIR_DESK_APP")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home), LOCALAPPDATA=str(self.home / "local"),
                   PAIR_DESK_DATA=str(self.data), FAKE_LOG=str(self.log),
                   PATH=os.pathsep.join([str(self.bin), os.path.dirname(sys.executable), *system]))
        return env

    def run_desk(self, *args, tools=TOOLS):
        res = subprocess.run([sys.executable, str(ROOT / "desk.py"), *args], cwd=self.work, env=self.env(tools),
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                             timeout=120)
        return res.returncode, res.stdout + res.stderr

    def calls(self) -> list[str]:
        if not self.log.exists():
            return []
        return [line.strip() for line in self.log.read_text(encoding="utf-8").splitlines()]


class PlanTest(InstallerCase):
    def test_plans_name_only_the_official_commands(self):
        ctx = installer.Context(source=ROOT, app=ROOT)

        def describe(steps):
            return [s.describe(ctx.app, ctx.source) for s in steps]

        self.assertEqual(describe(installer.plan_for("claude", "install", ctx)), [
            f"claude plugin marketplace add {installer.MARKETPLACE_REPO}",
            "claude plugin marketplace update agent-pair-programming",
            "claude plugin install agent-pair-programming@agent-pair-programming",
        ])
        self.assertEqual(describe(installer.plan_for("claude", "update", ctx)), [
            "claude plugin marketplace update agent-pair-programming",
            "claude plugin update agent-pair-programming@agent-pair-programming",
        ])
        self.assertEqual(describe(installer.plan_for("claude", "uninstall", ctx)),
                         ["claude plugin uninstall agent-pair-programming@agent-pair-programming"])
        copy_ctx = installer.Context(source=ROOT, app=self.app)
        for tool in ("codex", "opencode"):
            steps = installer.plan_for(tool, "install", ctx)
            self.assertEqual([s.kind for s in steps], ["integration"], "the source itself is used in place")
            self.assertEqual(Path(steps[0].argv[0]), ROOT / "integrations" / tool / "install.py")
            self.assertEqual([s.kind for s in installer.plan_for(tool, "install", copy_ctx)], ["copy", "integration"])
            self.assertEqual([s.kind for s in installer.plan_for(tool, "update", copy_ctx)], ["copy", "integration"])
            self.assertEqual([s.kind for s in installer.plan_for(tool, "uninstall", copy_ctx)],
                             ["integration", "remove-app"])

    def test_dry_run_shows_every_plan_and_changes_nothing(self):
        code, out = self.run_desk("install", "--dry-run", "--app-dir", str(self.app))
        self.assertEqual(code, 0, out)
        for tool in TOOLS:
            self.assertIn(f"{tool}: these steps will run:", out)
        self.assertIn(f"claude plugin marketplace add {installer.MARKETPLACE_REPO}", out)
        self.assertIn("claude plugin install agent-pair-programming@agent-pair-programming", out)
        self.assertIn(f"copy Pair Desk from", out)
        self.assertIn("would update Codex MCP server and Pair Desk hooks", out)
        self.assertIn("would update opencode MCP server", out)
        self.assertIn("Dry run done: nothing was changed.", out)
        self.assertEqual(self.calls(), [], "a dry run runs no tool command")
        for untouched in (self.app, self.home / ".codex", self.home / ".config", self.home / ".agents", self.data):
            self.assertFalse(untouched.exists(), untouched)

    def test_dry_run_of_each_tool_and_action(self):
        for action in ("install", "update", "uninstall"):
            for tool in TOOLS:
                code, out = self.run_desk(action, tool, "--dry-run")
                self.assertEqual(code, 0, out)
                self.assertIn(f"{tool}: these steps will run:", out)
                for other in set(TOOLS) - {tool}:
                    self.assertNotIn(f"{other}: these steps", out)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.home / ".codex").exists())

    def test_missing_tool_is_skipped(self):
        code, out = self.run_desk("install", "--dry-run", tools=("claude",))
        self.assertEqual(code, 0, out)
        self.assertIn("codex is not installed (not found on PATH); skipping it.", out)
        self.assertIn("opencode is not installed (not found on PATH); skipping it.", out)

    def test_unknown_tool_is_an_error(self):
        code, out = self.run_desk("install", "vscode", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("unknown tool vscode", out)


class RunTest(InstallerCase):
    def test_without_a_terminal_the_answer_is_no(self):
        code, out = self.run_desk("install", "claude")
        self.assertEqual(code, 0, out)
        self.assertIn("Run them for claude? [y/N]", out)
        self.assertIn("Skipped claude.", out)
        self.assertEqual(self.calls(), [])

    def test_claude_runs_the_official_commands_in_order(self):
        for action in ("install", "update", "uninstall"):
            code, out = self.run_desk(action, "claude", "--yes")
            self.assertEqual(code, 0, out)
        self.assertEqual(self.calls(), [
            f"claude plugin marketplace add {installer.MARKETPLACE_REPO}",
            "claude plugin marketplace update agent-pair-programming",
            "claude plugin install agent-pair-programming@agent-pair-programming",
            "claude plugin marketplace update agent-pair-programming",
            "claude plugin update agent-pair-programming@agent-pair-programming",
            "claude plugin uninstall agent-pair-programming@agent-pair-programming",
        ])

    def test_marketplace_can_be_a_local_clone(self):
        self.run_desk("install", "claude", "--yes", "--marketplace", str(ROOT))
        self.assertEqual(self.calls()[0], f"claude plugin marketplace add {ROOT}")

    def test_codex_and_opencode_run_a_copy_that_uninstall_removes(self):
        code, out = self.run_desk("install", "codex", "opencode", "--yes", "--app-dir", str(self.app))
        self.assertEqual(code, 0, out)
        self.assertTrue((self.app / "desk.py").is_file())
        self.assertTrue((self.app / "web" / "index.html").is_file())
        self.assertFalse((self.app / "tests").exists())
        self.assertEqual(json.loads((self.app / installer.APP_MARKER).read_text(encoding="utf-8"))["used_by"],
                         ["codex", "opencode"])
        codex = tomllib.loads((self.home / ".codex" / "config.toml").read_text(encoding="utf-8"))
        server = codex["mcp_servers"]["pair-desk"]
        self.assertIn(self.app.as_posix(), " ".join([server["command"], *server["args"]]),
                      "Codex runs the copy, not the source")
        oc = json.loads((self.home / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8"))
        self.assertIn(self.app.as_posix(), " ".join(oc["mcp"]["pair-desk"]["command"]))
        skill = (self.home / ".agents" / "skills" / "pair-desk-serve" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(self.app.as_posix(), skill)
        self.assertIn("Start the desk:", out)
        self.assertEqual(self.calls(), [], "Codex and opencode need no CLI command")

        code, out = self.run_desk("uninstall", "codex", "--yes", "--app-dir", str(self.app))
        self.assertEqual(code, 0, out)
        self.assertIn("still used by opencode", out)
        self.assertTrue(self.app.exists())
        code, out = self.run_desk("uninstall", "opencode", "--yes", "--app-dir", str(self.app))
        self.assertEqual(code, 0, out)
        self.assertFalse(self.app.exists())
        self.assertNotIn("pair-desk", (self.home / ".codex" / "config.toml").read_text(encoding="utf-8"))
        self.assertFalse((self.home / ".agents" / "skills" / "pair-desk").exists())

    def test_a_foreign_app_folder_is_never_overwritten(self):
        self.app.mkdir()
        (self.app / "mine.txt").write_text("keep", encoding="utf-8")
        code, out = self.run_desk("install", "codex", "--yes", "--app-dir", str(self.app))
        self.assertEqual(code, 1, out)
        self.assertIn("was not created by the Pair Desk installer", out)
        self.assertEqual((self.app / "mine.txt").read_text(encoding="utf-8"), "keep")


class LinkTest(InstallerCase):
    def setUp(self):
        super().setUp()
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_link_creates_the_project_and_links_the_repo(self):
        from pair_desk.context import resolve_project
        from pair_desk.store import Store
        repo = self.tmp / "my-game"
        (repo / ".git").mkdir(parents=True)
        installer.offer_link(self.data, repo / "src", None, False, False, False, ask=lambda prompt: "")
        self.assertEqual(resolve_project(self.data, repo), "my-game")
        with Store(self.data) as store:
            self.assertEqual(store.get_project("my-game")["name"], "my-game")

    def test_yes_dry_run_and_no_do_not_link(self):
        repo = self.tmp / "g"
        (repo / ".git").mkdir(parents=True)
        installer.offer_link(self.data, repo, None, False, True, False, ask=lambda prompt: "x")
        installer.offer_link(self.data, repo, None, False, False, True, ask=lambda prompt: "x")
        installer.offer_link(self.data, repo, None, False, False, False, ask=lambda prompt: "n")
        installer.offer_link(self.data, repo, "x", True, False, False, ask=lambda prompt: "x")
        self.assertFalse(self.data.exists(), "nothing was written")

    def test_link_flag_names_the_project(self):
        from pair_desk.context import resolve_project
        repo = self.tmp / "g"
        (repo / ".git").mkdir(parents=True)
        installer.offer_link(self.data, repo, "mygame", False, True, False, ask=lambda prompt: "")
        self.assertEqual(resolve_project(self.data, repo), "mygame")


class NativeToolsTest(InstallerCase):
    def test_both_tools_share_the_app_and_remove_only_their_own_setup(self):
        binaries = ("agy", "muse")
        code, out = self.run_desk("install", "antigravity", "muse", "--yes", "--app-dir", str(self.app),
                                  tools=binaries)
        self.assertEqual(code, 0, out)
        plugin = self.home / ".gemini" / "config" / "plugins" / "pair-desk"
        config = self.home / ".config" / "muse" / "settings.json"
        self.assertTrue((plugin / "plugin.json").is_file())
        self.assertIn("pair-desk", json.loads(config.read_text())["mcp_servers"])
        marker = json.loads((self.app / installer.APP_MARKER).read_text())
        self.assertEqual(marker["used_by"], ["antigravity", "muse"])
        code, out = self.run_desk("update", "antigravity", "muse", "--yes", "--app-dir", str(self.app),
                                  tools=binaries)
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to change", out)
        code, out = self.run_desk("uninstall", "antigravity", "--yes", "--app-dir", str(self.app), tools=binaries)
        self.assertEqual(code, 0, out)
        self.assertFalse((plugin / "plugin.json").exists())
        self.assertTrue((self.app / "desk.py").exists())
        self.assertIn("still used by muse", out)
        code, out = self.run_desk("uninstall", "muse", "--yes", "--app-dir", str(self.app), tools=binaries)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.app.exists())
        self.assertNotIn("pair-desk", json.loads(config.read_text()).get("mcp_servers", {}))
        self.assertFalse((self.home / ".agents" / "skills" / "pair-desk").exists())
        self.assertEqual(self.calls(), [], "neither integration needs a native CLI mutation")

    def test_antigravity_detection_requires_agy_not_an_ide_launcher(self):
        code, out = self.run_desk("install", "antigravity", "--dry-run", tools=("antigravity",))
        self.assertEqual(code, 0, out)
        self.assertIn("agy not found on PATH", out)
        self.assertNotIn("antigravity: these steps will run", out)


if __name__ == "__main__":
    import unittest
    unittest.main()
