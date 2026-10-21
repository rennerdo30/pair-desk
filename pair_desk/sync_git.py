"""Bounded, opaque snapshots on a dedicated Git data branch.

No checkout, domain interpretation, Git configuration writes, or conflict resolution.
Each operation owns a fresh bare repository below cache_dir and removes only that
temporary directory. Existing credential helpers and SSH authentication remain
available; repository content never supplies executable client configuration.
Trusted user HTTP proxy, certificate and credential-helper settings are retained.
SSH uses the standard ssh command and its normal user config/agent; custom
core.sshCommand and inherited GIT_SSH_COMMAND overrides are unsupported.

Writers must advance this branch normally. Administrative rewrites/deletion are
unsupported: a detectable parent mismatch (including deletion) rejects publication,
but a normal push cannot atomically guard against an administrative rewind during
the final push handshake. There is deliberately no force or force-with-lease.
"""

from __future__ import annotations

import contextlib
import io
import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


MAX_BYTES = 128 * 1024 * 1024
MAX_FILES = 10_000
# Includes packed objects/history overhead, not just the accepted snapshot.
MAX_REPOSITORY_BYTES = 256 * 1024 * 1024
MAX_TREE_BYTES = 4 * 1024 * 1024
_OID = re.compile(r"[0-9a-f]{40}")
_FILE = re.compile(r"\.pair-desk/(?:manifest\.json|state\.json|blobs/[0-9a-f]{64})")
_LOG = logging.getLogger(__name__)


class SyncError(Exception):
    """Transport failure, with no remote address or subprocess diagnostics."""


class PushRejected(SyncError):
    """The remote changed; the caller must explicitly resolve the race."""


class _GitFailure(SyncError):
    def __init__(self, code: int, rejected: bool = False):
        super().__init__("Git transport command failed.")
        self.code = code
        self.rejected = rejected


