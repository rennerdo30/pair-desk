"""opencode installer: JSONC config merge, backups, idempotency, uninstall, the plugin shim, and
the plugin itself under node (skipped when node is not installed)."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from helpers import TempDirCase  # also puts the repo root on sys.path

from integrations import common
from integrations.opencode import install as opencode
from pair_desk.context import save_link
from pair_desk.store import Store

JSONC = """{
  // global opencode config
  "$schema": "https://opencode.ai/config.json",
  "plugin": [
    "opencode-goal-plugin@0.10.0", /* pinned */
  ],
  "mcp": {
    "other": {"type": "remote", "url": "https://example.invalid/mcp?a=1//not-a-comment"},
  },
}
"""


def quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        code = fn(*args)
    return code, out.getvalue()


class JsoncTest(unittest.TestCase):
    def test_strip_comments_and_trailing_commas_outside_strings(self):
        clean, had = common.strip_jsonc(JSONC)
        self.assertTrue(had)
        data = json.loads(clean)
        self.assertEqual(data["plugin"], ["opencode-goal-plugin@0.10.0"])
        self.assertEqual(data["mcp"]["other"]["url"], "https://example.invalid/mcp?a=1//not-a-comment")
        self.assertEqual(common.strip_jsonc('{"a": "x, }"}')[0], '{"a": "x, }"}')
        self.assertFalse(common.strip_jsonc('{"a": "/* no */"}')[1])


class OpencodeInstallTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.dir = self.tmp / "opencode"
        self.dir.mkdir()
        self.skills = self.tmp / "skills"
        self.config = self.dir / "opencode.jsonc"
        self.config.write_text(JSONC, encoding="utf-8", newline="")
        self.desk = common.fwd(common.DESK_PY)

    def install(self, *extra):
        return quiet(opencode.run, ["--config-dir", str(self.dir), "--skills-dir", str(self.skills), *extra])

    def backups(self):
        return sorted(self.dir.glob("opencode.jsonc.pair-desk-backup-*"))

    def load(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def test_install_merges_and_keeps_other_entries(self):
        code, out = self.install()
        self.assertEqual(code, 0)
        self.assertIn("drops them", out)
        data = self.load()
        self.assertEqual(data["mcp"]["pair-desk"], {"type": "local", "enabled": True,
                                                    "command": [*common.desk_command(common.DESK_PY), "mcp"]})
        self.assertEqual(data["mcp"]["other"]["type"], "remote")
        self.assertEqual(data["plugin"], ["opencode-goal-plugin@0.10.0"])
        self.assertEqual(data["$schema"], "https://opencode.ai/config.json")
        backups = self.backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), JSONC, "the backup keeps the comments")

    def test_idempotent(self):
        self.install()
        first = self.config.read_bytes()
        shim = (self.dir / "plugins" / "pair-desk.js").read_bytes()
        code, out = self.install()
        self.assertEqual(code, 0)
        self.assertIn("Nothing to change.", out)
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual((self.dir / "plugins" / "pair-desk.js").read_bytes(), shim)
        self.assertEqual(len(self.backups()), 1)

    def test_uninstall_removes_only_pair_desk(self):
        self.install()
        code, _ = self.install("--uninstall")
        self.assertEqual(code, 0)
        data = self.load()
        self.assertEqual(list(data["mcp"]), ["other"])
        self.assertEqual(data["plugin"], ["opencode-goal-plugin@0.10.0"])
        self.assertFalse((self.dir / "plugins" / "pair-desk.js").exists())
        self.assertFalse((self.skills / "pair-desk").exists())
        self.assertEqual(len(self.backups()), 2)
        _, out = self.install("--uninstall")
        self.assertIn("Nothing to change.", out)
        self.assertEqual(len(self.backups()), 2)

    def test_uninstall_drops_an_mcp_section_it_emptied(self):
        (self.dir / "opencode.jsonc").unlink()
        self.install()
        self.assertEqual(self.dir / "opencode.json", opencode.config_file(self.dir))
        self.install("--uninstall")
        self.assertEqual(json.loads((self.dir / "opencode.json").read_text(encoding="utf-8")),
                         {"$schema": "https://opencode.ai/config.json"})

    def test_dry_run_writes_nothing(self):
        code, out = self.install("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would write plugin", out)
        self.assertEqual(self.config.read_text(encoding="utf-8"), JSONC)
        self.assertEqual(self.backups(), [])
        self.assertFalse((self.dir / "plugins").exists())
        self.assertFalse(self.skills.exists())

    def test_foreign_plugin_file_is_left_alone(self):
        plugins = self.dir / "plugins"
        plugins.mkdir()
        (plugins / "pair-desk.js").write_text("export const Mine = async () => ({})\n", encoding="utf-8")
        self.install()
        self.assertIn("Mine", (plugins / "pair-desk.js").read_text(encoding="utf-8"))
        self.install("--uninstall")
        self.assertTrue((plugins / "pair-desk.js").exists())

    def test_broken_config_is_left_alone(self):
        self.config.write_text('{"mcp": ', encoding="utf-8")
        code, _ = self.install()
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_text(encoding="utf-8"), '{"mcp": ')
        self.assertEqual(self.backups(), [])

    def test_shim_reexports_only_the_plugin(self):
        text = opencode.shim_text()
        exports = [line for line in text.splitlines() if line.startswith("export")]
        self.assertEqual(len(exports), 1)
        self.assertIn("{ PairDeskPlugin }", exports[0])
        self.assertIn(opencode.PLUGIN_SRC.as_uri(), exports[0])


NODE = shutil.which("node")

PLUGIN_PROBE = r"""
import { pathToFileURL } from "node:url"
const mod = await import(pathToFileURL(process.argv[2]).href)
const [linked, unlinked] = [process.argv[3], process.argv[4]]
const toasts = []
const client = { tui: { showToast: async (req) => toasts.push(req.body) } }
const hooks = await mod.PairDeskPlugin({ client, directory: unlinked })
await hooks.event({ event: { type: "session.created", properties: { info: { id: "s1", directory: linked } } } })
const sys1 = { system: ["base"] }
await hooks["experimental.chat.system.transform"]({ sessionID: "s1" }, sys1)
await hooks["experimental.chat.system.transform"]({ sessionID: "s1" }, sys1)
const sys2 = { system: ["base"] }
await hooks["experimental.chat.system.transform"]({ sessionID: "s2" }, sys2)
console.log(JSON.stringify({ toasts, sys1: sys1.system, sys2: sys2.system }))
"""


@unittest.skipUnless(NODE, "node is not installed")
class PluginUnderNodeTest(TempDirCase):
    def test_summary_toast_and_system_block_only_in_linked_folders(self):
        data = self.tmp / "data"
        linked, unlinked = self.tmp / "game", self.tmp / "elsewhere"
        linked.mkdir()
        unlinked.mkdir()
        with Store(data) as store:
            store.create_project("mygame", "MyGame", "MG")
            store.create_issue("mygame", {"title": "Rock at the harbor", "source": "owner"})
        save_link(data, linked, "mygame")
        probe = self.tmp / "probe.mjs"
        probe.write_text(PLUGIN_PROBE, encoding="utf-8")
        env = dict(os.environ, PAIR_DESK_DATA=str(data))
        run = subprocess.run([NODE, str(probe), str(opencode.PLUGIN_SRC), str(linked), str(unlinked)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(len(out["toasts"]), 1)
        self.assertEqual(out["toasts"][0]["title"], "Pair Desk")
        self.assertIn("Pair Desk (MyGame): 1 new report", out["toasts"][0]["message"])
        self.assertEqual(len(out["sys1"]), 2, "the block is added once per request, not twice")
        self.assertIn("<pair-desk>", out["sys1"][1])
        self.assertIn("Never mark anything passed", out["sys1"][1])
        self.assertIn("1 new report", out["sys1"][1])
        self.assertEqual(out["sys2"], ["base"], "a session in an unlinked folder gets nothing")
        self.assertIn("At session start read failed checks", out["sys1"][1])
        # an agent-bridge delegated job keeps to its brief
        run = subprocess.run([NODE, str(probe), str(opencode.PLUGIN_SRC), str(linked), str(unlinked)],
                             capture_output=True, text=True, encoding="utf-8", timeout=60,
                             env=dict(env, AGENT_BRIDGE_JOB_ID="opencode-job-1"))
        self.assertEqual(run.returncode, 0, run.stderr)
        block = json.loads(run.stdout.strip().splitlines()[-1])["sys1"][1]
        self.assertIn("You are a delegated agent-bridge job", block)
        self.assertIn("delegated agent-bridge job (opencode-job-1)", block)
        self.assertNotIn("At session start read failed checks", block)


if __name__ == "__main__":
    unittest.main()
