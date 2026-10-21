"""Git transport acceptance checks, using only disposable local repositories."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pair_desk.sync_git import GitBranch, PushRejected, SyncError


IDENTITY = {
    "GIT_AUTHOR_NAME": "Synthetic Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Synthetic Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


class GitBranchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pairdesk-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.git("init", "--bare", "--template=", str(self.remote))
        self.first = self.transport("first")
        self.second = self.transport("second")
        self.files = {".pair-desk/manifest.json": b'{"version":1}',
                      ".pair-desk/state.json": b'{"issues":[]}'}

    def git(self, *args, data=None, cwd=None, env=None):
        clean = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        clean.update(IDENTITY)
        clean.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        clean.update(env or {})
        # run() owns, waits for, and kills only its own child on timeout.
        result = subprocess.run(["git", *args], input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd or self.root, env=clean,
                                timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.strip()

    def transport(self, name, **kwargs):
        return GitBranch(self.root / name, str(self.remote), author_name="Synthetic Test",
                         author_email="test@example.invalid", **kwargs)

    def test_initial_roundtrip_and_independent_descendant(self):
        self.assertEqual(self.first.read(), (None, {}))
        one = self.first.publish(self.files, None)
        self.assertEqual(self.second.read(), (one, self.files))
        changed = {**self.files, ".pair-desk/blobs/" + "a" * 64: b"binary\x00\xff"}
        two = self.second.publish(changed, one)
        self.assertNotEqual(one, two)
        self.assertEqual(self.first.read(), (two, changed))
        self.assertEqual(self.git("--git-dir", str(self.remote), "rev-parse", two + "^"), one.encode())
        self.assertEqual(self.git("--git-dir", str(self.remote), "log", "-1", "--format=%an <%ae>", two),
                         b"Synthetic Test <test@example.invalid>")

    def test_head_only_queries_ref_without_fetch_or_blob_reads(self):
        tip = "a" * 40

        def stub(repo, args, **kwargs):
            if args[0] == "init":
                return b""
            if args[:2] == ["ls-remote", "--get-url"]:
                return os.fsencode(self.first.remote) + b"\n"
            self.assertEqual(args, ["ls-remote", "--exit-code", "--refs", "--", self.first.remote,
                                    "refs/heads/pair-desk-data"])
            self.assertEqual(kwargs["limit"], 4096)
            return tip.encode() + b"\trefs/heads/pair-desk-data\n"

        with mock.patch.object(self.first, "_run", side_effect=stub) as run:
            self.assertEqual(self.first.head(), tip)
            self.assertEqual(run.call_count, 3)
        self.assertEqual(self.first.head(), None)
        actual = self.first.publish(self.files, None)
        self.assertEqual(self.second.head(), actual)

    def test_noop_and_delete_files(self):
        one = self.first.publish(self.files, None)
        self.assertEqual(self.second.publish(dict(self.files), one), one)
        two = self.first.publish({}, one)
        self.assertEqual(self.second.read(), (two, {}))
        self.assertEqual(self.second.publish({}, two), two)

    def test_stale_parent_rejected_including_noop_and_initial(self):
        one = self.first.publish(self.files, None)
        self.second.read()
        changed = {**self.files, ".pair-desk/state.json": b"updated"}
        two = self.first.publish(changed, one)
        for files, parent in ((self.files, one), (changed, one), (self.files, None)):
            with self.subTest(parent=parent), self.assertRaises(PushRejected):
                self.second.publish(files, parent)
        self.assertEqual(self.first.read(), (two, changed))

    def test_detectable_administrative_rewind_or_deletion_rejects_pending_changes(self):
        one = self.first.publish(self.files, None)
        two = self.first.publish({".pair-desk/state.json": b"second"}, one)
        pending = {".pair-desk/state.json": b"pending"}
        self.git("--git-dir", str(self.remote), "update-ref", "refs/heads/pair-desk-data", one, two)
        with self.assertRaises(PushRejected):
            self.second.publish(pending, two)
        self.assertEqual(self.first.read(), (one, self.files))
        self.git("--git-dir", str(self.remote), "update-ref", "-d", "refs/heads/pair-desk-data", one)
        with self.assertRaises(PushRejected):
            self.second.publish(pending, two)
        self.assertEqual(pending, {".pair-desk/state.json": b"pending"})
        self.assertEqual(self.first.read(), (None, {}))

    def test_reads_only_exact_ref_and_never_default_head_or_similar_branch(self):
        other = self.transport("other", branch="unrelated")
        other.publish({".pair-desk/state.json": b"other"}, None)
        self.assertEqual(self.first.read(), (None, {}))
        tip = self.first.publish(self.files, None)
        self.git("--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/unrelated")
        self.assertEqual(self.first.read(), (tip, self.files))

    def test_push_race_rejected_without_merging(self):
        one = self.first.publish(self.files, None)
        original = self.second._run
        winner = []

        def race(repo, args, **kwargs):
            if args[0] == "push":
                winner.append(self.first.publish({".pair-desk/state.json": b"winner"}, one))
            return original(repo, args, **kwargs)

        with mock.patch.object(self.second, "_run", side_effect=race):
            with self.assertRaises(PushRejected):
                self.second.publish({".pair-desk/state.json": b"loser"}, one)
        self.assertEqual(self.first.read(), (winner[0], {".pair-desk/state.json": b"winner"}))

    def test_receive_side_race_reported_as_remote_rejection_is_still_push_rejected(self):
        from pair_desk import sync_git
        one = self.first.publish(self.files, None)
        original = self.second._run
        winner = []

        def race(repo, args, **kwargs):
            if args[0] == "push":
                winner.append(self.first.publish({".pair-desk/state.json": b"winner"}, one))
                raise sync_git._GitFailure(1)  # server lost ref-lock after advertisement
            return original(repo, args, **kwargs)

        with mock.patch.object(self.second, "_run", side_effect=race):
            with self.assertRaises(PushRejected):
                self.second.publish({".pair-desk/state.json": b"loser"}, one)
        self.assertEqual(self.first.head(), winner[0])

    def test_invalid_input_paths_and_types_are_rejected(self):
        for name in ("README.md", "../state.json", "/.pair-desk/state.json", ".pair-desk/../state.json",
                     ".pair-desk\\state.json", ".pair-desk/blobs/" + "A" * 64,
                     ".pair-desk/blobs/" + "a" * 63, ".pair-desk/state.json\x00suffix",
                     ".pair-desk/.git/config", ".pair-desk/state.json/child"):
            with self.subTest(name=name), self.assertRaises(SyncError):
                self.first.publish({name: b"bad"}, None)
        with self.assertRaises(SyncError):
            self.first.publish({".pair-desk/state.json": "not bytes"}, None)
        for parent in ("HEAD", "-p", "a" * 39, "a" * 40 + "^{tree}"):
            with self.subTest(parent=parent), self.assertRaises(SyncError):
                self.first.publish(self.files, parent)

    def forge(self, path, mode="100644"):
        repo = self.root / "forger.git"
        self.git("init", "--bare", "--template=", str(repo))
        prefix = ("--git-dir", str(repo))
        obj = self.git(*prefix, "hash-object", "-w", "--stdin", data=b"payload").decode()
        if mode == "160000":
            empty = self.git(*prefix, "mktree", data=b"").decode()
            obj = self.git(*prefix, "commit-tree", empty, data=b"test\n").decode()
        index = {"GIT_INDEX_FILE": str(self.root / "forge.index")}
        self.git(*prefix, "read-tree", "--empty", env=index)
        self.git(*prefix, "update-index", "--add", "--cacheinfo", f"{mode},{obj},{path}", env=index)
        tree = self.git(*prefix, "write-tree", env=index).decode()
        commit = self.git(*prefix, "commit-tree", tree, data=b"forged\n").decode()
        self.git(*prefix, "push", str(self.remote), commit + ":refs/heads/pair-desk-data")

    def test_remote_arbitrary_paths_symlinks_executable_and_submodule_rejected(self):
        for path, mode in (("README.md", "100644"), (".pair-desk/state.json", "120000"),
                           (".pair-desk/state.json", "100755"), (".pair-desk/state.json", "160000")):
            with self.subTest(path=path, mode=mode):
                # Each case has its own synthetic remote; no forced pushes.
                self.remote = self.root / ("remote-" + mode + path.replace("/", "_") + ".git")
                self.git("init", "--bare", "--template=", str(self.remote))
                self.forge(path, mode)
                with self.assertRaises(SyncError):
                    self.transport("reader-" + mode).read()

    def test_bounds_on_publish_and_remote_tree(self):
        from pair_desk import sync_git
        with mock.patch.object(sync_git, "MAX_FILES", 1):
            with self.assertRaises(SyncError):
                self.first.publish(self.files, None)
        with mock.patch.object(sync_git, "MAX_BYTES", 2):
            with self.assertRaises(SyncError):
                self.first.publish(self.files, None)
        self.first.publish(self.files, None)
        with mock.patch.object(sync_git, "MAX_FILES", 1):
            with self.assertRaises(SyncError):
                self.second.read()
        with mock.patch.object(sync_git, "MAX_BYTES", 2):
            with self.assertRaises(SyncError):
                self.second.read()

    def test_offline_error_does_not_expose_remote(self):
        transport = GitBranch(self.root / "offline", str(self.root / "private-credential-path.git"),
                              author_name="Test", author_email="test@example.invalid")
        with self.assertRaises(SyncError) as caught:
            transport.read()
        self.assertNotIn("private-credential", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_remote_and_branch_validation(self):
        for remote in ("-oops", "ext::echo bad", "file:///tmp/remote", "http://host/repo",
                       "https://user:secret@host/repo", "https://host/repo?token=secret",
                       "ssh://-host/repo", "git@-host:repo", "git@host:-repo", "https://host/repo#secret",
                       "https://host/repo?", "https://host/repo#", "host\npath"):
            with self.subTest(remote=remote), self.assertRaises(SyncError) as caught:
                GitBranch(self.root / "bad", remote, author_name="Test", author_email="test@example.invalid")
            self.assertNotIn("secret", str(caught.exception))
        for branch in ("-bad", "HEAD", "a..b", "a@{b", "a.lock", "a b", "refs/heads/b", "a\nb"):
            with self.subTest(branch=branch), self.assertRaises(SyncError):
                self.transport("bad", branch=branch)
        for remote in ("https://example.invalid/desk.git", "ssh://git@example.invalid/desk.git",
                       "git@example.invalid:desk.git", "ssh://git@[::1]:2222/desk.git"):
            with self.subTest(allowed=remote):
                GitBranch(self.root / "validated-only", remote, author_name="Test", author_email="test@example.invalid")

    def fake_home(self, config):
        home = self.root / "synthetic-home"
        home.mkdir(exist_ok=True)
        (home / ".gitconfig").write_text(config, encoding="utf-8")
        return {"HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / "xdg")}

    def test_global_hook_filter_fsmonitor_signing_and_managed_cache_config_not_executed(self):
        hooks = self.root / "hooks"
        hooks.mkdir()
        marker = self.root / "executed"
        script = "#!/bin/sh\nprintf unsafe > '" + marker.as_posix() + "'\nexit 1\n"
        for name in ("pre-push", "post-commit", "pre-commit", "fsmonitor"):
            (hooks / name).write_text(script, encoding="utf-8")
            (hooks / name).chmod(0o700)
        config = ('[core]\n hooksPath = "' + hooks.as_posix() + '"\n fsmonitor = "' +
                  (hooks / "fsmonitor").as_posix() + '"\n[commit]\n gpgSign = true\n' +
                  '[gpg]\n program = never-execute-gpg\n[filter "bad"]\n clean = never-execute-clean\n' +
                  ' smudge = never-execute-smudge\n required = true\n')
        self.first.cache_dir.mkdir()
        # An existing caller/cache config is never opened as a Git repository.
        (self.first.cache_dir / "config").write_text(config, encoding="utf-8")
        with mock.patch.dict(os.environ, self.fake_home(config)):
            tip = self.first.publish(self.files, None)
            self.assertEqual(self.second.read(), (tip, self.files))
        self.assertFalse(marker.exists())
        self.assertEqual((self.first.cache_dir / "config").read_text(encoding="utf-8"), config)
        self.assertEqual(list(self.first.cache_dir.glob("transport-*")), [])

    def test_global_remote_rewrite_is_rejected_without_execution_or_address_leak(self):
        address = self.first.remote.replace("\\", "\\\\").replace('"', '\\"')
        config = '[url "ext::never-execute-private-secret"]\n insteadOf = "' + address + '"\n'
        with mock.patch.dict(os.environ, self.fake_home(config)):
            with self.assertRaises(SyncError) as caught:
                self.first.read()
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn(str(self.remote), str(caught.exception))

    def test_timeout_and_output_limits_stop_only_the_owned_process(self):
        from pair_desk import sync_git
        child = mock.Mock(pid=12345, returncode=None)
        child.poll.return_value = None
        with mock.patch.object(sync_git.subprocess, "Popen", return_value=child), \
                mock.patch.object(sync_git.time, "monotonic", side_effect=[0, 30]), \
                mock.patch.object(sync_git, "_stop") as stop:
            with self.assertRaisesRegex(SyncError, "timed out"):
                self.first._run(self.root / "fake.git", ["ls-remote", "--", self.first.remote])
            stop.assert_called_once_with(child)

        def output_overflow(*args, **kwargs):
            kwargs["stdout"].write(b"too much output")
            kwargs["stdout"].flush()
            return child

        with mock.patch.object(sync_git.subprocess, "Popen", side_effect=output_overflow), \
                mock.patch.object(sync_git, "_stop") as stop:
            with self.assertRaisesRegex(SyncError, "limits"):
                self.first._run(self.root / "fake.git", ["ls-tree"], limit=1)
            stop.assert_called_once_with(child)

    def test_disk_pack_budget_rejects_and_cleans_managed_repository(self):
        from pair_desk import sync_git
        self.first.publish(self.files, None)
        with mock.patch.object(sync_git, "MAX_REPOSITORY_BYTES", 1):
            with self.assertRaisesRegex(SyncError, "limits"):
                self.second.read()
        self.assertEqual(list(self.second.cache_dir.glob("transport-*")), [])

    def test_pack_rename_during_disk_budget_scan_does_not_fail_transport(self):
        directory = str(self.root / "objects")
        with mock.patch("pair_desk.sync_git.os.walk", return_value=[(directory, [], ["temporary-pack"])]), \
                mock.patch.object(Path, "stat", side_effect=FileNotFoundError):
            self.first._check_disk(self.root)

    def test_empty_unexpected_tree_and_invalid_batch_reply_rejected(self):
        commit = "a" * 40
        unexpected = b"040000 tree " + commit.encode() + b"       -\tunexpected\0"
        with mock.patch.object(self.first, "_run", side_effect=[commit.encode(), b"100", unexpected]):
            with self.assertRaisesRegex(SyncError, "unsupported"):
                self.first._files(self.root / "unused", commit)
        listing = b"100644 blob " + commit.encode() + b"       2\t.pair-desk/state.json\0"
        for reply in (b"missing\n", commit.encode() + b" blob 2\na\n", commit.encode() + b" blob 2\nab\nextra"):
            with self.subTest(reply=reply), mock.patch.object(self.first, "_run",
                                                           side_effect=[commit.encode(), b"100", listing, reply]):
                with self.assertRaises(SyncError):
                    self.first._files(self.root / "unused", commit)

    def test_oversize_tree_is_rejected_before_tree_expansion(self):
        from pair_desk import sync_git
        commit = "a" * 40
        with mock.patch.object(self.first, "_run", side_effect=[commit.encode(), str(sync_git.MAX_TREE_BYTES + 1).encode()]) as run:
            with self.assertRaisesRegex(SyncError, "limits"):
                self.first._files(self.root / "unused", commit)
        self.assertEqual([call.args[1][0] for call in run.call_args_list], ["rev-parse", "cat-file"])

    def test_dirty_staged_checkout_and_inherited_git_overrides_untouched(self):
        code = self.root / "code"
        self.git("init", "--template=", str(code))
        (code / "file.txt").write_bytes(b"base\n")
        self.git("add", "file.txt", cwd=code)
        self.git("commit", "-m", "synthetic base", cwd=code)
        (code / "file.txt").write_bytes(b"staged\n")
        self.git("add", "file.txt", cwd=code)
        (code / "file.txt").write_bytes(b"dirty\n")
        (code / "untracked.txt").write_bytes(b"untracked\n")
        before = {p.relative_to(code): p.read_bytes() for p in code.rglob("*") if p.is_file()}
        poison = {"GIT_DIR": str(code / ".git"), "GIT_WORK_TREE": str(code),
                  "GIT_INDEX_FILE": str(code / ".git/index"), "GIT_CONFIG_COUNT": "1",
                  "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(code),
                  "GIT_OBJECT_DIRECTORY": str(code / ".git/objects"), "GIT_SSH_COMMAND": "not-a-command",
                  "GIT_CONFIG_PARAMETERS": "'core.bare=false'", "GIT_TRACE": str(code / "trace"),
                  "GIT_EXEC_PATH": str(code), "GIT_CONFIG": str(code / "config"),
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(code), "GIT_PROXY_COMMAND": "never-execute"}
        with mock.patch.dict(os.environ, poison):
            one = self.first.publish(self.files, None)
            self.assertEqual(self.second.read(), (one, self.files))
        after = {p.relative_to(code): p.read_bytes() for p in code.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