def _remote_address(remote: str) -> tuple[str, str]:
    """Validate before Git can interpret an option, helper, alias, or URL."""
    if (not isinstance(remote, str) or not remote or len(remote) > 8192 or remote.startswith(("-", " "))
            or any(ord(c) < 32 or ord(c) == 127 for c in remote)):
        raise SyncError("Invalid Git remote.")
    if "://" in remote:
        try:
            url = urlsplit(remote)
            host = url.hostname
            port = url.port
            if (url.scheme not in ("https", "ssh") or not host or host.startswith("-")
                    or not re.fullmatch(r"[A-Za-z0-9._:-]+", host)
                    or not url.path or "?" in remote or "#" in remote or url.password is not None
                    or any(c.isspace() for c in remote)
                    or (url.scheme == "https" and url.username is not None)
                    or (url.username is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", url.username))
                    or (port is not None and port < 1)):
                raise ValueError
            return remote, url.scheme
        except ValueError:
            raise SyncError("Invalid Git remote.") from None
    if remote.startswith("git@"):
        if not re.fullmatch(r"git@[A-Za-z0-9_][A-Za-z0-9_.-]*:[^\s:-][^\s]*", remote):
            raise SyncError("Invalid Git remote.")
        return remote, "ssh"
    # Only plain filesystem paths, never file://, remote helpers or other scp forms.
    if (":" in remote and not re.match(r"^[A-Za-z]:[/\\]", remote)) or "@" in remote:
        raise SyncError("Invalid Git remote.")
    return str(Path(remote).absolute()), "file"


def _branch_ref(branch: str) -> str:
    if (not isinstance(branch, str) or len(branch) > 1024 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
            or branch == "HEAD" or branch.startswith("refs/") or ".." in branch
            or any(not p or p.startswith(".") or p.endswith((".", ".lock")) for p in branch.split("/"))):
        raise SyncError("Invalid data branch.")
    return "refs/heads/" + branch


def _environment(name: str, email: str, index: Path) -> dict[str, str]:
    # Drop ALL inherited Git control, config, tracing, executable and object-store
    # overrides, including future GIT_CONFIG_KEY_n / VALUE_n additions.
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_INDEX_FILE=str(index), GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="",
               SSH_ASKPASS="", GCM_INTERACTIVE="never", GCM_GUI_PROMPT="0", LC_ALL="C",
               GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    return env


def _stop(proc: subprocess.Popen) -> None:
    """Kill only this recorded child and its descendants, then reap it."""
    if os.name == "nt":
        killer = None
        try:
            killer = subprocess.Popen(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            _LOG.debug("Git cleanup PID %s for owned PID %s", killer.pid, proc.pid)
            killer.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            if killer is not None:
                killer.kill()
                killer.wait(timeout=2)
        finally:
            if proc.poll() is None:
                proc.kill()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=2)


class GitBranch:
    """A cancellable data transport; timeout bounds each complete public call.

    Cancellation/deadline checks are cooperative between local file operations
    and during subprocess waits. Owned-process reaping and temporary-directory
    cleanup still run after cancellation or expiry, with bounded process waits.
    """

    def __init__(self, cache_dir: Path, remote: str, branch: str = "pair-desk-data", *,
                 author_name: str, author_email: str, timeout: float = 20,
                 cancel_event: threading.Event | None = None):
        self.remote, self.protocol = _remote_address(remote)
        self.ref = _branch_ref(branch)
        if any(not isinstance(v, str) or not v.strip() or any(ord(c) < 32 or c in "<>\x7f" for c in v)
               for v in (author_name, author_email)):
            raise SyncError("Invalid commit identity.")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise SyncError("Invalid Git timeout.")
        self.cache_dir = Path(cache_dir).absolute()
        self.author_name = author_name
        self.author_email = author_email
        self.timeout = timeout
        self.cancel_event = cancel_event
        self._operation_state = threading.local()

    def _remaining(self, command_deadline: float | None = None) -> float | None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise SyncError("Git transport cancelled.")
        deadline = getattr(self._operation_state, "deadline", None)
        if command_deadline is not None:
            deadline = min(deadline, command_deadline) if deadline is not None else command_deadline
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SyncError("Git transport timed out.")
        return remaining

    @contextlib.contextmanager
    def _operation(self):
        # Instance-local, thread-local state keeps concurrent callers' deadlines
        # independent. Nested calls cannot extend their enclosing operation.
        previous = getattr(self._operation_state, "deadline", None)
        deadline = time.monotonic() + self.timeout
        self._operation_state.deadline = min(previous, deadline) if previous is not None else deadline
        try:
            self._remaining()
            yield
            self._remaining()
        finally:
            if previous is None:
                del self._operation_state.deadline
            else:
                self._operation_state.deadline = previous

    def _options(self, repo: Path) -> list[str]:
        # These are invocation-local; do not write config or copy remote config.
        settings = {
            "core.hooksPath": str(repo.parent / "no-hooks"), "core.fsmonitor": "false",
            "core.sshCommand": "ssh -oBatchMode=yes -oPermitLocalCommand=no -oClearAllForwardings=yes",
            "ssh.variant": "ssh", "core.gitProxy": "", "core.askPass": "",
            "credential.interactive": "false", "protocol.allow": "never",
            f"protocol.{self.protocol}.allow": "always", "submodule.recurse": "false",
            "fetch.recurseSubmodules": "false", "fetch.fsckObjects": "true",
            "transfer.fsckObjects": "true", "receive.fsckObjects": "true",
            "fetch.unpackLimit": "1", "fetch.writeCommitGraph": "false",
            "gc.auto": "0", "maintenance.auto": "false", "commit.gpgSign": "false",
            "pack.threads": "1", "index.threads": "1", "core.deltaBaseCacheLimit": "16m",
            "http.followRedirects": "false", "http.sslVerify": "true", "http.maxRequests": "1",
        }
        return [arg for key, value in settings.items() for arg in ("-c", key + "=" + value)]

    @staticmethod
    def _check_disk(repo: Path) -> None:
        total = count = 0
        for directory, dirs, files in os.walk(repo / "objects", followlinks=False):
            count += len(dirs) + len(files)
            if count > MAX_FILES * 4 + 100:
                raise SyncError("Git repository exceeds transport limits.")
            for name in files:
                try:
                    total += (Path(directory) / name).stat().st_size
                except FileNotFoundError:
                    # index-pack atomically renames temporary packs while fetch
                    # runs. The next poll/final scan sees the completed filename.
                    continue
                if total > MAX_REPOSITORY_BYTES:
                    raise SyncError("Git repository exceeds transport limits.")

    def _run(self, repo: Path, args: list[str], *, data: bytes = b"", limit: int = MAX_TREE_BYTES) -> bytes:
        self._remaining()
        argv = ["git", "--no-replace-objects", "--literal-pathspecs", "--no-optional-locks",
                *self._options(repo), "--git-dir=" + str(repo), *args]
        env = _environment(self.author_name, self.author_email, repo.parent / "index")
        # Files avoid unbounded PIPE buffering and blocked stdin writes. Polling
        # bounds output and pack storage while Git runs, including hostile input.
        with tempfile.TemporaryFile(dir=repo.parent) as source, tempfile.TemporaryFile(dir=repo.parent) as output:
            source.write(data)
            source.seek(0)
            self._remaining()
            proc = subprocess.Popen(argv, cwd=repo.parent, env=env, stdin=source, stdout=output,
                                    stderr=subprocess.DEVNULL, close_fds=True,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    start_new_session=os.name != "nt")
            _LOG.debug("Git transport %s started owned PID %s", args[0], proc.pid)
            try:
                deadline = time.monotonic() + self.timeout
                while True:
                    remaining = self._remaining(deadline)
                    if os.fstat(output.fileno()).st_size > limit:
                        raise SyncError("Git output exceeds transport limits.")
                    self._check_disk(repo)
                    remaining = self._remaining(deadline)
                    try:
                        proc.wait(timeout=min(0.05, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        pass
                self._remaining(deadline)
                self._check_disk(repo)
                if os.fstat(output.fileno()).st_size > limit:
                    raise SyncError("Git output exceeds transport limits.")
                output.seek(0)
                result = output.read(limit + 1)
                self._remaining(deadline)
                if proc.returncode:
                    rejected = args[0] == "push" and any(
                        line.startswith(b"!\t") and b"\t[rejected]" in line for line in result.splitlines())
                    raise _GitFailure(proc.returncode, rejected)
                return result
            finally:
                if proc.poll() is None:
                    _stop(proc)

    @contextlib.contextmanager
    def _repository(self):
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="transport-", dir=self.cache_dir) as directory:
                repo = Path(directory) / "data.git"
                (repo.parent / "no-hooks").mkdir()
                self._run(repo, ["init", "--bare", "--template=", "--object-format=sha1", str(repo)])
                # Reject global URL rewrites rather than silently contacting a
                # different destination or enabling another transport helper.
                resolved = self._run(repo, ["ls-remote", "--get-url", "--", self.remote], limit=64 * 1024)
                if resolved.rstrip(b"\r\n") != os.fsencode(self.remote):
                    raise SyncError("Git remote rewriting is not supported.")
                yield repo
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            _LOG.debug("Git transport failure class %s, errno %s", type(error).__name__, getattr(error, "errno", None))
            raise SyncError("Git transport could not complete.") from None

    @staticmethod
    def _oid(raw: bytes) -> str:
        try:
            value = raw.strip().decode("ascii")
        except UnicodeError:
            raise SyncError("Invalid Git object identity.") from None
        if not _OID.fullmatch(value):
            raise SyncError("Invalid Git object identity.")
        return value

    def _head(self, repo: Path) -> str | None:
        try:
            refs = self._run(repo, ["ls-remote", "--exit-code", "--refs", "--", self.remote, self.ref], limit=4096)
        except _GitFailure as error:
            if error.code == 2:
                return None
            raise
        lines = refs.splitlines()
        if len(lines) != 1 or lines[0].split(b"\t")[1:] != [self.ref.encode("ascii")]:
            raise SyncError("Invalid data branch advertisement.")
        return self._oid(lines[0].split(b"\t")[0])

    def _fetch(self, repo: Path) -> str | None:
        if self._head(repo) is None:
            return None
        self._run(repo, ["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules",
                         "--no-write-fetch-head", "--no-auto-maintenance", "--no-progress", "--",
                         self.remote, self.ref + ":refs/pair-desk/snapshot"])
        return self._oid(self._run(repo, ["rev-parse", "--verify", "refs/pair-desk/snapshot^{commit}"]))

    def _files(self, repo: Path, commit: str) -> dict[str, bytes]:
        tree = self._oid(self._run(repo, ["rev-parse", "--verify", commit + "^{tree}"]))
        pending = [(tree, "")]
        blobs = []
        total = entries = tree_bytes = 0
        # Only the root and two whitelisted directories can be traversed. Check
        # each tree's uncompressed size before ls-tree allocates/decompresses it.
        while pending:
            self._remaining()
            tree, prefix = pending.pop()
            try:
                size = int(self._run(repo, ["cat-file", "-s", tree], limit=32))
            except ValueError:
                raise SyncError("Invalid data tree size.") from None
            tree_bytes += size
            if size < 0 or tree_bytes > MAX_TREE_BYTES:
                raise SyncError("Data tree exceeds transport limits.")
            listing = self._run(repo, ["ls-tree", "-l", "-z", tree])
            for entry in listing.split(b"\0"):
                self._remaining()
                if not entry:
                    continue
                entries += 1
                if entries > MAX_FILES + 2:
                    raise SyncError("Data tree exceeds transport limits.")
                try:
                    info, path = entry.split(b"\t", 1)
                    mode, kind, oid, size = info.split()
                    name = prefix + path.decode("ascii")
                    object_id = self._oid(oid)
                    if mode == b"040000" and kind == b"tree" and name in (".pair-desk", ".pair-desk/blobs"):
                        pending.append((object_id, name + "/"))
                        continue
                    if mode != b"100644" or kind != b"blob" or not _FILE.fullmatch(name):
                        raise ValueError
                    length = int(size)
                    if length < 0:
                        raise ValueError
                except (ValueError, UnicodeError):
                    raise SyncError("Data tree contains an unsupported path or mode.") from None
                total += length
                blobs.append((name, object_id, length))
                if len(blobs) > MAX_FILES or total > MAX_BYTES:
                    raise SyncError("Data tree exceeds transport limits.")
        # No filters, textconv, working tree or path interpretation in cat-file.
        raw = self._run(repo, ["cat-file", "--batch"],
                        data=b"".join(oid.encode() + b"\n" for _, oid, _ in blobs),
                        limit=total + len(blobs) * 128)
        stream = io.BytesIO(raw)
        files = {}
        for name, oid, length in blobs:
            self._remaining()
            if stream.readline(128) != f"{oid} blob {length}\n".encode():
                raise SyncError("Invalid data blob response.")
            content = stream.read(length)
            if len(content) != length or stream.read(1) != b"\n" or name in files:
                raise SyncError("Invalid data blob response.")
            files[name] = content
        if stream.read(1):
            raise SyncError("Invalid data blob response.")
        return files

    def read(self) -> tuple[str | None, dict[str, bytes]]:
        """Read only the exact remote branch; a missing branch is (None, {})."""
        with self._operation(), self._repository() as repo:
            commit = self._fetch(repo)
            return (commit, self._files(repo, commit)) if commit else (None, {})

    def head(self) -> str | None:
        """Probe the exact ref without fetching any objects (cheap poll fast path)."""
        with self._operation(), self._repository() as repo:
            return self._head(repo)

    def publish(self, files: dict[str, bytes], parent: str | None) -> str:
        """Replace a complete snapshot, conditional on its exact parent, without force."""
        with self._operation():
            return self._publish(files, parent)

    def _publish(self, files: dict[str, bytes], parent: str | None) -> str:
        if parent is not None and (not isinstance(parent, str) or not _OID.fullmatch(parent)):
            raise SyncError("Invalid parent commit.")
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise SyncError("Data snapshot exceeds transport limits.")
        files = dict(files)
        total = 0
        for name, content in files.items():
            self._remaining()
            if not isinstance(name, str) or not _FILE.fullmatch(name) or not isinstance(content, bytes):
                raise SyncError("Data snapshot contains an unsupported path or content.")
            total += len(content)
            if total > MAX_BYTES:
                raise SyncError("Data snapshot exceeds transport limits.")
        with self._repository() as repo:
            current = self._fetch(repo)
            if parent != current:
                raise PushRejected("Data branch changed; read it again before publishing.")
            if current is not None and files == self._files(repo, current):
                return current
            self._run(repo, ["read-tree", "--empty"])
            staged = repo.parent / "content"
            staged.mkdir()
            names = sorted(files)
            paths = []
            for i, name in enumerate(names):
                self._remaining()
                path = staged / str(i)
                path.write_bytes(files[name])
                paths.append(os.fsencode(path) + b"\n")
            hashes = self._run(repo, ["hash-object", "-w", "--no-filters", "--stdin-paths"],
                               data=b"".join(paths), limit=MAX_FILES * 41).splitlines()
            if len(hashes) != len(names):
                raise SyncError("Invalid data object response.")
            index = b"".join(b"100644 " + self._oid(oid).encode() + b"\t" + name.encode() + b"\0"
                             for name, oid in zip(names, hashes))
            self._run(repo, ["update-index", "-z", "--index-info"], data=index)
            tree = self._oid(self._run(repo, ["write-tree"]))
            args = ["commit-tree", tree]
            if parent is not None:
                args += ["-p", parent]
            commit = self._oid(self._run(repo, args, data=b"Update Pair Desk data\n"))
            try:
                self._run(repo, ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no",
                                 "--", self.remote, commit + ":" + self.ref])
            except _GitFailure as error:
                self._remaining()
                rejected = error.rejected
                if not rejected:
                    # A receive-side race after advertisement can be reported as
                    # "remote rejected" rather than porcelain "[rejected]".
                    try:
                        rejected = self._head(repo) != parent
                    except SyncError:
                        self._remaining()
                        pass  # Preserve a sanitized transport failure if offline.
                if rejected:
                    raise PushRejected("Data branch changed; read it again before publishing.") from None
                raise
            return commit
