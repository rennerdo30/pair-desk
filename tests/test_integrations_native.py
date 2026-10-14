"""Antigravity/Muse installers and native hooks: isolated files, no model/provider calls."""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from helpers import ROOT, TempDirCase
from integrations import common, installer, native
from integrations.antigravity import install as agy
from integrations.muse import install as muse
from pair_desk import cli
from pair_desk.context import save_link
from pair_desk.store import Store


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        code = fn(*args)
    return code, out.getvalue() + err.getvalue()


class NativeInstallTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.plugin = self.tmp / "plugins" / "pair-desk"
        self.config_dir = self.tmp / "muse"
        self.config = self.config_dir / "settings.json"
        self.skills = self.tmp / "skills"

    def antigravity(self, *extra):
        return quiet(agy.run, ["--plugin-dir", str(self.plugin), "--python", sys.executable, *extra])

    def muse(self, *extra):
        return quiet(muse.run, ["--config-dir", str(self.config_dir), "--skills-dir", str(self.skills),
                                "--python", sys.executable, *extra])

    def write_config(self, value):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def test_antigravity_native_schema_idempotency_and_uninstall_backups(self):
        code, out = self.antigravity()
        self.assertEqual(code, 0, out)
        files = {name: (self.plugin / name).read_bytes() for name in agy.FILES}
        server = json.loads(files["mcp_config.json"])["mcpServers"]["pair-desk"]
        self.assertEqual(server["command"], sys.executable)
        self.assertEqual(server["args"], [common.fwd(common.DESK_PY), "mcp"])
        self.assertEqual(server["env"]["PAIR_DESK_AUTHOR"], "antigravity")
        hooks = json.loads(files["hooks.json"])["pair-desk"]
        self.assertEqual(set(hooks), {"SessionStart", "PreInvocation"})
        self.assertTrue((self.plugin / "skills" / "pair-desk" / "SKILL.md").is_file())
        code, out = self.antigravity()
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to change", out)
        self.assertFalse(list(self.plugin.glob("*.pair-desk-backup-*")))
        self.assertEqual({name: (self.plugin / name).read_bytes() for name in agy.FILES}, files)
        (self.plugin / "user.txt").write_text("keep", encoding="utf-8")
        self.assertEqual(self.antigravity("--uninstall")[0], 0)
        self.assertEqual((self.plugin / "user.txt").read_text(), "keep")
        for name in agy.FILES:
            self.assertFalse((self.plugin / name).exists())
            self.assertEqual(next(self.plugin.glob(name + ".pair-desk-backup-*")).read_bytes(), files[name])
        self.assertEqual(self.antigravity("--uninstall")[0], 0)
        self.assertEqual(self.antigravity()[0], 0, "uninstall leftovers must not prevent reinstallation")

    def test_antigravity_update_preserves_other_metadata(self):
        self.antigravity()
        path = self.plugin / "mcp_config.json"
        value = json.loads(path.read_text())
        value["mcpServers"]["user-server"] = {"command": "custom"}
        path.write_text(json.dumps(value))
        hooks_path = self.plugin / "hooks.json"
        hooks = json.loads(hooks_path.read_text())
        hooks["user-hook"] = {"Stop": []}
        hooks["pair-desk"]["enabled"] = False
        hooks_path.write_text(json.dumps(hooks))
        self.assertEqual(self.antigravity()[0], 0)
        self.assertEqual(json.loads(path.read_text())["mcpServers"]["user-server"], {"command": "custom"})
        hooks = json.loads(hooks_path.read_text())
        self.assertEqual(hooks["user-hook"], {"Stop": []})
        self.assertFalse(hooks["pair-desk"]["enabled"])

    def test_foreign_antigravity_plugin_is_never_touched(self):
        self.plugin.mkdir(parents=True)
        manifest = self.plugin / "plugin.json"
        manifest.write_text('{"name":"mine"}')
        for extra in ((), ("--uninstall",), ("--dry-run",)):
            self.assertEqual(self.antigravity(*extra)[0], 1)
            self.assertEqual(manifest.read_text(), '{"name":"mine"}')

    def test_antigravity_foreign_server_is_rejected_before_any_write(self):
        self.antigravity()
        path = self.plugin / "mcp_config.json"
        value = json.loads(path.read_text())
        value["mcpServers"]["pair-desk"] = {"command": "foreign"}
        path.write_text(json.dumps(value))
        before = {p: p.read_bytes() for p in self.plugin.rglob("*") if p.is_file()}
        self.assertEqual(self.antigravity()[0], 1)
        self.assertEqual({p: p.read_bytes() for p in self.plugin.rglob("*") if p.is_file()}, before)

    def test_antigravity_update_preserves_sibling_handlers_and_server_options(self):
        self.antigravity()
        path = self.plugin / "hooks.json"
        value = json.loads(path.read_text())
        custom = {"type": "command", "command": "echo custom"}
        value["pair-desk"]["SessionStart"].append(custom)
        path.write_text(json.dumps(value))
        server_path = self.plugin / "mcp_config.json"
        config = json.loads(server_path.read_text())
        config["mcpServers"]["pair-desk"]["disabled"] = True
        config["mcpServers"]["pair-desk"]["env"]["CUSTOM"] = "preserved"
        server_path.write_text(json.dumps(config))
        for _ in range(2):
            self.assertEqual(self.antigravity()[0], 0)
            handlers = json.loads(path.read_text())["pair-desk"]["SessionStart"]
            self.assertEqual(len(handlers), 2)
            self.assertIn(custom, handlers)
        server = json.loads(server_path.read_text())["mcpServers"]["pair-desk"]
        self.assertTrue(server["disabled"])
        self.assertEqual(server["env"]["CUSTOM"], "preserved")

    def test_muse_preserves_user_handler_appended_to_managed_group(self):
        self.muse()
        value = json.loads(self.config.read_text())
        group = value["hooks"]["SessionStart"][0]
        custom = {"type": "command", "command": "echo custom"}
        group["hooks"].append(custom)
        group["matcher"] = "startup"
        self.write_config(value)
        for _ in range(2):
            self.assertEqual(self.muse()[0], 0)
            groups = json.loads(self.config.read_text())["hooks"]["SessionStart"]
            self.assertEqual(groups[0], {"matcher": "startup", "hooks": [custom]})
            self.assertEqual(len(groups), 2)
        self.assertEqual(self.muse("--uninstall")[0], 0)
        self.assertEqual(json.loads(self.config.read_text())["hooks"]["SessionStart"],
                         [{"matcher": "startup", "hooks": [custom]}])

    def test_dry_runs_and_absent_uninstalls_write_nothing(self):
        for fn in (self.antigravity, self.muse):
            self.assertEqual(fn("--dry-run")[0], 0)
            self.assertEqual(fn("--uninstall")[0], 0)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_muse_merge_backup_idempotency_and_shared_skills(self):
        original = {"schema_version": 1, "model": "user-model", "mcp_servers": {"other": {"command": "custom"}},
                    "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo user"}]}],
                              "Stop": [{"hooks": [{"command": "echo stop"}]}]}}
        self.write_config(original)
        original_bytes = self.config.read_bytes()
        report = common.Report()
        common.install_skills(self.skills, "codex", report)
        code, out = self.muse()
        self.assertEqual(code, 0, out)
        value = json.loads(self.config.read_text())
        self.assertEqual(value["model"], "user-model")
        self.assertEqual(value["mcp_servers"]["other"], original["mcp_servers"]["other"])
        self.assertEqual(value["mcp_servers"]["pair-desk"]["framing"], "jsonl")
        self.assertEqual(value["hooks"]["Stop"], original["hooks"]["Stop"])
        self.assertEqual(len(value["hooks"]["SessionStart"]), 2)
        self.assertTrue(native.managed_hook(value["hooks"]["SessionStart"][1], "muse"))
        self.assertEqual(next(self.config_dir.glob("settings.json.pair-desk-backup-*")).read_bytes(), original_bytes)
        first = self.config.read_bytes()
        self.assertIn("Nothing to change", self.muse()[1])
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual(len(list(self.config_dir.glob("settings.json.pair-desk-backup-*"))), 1)
        self.assertEqual(self.muse("--uninstall")[0], 0)
        self.assertEqual(json.loads(self.config.read_text()), original)
        marker = json.loads((self.skills / "pair-desk" / common.MARKER).read_text())
        self.assertEqual(marker["installed_by"], ["codex"])

    def test_muse_accepts_camelcase_and_preserves_bom_jsonc_backup(self):
        self.config_dir.mkdir()
        self.config.write_text('\ufeff{ /* keep in backup */ "schema_version":1, "mcpServers":{}, }', encoding="utf-8")
        first = self.config.read_bytes()
        self.assertEqual(self.muse()[0], 0)
        self.assertIn("pair-desk", json.loads(self.config.read_text())["mcpServers"])
        self.assertEqual(next(self.config_dir.glob("*.pair-desk-backup-*")).read_bytes(), first)

    def test_malformed_or_foreign_muse_settings_fail_before_any_write(self):
        for value in ([], {"hooks": []}, {"hooks": {"SessionStart": {}}}, {"mcp_servers": []},
                      {"schema_version": 2}, {"mcp_servers": {}, "mcpServers": {}},
                      {"mcp_servers": {"pair-desk": {"command": "foreign"}}},
                      {"mcp_servers": {"pair-desk": {"env": []}}}):
            with self.subTest(value=value):
                self.write_config(value)
                first = self.config.read_bytes()
                self.assertEqual(self.muse()[0], 1)
                self.assertEqual(self.config.read_bytes(), first)
                self.assertFalse(self.skills.exists())
                self.assertFalse(list(self.config_dir.glob("*.pair-desk-backup-*")))

    def test_changed_python_updates_one_hook_per_event(self):
        self.muse()
        old = json.loads(self.config.read_text())
        new = muse.merged_settings(old, ["different-python", "/different/desk.py"])
        for event in muse.EVENTS:
            self.assertEqual(len(new["hooks"][event]), 1)
        self.assertEqual(new["mcp_servers"]["pair-desk"]["command"], "different-python")
        self.assertNotEqual(old, new)

    def test_linked_config_is_rejected(self):
        target = self.tmp / "foreign.json"
        target.write_text("{}")
        self.config_dir.mkdir()
        try:
            self.config.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.assertEqual(self.muse()[0], 1)
        self.assertEqual(target.read_text(), "{}")


class NativeHookTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.data = self.tmp / "data"
        with Store(self.data) as store:
            store.create_project("game", "Game", "MG")
            store.create_issue("game", {"title": "new report", "source": "owner"})
            store.set_handoff("game", "## Next step\n\nCheck the build\n", author="codex")
        save_link(self.data, self.repo, "game")

    def hook(self, event, client, session="s1"):
        payload = ({"workspacePaths": [str(self.repo)], "conversationId": session} if client == "antigravity"
                   else {"cwd": str(self.repo), "session_id": session})
        args = argparse.Namespace(event=event, client=client)
        with patch.object(cli.sys, "stdin", io.StringIO(json.dumps(payload))), patch.object(cli, "ensure_server"):
            code, out = quiet(cli.cmd_hook, args, self.data)
        self.assertEqual(code, 0)
        return json.loads(out) if out else None

    def test_native_start_and_owner_updates_are_delivered_once_per_session(self):
        for client in ("antigravity", "muse"):
            value = self.hook("session-start", client)
            context = (value["injectSteps"][0]["systemMessage"]["systemMessage"] if client == "antigravity"
                       else value["hookSpecificOutput"]["additionalContext"])
            self.assertIn("1 new report", context)
            self.assertIn("Check the build", context)
        with Store(self.data) as store:
            store.add_comment("MG-1", "owner", "Please fix it")
        for client in ("antigravity", "muse"):
            value = self.hook("user-prompt-submit", client)
            self.assertIn("Please fix it", json.dumps(value))
            self.assertIsNone(self.hook("user-prompt-submit", client))

    def test_antigravity_without_workspace_is_silent(self):
        with patch.object(cli.sys, "stdin", io.StringIO('{"conversationId":"s1"}')):
            code, out = quiet(cli.cmd_hook, argparse.Namespace(event="session-start", client="antigravity"), self.data)
        self.assertEqual((code, out), (0, ""))

    def test_literal_native_shell_command_executes_with_spaces_quotes_and_unicode(self):
        script = self.tmp / "test ' unicode \u65e5.py"
        script.write_text('import json,sys\nprint(json.dumps({"argv":sys.argv[1:],"input":json.load(sys.stdin)}))\n')
        command = native.hook_command([sys.executable, str(script)], "session-start", "muse")
        shell = ["powershell.exe", "-NoProfile", "-Command", command] if os.name == "nt" else ["sh", "-c", command]
        result = subprocess.run(shell, input='{"test":"value"}', capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["input"], {"test": "value"})
        self.assertEqual(value["argv"], ["hook", "session-start", "--client", "muse"])


