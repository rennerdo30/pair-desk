"""Antigravity config preservation, lifecycle and real MCP launch checks."""

import contextlib
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

from helpers import ROOT, TempDirCase
from integrations import common, installer
from integrations.antigravity import install as antigravity
from pair_desk.store import Store


class AntigravityInstallTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.folder = self.tmp / "gemini" / "antigravity"
        self.folder.mkdir(parents=True)
        self.config = self.folder / "mcp_config.json"
        self.rules = self.folder.parent / "GEMINI.md"
        self.skills = self.tmp / "skills"
        self.original_config = json.dumps({"mcpServers": {
            "other": {"command": "other", "args": ["keep"]}}, "preferences": {"keep": True}})
        self.original_rules = "# Existing rules\r\n\r\nKeep these exactly.\r\n"
        common.write_text(self.config, self.original_config)
        common.write_text(self.rules, self.original_rules)

    def install(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = antigravity.run(["--config-dir", str(self.folder), "--rules-file", str(self.rules),
                                   "--skills-dir", str(self.skills), "--python", sys.executable, *extra])
        return code, out.getvalue() + err.getvalue()

    def test_merge_backup_idempotency_and_uninstall_preserve_user_content(self):
        code, out = self.install()
        self.assertEqual(code, 0, out)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["mcpServers"]["other"], {"command": "other", "args": ["keep"]})
        self.assertEqual(config["preferences"], {"keep": True})
        self.assertEqual(config["mcpServers"]["pair-desk"]["command"], sys.executable)
        self.assertTrue(common.read_text(self.rules).startswith(self.original_rules))
        first = self.config.read_bytes(), self.rules.read_bytes()
        code, out = self.install()
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to change", out)
        self.assertEqual(first, (self.config.read_bytes(), self.rules.read_bytes()))
        backups = list(self.folder.glob("mcp_config.json.pair-desk-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(common.read_text(backups[0]), self.original_config)
        code, out = self.install("--uninstall")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(self.config.read_text()), json.loads(self.original_config))
        self.assertEqual(common.read_text(self.rules), self.original_rules)
        self.assertFalse((self.skills / "pair-desk").exists())
        self.assertIn("Nothing to change", self.install("--uninstall")[1])

    def test_dry_run_does_not_write_config_rules_skills_or_backups(self):
        before = self.config.read_bytes(), self.rules.read_bytes()
        self.assertEqual(self.install("--dry-run")[0], 0)
        self.assertEqual(before, (self.config.read_bytes(), self.rules.read_bytes()))
        self.assertFalse(self.skills.exists())
        self.assertEqual(list(self.folder.parent.rglob("*.pair-desk-backup-*")), [])

    def test_invalid_config_or_rules_prevents_all_writes(self):
        for bad in ("[]", '{"mcpServers":null}', '{"mcpServers":[]}', '{"mcpServers":'):
            with self.subTest(config=bad):
                self.config.write_text(bad)
                self.assertEqual(self.install()[0], 1)
                self.assertEqual(self.config.read_text(), bad)
                self.assertEqual(common.read_text(self.rules), self.original_rules)
                self.assertFalse(self.skills.exists())
        common.write_text(self.config, self.original_config)
        common.write_text(self.rules, self.original_rules + antigravity.RULE_START)
        self.assertEqual(self.install()[0], 1)
        self.assertEqual(common.read_text(self.config), self.original_config)
        self.assertFalse(self.skills.exists())

    def test_shared_skills_survive_uninstall_until_last_owner(self):
        common.install_skills(self.skills, "codex", common.Report(), 'python "desk.py"')
        self.install()
        self.install("--uninstall")
        marker = json.loads((self.skills / "pair-desk" / common.MARKER).read_text())
        self.assertEqual(marker["installed_by"], ["codex"])
        common.uninstall_skills(self.skills, "codex", common.Report())
        self.assertFalse((self.skills / "pair-desk").exists())

    def test_foreign_skills_and_optional_rules_are_preserved(self):
        foreign = self.skills / "pair-desk" / "SKILL.md"
        common.write_text(foreign, "user-owned skill")
        self.install("--no-rules")
        self.assertEqual(foreign.read_text(), "user-owned skill")
        self.assertEqual(common.read_text(self.rules), self.original_rules)
        self.install("--uninstall", "--no-rules")
        self.assertEqual(foreign.read_text(), "user-owned skill")

    def test_update_managed_rules_preserves_user_additions_after_block(self):
        self.install()
        text = common.read_text(self.rules) + "\n# More user rules\n"
        common.write_text(self.rules, text)
        self.install("--python", "custom-python")
        updated = common.read_text(self.rules)
        self.assertIn("custom-python", updated)
        self.assertTrue(updated.endswith("\n# More user rules\n"))
        self.install("--uninstall")
        self.assertEqual(common.read_text(self.rules), self.original_rules + "\n# More user rules\n")

    def test_configured_command_runs_real_mcp_and_enforces_owner_verdict(self):
        self.assertEqual(self.install()[0], 0)
        entry = json.loads(self.config.read_text())["mcpServers"]["pair-desk"]
        data = self.tmp / "desk-data"
        with Store(data) as store:
            store.create_project("mygame", "MyGame", "MG")
            store.create_project("other", "Other", "OT")
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "antigravity-test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        for mid, name, arguments in [(3, "list_projects", {}),
                (4, "create_issue", {"project": "mygame", "title": "Check terrain", "command": "/goto 1 2 3"}),
                (5, "comment", {"id": "MG-1", "text": "Verification evidence"}),
                (6, "set_status", {"id": "MG-1", "status": "passed"})]:
            messages.append({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}})
        env = dict(os.environ, **entry["env"], PAIR_DESK_DATA=str(data), PAIR_DESK_CHANNEL="off")
        proc = subprocess.run([entry["command"], *entry["args"]], cwd=self.tmp, env=env,
                              input="\n".join(json.dumps(m) for m in messages) + "\n",
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = {m["id"]: m["result"] for m in map(json.loads, proc.stdout.splitlines())}
        self.assertEqual(responses[1]["serverInfo"]["name"], "pair-desk")
        self.assertIn("set_handoff", [t["name"] for t in responses[2]["tools"]])
        created = json.loads(responses[4]["content"][0]["text"])
        self.assertEqual(created["id"], "MG-1")
        comment = json.loads(responses[5]["content"][0]["text"])
        self.assertEqual(comment["comment"]["author"], "antigravity")
        self.assertTrue(responses[6]["isError"])
        self.assertIn("owner", responses[6]["content"][0]["text"])

    def test_desktop_detection_and_explicit_selection_do_not_require_cli(self):
        with patch.object(installer, "_which", return_value=None), \
                patch.object(antigravity, "config_dir", return_value=self.folder):
            self.assertTrue(installer.tool_available("antigravity"))
        with patch.object(installer, "_which", return_value=None), \
                patch.object(antigravity, "config_dir", return_value=self.tmp / "missing"), \
                patch("integrations.installer.Path.exists", return_value=False):
            self.assertFalse(installer.tool_available("antigravity"))
            self.assertTrue(installer.tool_available("antigravity", explicit=True))
            self.assertFalse(installer.tool_available("codex", explicit=True))

    def test_top_level_install_update_uninstall_keep_other_app_users(self):
        home = self.tmp / "home"
        home.mkdir()
        app = self.tmp / "app with spaces"
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PAIR_DESK_DATA=str(self.tmp / "data"))
        for action in ("install", "update"):
            result = subprocess.run([sys.executable, str(ROOT / "desk.py"), action, "antigravity",
                                     "--yes", "--no-link", "--app-dir", str(app), "--python", sys.executable],
                                    cwd=self.tmp, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((home / ".gemini" / "antigravity" / "mcp_config.json").read_text())
        self.assertEqual(config["mcpServers"]["pair-desk"]["args"], [common.fwd(app / "desk.py"), "mcp"])
        marker = json.loads((app / installer.APP_MARKER).read_text())
        marker["used_by"].append("codex")
        (app / installer.APP_MARKER).write_text(json.dumps(marker))
        result = subprocess.run([sys.executable, str(ROOT / "desk.py"), "uninstall", "antigravity",
                                 "--yes", "--no-link", "--app-dir", str(app)],
                                cwd=self.tmp, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(app.exists())
        self.assertEqual(json.loads((app / installer.APP_MARKER).read_text())["used_by"], ["codex"])
