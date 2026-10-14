"""Codex installer: config.toml merge, backups, idempotency, uninstall, and the shared skills."""

import contextlib
import io
import tomllib
from pathlib import Path

from helpers import TempDirCase  # also puts the repo root on sys.path

from integrations import common
from integrations.codex import install as codex
from integrations.opencode import install as opencode

EXISTING = '''model = "gpt-6-luna"
notify = [ "C:\\\\tools\\\\notify.exe", "turn-ended" ]

[features]
memories = true

[mcp_servers.node_repl]
args = []
command = 'C:\\tools\\node_repl.exe'

[mcp_servers.node_repl.env]
CODEX_HOME = 'C:\\Users\\me\\.codex'

[[hooks.PreToolUse]]
matcher = "^Bash$"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "python policy.py"

[[hooks.SessionStart]]
matcher = "startup"

[[hooks.SessionStart.hooks]]
type = "command"
command = "python other_tool.py"

[projects.'d:\\src']
trust_level = "trusted"

[hooks.state."agent-bridge@agent-bridge:plugin.json#hooks[0]:session_start:0:0"]
trusted_hash = "sha256:abc"
'''


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        code = fn(*args)
    return code, out.getvalue()


class CodexInstallTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "codex"
        self.home.mkdir()
        self.skills = self.tmp / "agents-skills"
        self.config = self.home / "config.toml"
        self.config.write_text(EXISTING, encoding="utf-8", newline="")
        self.desk = common.fwd(common.DESK_PY)
        self.prefix = common.desk_command(common.DESK_PY)  # how this OS starts desk.py

    def install(self, *extra):
        return quiet(codex.run, ["--codex-home", str(self.home), "--skills-dir", str(self.skills), *extra])

    def backups(self):
        return sorted(self.home.glob("config.toml.pair-desk-backup-*"))

    def test_install_adds_server_and_hook_and_keeps_everything_else(self):
        code, _ = self.install()
        self.assertEqual(code, 0)
        text = self.config.read_text(encoding="utf-8")
        self.assertTrue(text.startswith(EXISTING.rstrip()), "existing entries must stay byte for byte")
        cfg = tomllib.loads(text)
        self.assertEqual(cfg["mcp_servers"]["pair-desk"], {"command": self.prefix[0], "args": [*self.prefix[1:], "mcp"],
                                                           "startup_timeout_sec": 20})
        self.assertIn("node_repl", cfg["mcp_servers"])
        self.assertEqual(cfg["mcp_servers"]["node_repl"]["env"]["CODEX_HOME"], "C:\\Users\\me\\.codex")
        starts = cfg["hooks"]["SessionStart"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0]["hooks"][0]["command"], "python other_tool.py")
        self.assertEqual(starts[1]["matcher"], "startup|resume|clear")
        self.assertEqual(starts[1]["hooks"][0]["command"], common.shell_line([*self.prefix, "hook", "session-start", "--client", "codex"]))
        prompts = cfg["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["hooks"][0]["command"],
                         common.shell_line([*self.prefix, "hook", "user-prompt-submit", "--client", "codex"]))
        self.assertEqual(len(cfg["hooks"]["PreToolUse"]), 1)
        self.assertIn("state", cfg["hooks"])
        self.assertEqual(cfg["model"], "gpt-6-luna")

    def test_backup_before_change_and_idempotent_rerun(self):
        self.install()
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), EXISTING)
        after_first = self.config.read_bytes()
        code, out = self.install()
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_bytes(), after_first)
        self.assertEqual(len(self.backups()), 1, "an unchanged config gets no new backup")
        self.assertIn("Nothing to change.", out)
        self.assertEqual(self.config.read_text(encoding="utf-8").count("[mcp_servers.pair-desk]"), 1)

    def test_uninstall_restores_the_original(self):
        self.install()
        code, _ = self.install("--uninstall")
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(encoding="utf-8"), EXISTING)
        self.assertEqual(len(self.backups()), 2)
        self.assertFalse((self.skills / "pair-desk").exists())
        code, out = self.install("--uninstall")
        self.assertEqual(len(self.backups()), 2, "uninstalling twice changes nothing")
        self.assertIn("Nothing to change.", out)

    def test_dry_run_writes_nothing(self):
        code, out = self.install("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would update", out)
        self.assertEqual(self.config.read_text(encoding="utf-8"), EXISTING)
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.skills.exists())

    def test_replaces_an_entry_added_by_codex_mcp_add(self):
        manual = EXISTING + '\n[mcp_servers.pair-desk]\ncommand = "py"\nargs = ["old/desk.py", "mcp"]\n\n' \
                            '[mcp_servers.pair-desk.env]\nX = "1"\n\n[tui]\nscreen_reader_detection_done = true\n'
        self.config.write_text(manual, encoding="utf-8", newline="")
        self.install()
        text = self.config.read_text(encoding="utf-8")
        cfg = tomllib.loads(text)
        self.assertEqual(text.count("[mcp_servers.pair-desk]"), 1)
        self.assertNotIn("env", cfg["mcp_servers"]["pair-desk"])
        self.assertEqual(cfg["mcp_servers"]["pair-desk"]["args"], [*self.prefix[1:], "mcp"])
        self.assertTrue(cfg["tui"]["screen_reader_detection_done"])

    def test_block_moved_by_codex_is_still_found(self):
        self.install()
        text = self.config.read_text(encoding="utf-8")
        # Codex rewrote the file and something landed after our block
        self.config.write_text(text + "\n[projects.'e:\\new']\ntrust_level = \"trusted\"\n", encoding="utf-8")
        self.install("--uninstall")
        cfg = tomllib.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn("pair-desk", cfg["mcp_servers"])
        self.assertEqual(len(cfg["hooks"]["SessionStart"]), 1)
        self.assertEqual(cfg["projects"]["e:\\new"]["trust_level"], "trusted")

    def test_invalid_toml_is_left_alone(self):
        self.config.write_text("model = \n[broken", encoding="utf-8")
        code, _ = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_text(encoding="utf-8"), "model = \n[broken")
        self.assertEqual(self.backups(), [])

    def test_missing_config_is_created(self):
        self.config.unlink()
        code, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn("pair-desk", tomllib.loads(self.config.read_text(encoding="utf-8"))["mcp_servers"])
        self.assertEqual(self.backups(), [])
        self.install("--uninstall")
        self.assertEqual(self.config.read_text(encoding="utf-8"), "")