class NativeDiscoveryTest(unittest.TestCase):
    def test_only_standard_macos_system_alias_targets_are_allowed(self):
        with patch.object(native.sys, "platform", "darwin"):
            for name in ("var", "tmp", "etc"):
                for target in (f"/private/{name}", f"private/{name}"):
                    with patch.object(native.os, "readlink", return_value=target):
                        self.assertTrue(native.darwin_system_alias(PurePosixPath("/" + name)))
                with patch.object(native.os, "readlink", return_value="/custom"):
                    self.assertFalse(native.darwin_system_alias(PurePosixPath("/" + name)))
            with patch.object(native.os, "readlink", return_value="/private/var"):
                self.assertFalse(native.darwin_system_alias(PurePosixPath("/user-link")))
        with patch.object(native.sys, "platform", "linux"):
            self.assertFalse(native.darwin_system_alias(PurePosixPath("/var")))

    def test_dry_run_does_not_chmod_the_posix_launcher(self):
        with patch.object(common, "ensure_executable") as chmod:
            common.desk_command(common.DESK_PY, windows=False, dry_run=True)
        chmod.assert_not_called()

    def test_posix_hooks_quote_shell_metacharacters(self):
        import shlex
        prefix = ["/tmp/has space/python", "/tmp/$(must-not-run);'quote/desk.py"]
        command = native.hook_command(prefix, "session-start", "muse", windows=False)
        self.assertEqual(shlex.split(command, comments=True), [*prefix, "hook", "session-start", "--client", "muse"])

    def test_names_and_plans_include_both_tools(self):
        self.assertIn("antigravity", installer.TOOLS)
        self.assertIn("muse", installer.TOOLS)
        self.assertEqual(installer.BINARIES["antigravity"], "agy")
        for tool in ("antigravity", "muse"):
            ctx = installer.Context(source=ROOT, app=ROOT)
            for action in installer.ACTIONS:
                steps = installer.plan_for(tool, action, ctx)
                self.assertEqual(steps[0].tool, tool)
                self.assertEqual(Path(steps[0].argv[0]), ROOT / "integrations" / tool / "install.py")


if __name__ == "__main__":
    unittest.main()