class SkillsTest(TempDirCase):
    def test_rendered_skills_have_no_claude_plugin_paths(self):
        skills = common.render_skills('python "E:/desk/desk.py"')
        self.assertEqual(set(skills), {"pair-desk", "pair-desk-serve", "pair-desk-triage"})
        for name, text in skills.items():
            self.assertTrue(text.startswith(f"---\nname: {name}\n"), name)
            for bad in ("plugin root", "CLAUDE_PLUGIN_ROOT", "two folders above", "/agent-pair-programming:",
                        "bin/pair-desk", "`pair-desk list", "`pair-desk show", "Bash tool"):
                self.assertNotIn(bad, text, f"{name} still says {bad!r}")
        self.assertIn('python "E:/desk/desk.py" serve --detach', skills["pair-desk-serve"])
        self.assertIn('`python "E:/desk/desk.py" stop`', skills["pair-desk-serve"])
        self.assertIn('`python "E:/desk/desk.py" list --status failed,reported --json`', skills["pair-desk-triage"])
        self.assertIn('`python "E:/desk/desk.py" link --project <slug>`', skills["pair-desk"])
        self.assertIn("the `pair-desk` MCP tools", skills["pair-desk-triage"], "the server name is not a command")


class DeskCommandTest(TempDirCase):
    def test_explicit_python_wins(self):
        desk = self.tmp / "desk.py"
        self.assertEqual(common.desk_command(desk, "python3.12"), ["python3.12", common.fwd(desk)])

    def test_posix_uses_the_launcher(self):
        prefix = common.desk_command(common.DESK_PY, windows=False)
        self.assertEqual(prefix, [(common.REPO_ROOT / "bin" / "pair-desk").as_posix()])
        self.assertEqual(common.desk_command(self.tmp / "desk.py", windows=False),
                         ["python3", common.fwd(self.tmp / "desk.py")], "no launcher next to desk.py")

    def test_windows_names_a_working_interpreter(self):
        prefix = common.desk_command(common.DESK_PY, windows=True)
        self.assertEqual(prefix[-1], common.fwd(common.DESK_PY))
        self.assertTrue(common.python_ok(prefix[:-1]), prefix)

    def test_shell_line_quotes_paths_only(self):
        self.assertEqual(common.shell_line(["python", "C:/a b/desk.py", "hook", "session-start"]),
                         'python "C:/a b/desk.py" hook session-start')
        self.assertEqual(common.shell_line(["/opt/pd/bin/pair-desk", "mcp"]), '"/opt/pd/bin/pair-desk" mcp')

    def test_skills_are_shared_between_codex_and_opencode(self):
        skills = self.tmp / "skills"
        foreign = skills / "pair-desk-triage"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("mine", encoding="utf-8")
        home, oc = self.tmp / "codex", self.tmp / "opencode"
        quiet(codex.run, ["--codex-home", str(home), "--skills-dir", str(skills)])
        quiet(opencode.run, ["--config-dir", str(oc), "--skills-dir", str(skills)])
        self.assertTrue((skills / "pair-desk" / "SKILL.md").is_file())
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "mine", "someone else's skill is kept")
        quiet(codex.run, ["--codex-home", str(home), "--skills-dir", str(skills), "--uninstall"])
        self.assertTrue((skills / "pair-desk" / "SKILL.md").is_file(), "opencode still uses it")
        quiet(opencode.run, ["--config-dir", str(oc), "--skills-dir", str(skills), "--uninstall"])
        self.assertFalse((skills / "pair-desk").exists())
        self.assertFalse((skills / "pair-desk-serve").exists())
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "mine")


if __name__ == "__main__":
    import unittest
    unittest.main()
