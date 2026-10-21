# Project sharing through a Git data branch

Pair Desk keeps its working database on each machine. An explicitly enabled project can
exchange portable records through the `pair-desk-data` branch of its existing repository.
GitHub, GitLab and other Git servers use the same transport. No Pair Desk hub is required.

Sharing is off by default. Choosing a remote for a project is permission to publish that
project's desk contents to everyone who can read that repository, including on public
repositories. Code-repository visibility is also the visibility of its data branch.

## Storage and identity

The data branch contains a format manifest and a complete structured JSON project snapshot
under `.pair-desk/`. Pair Desk manages an independent Git repository inside the local desk
data folder. It never changes a code checkout's branch, index, staged changes or working files.
There is no automatic merge of the data branch into `main` or any feature branch.

The portable snapshot includes issues, comments, plans, relationships, activity, handoff
revisions, build evidence and attachment bytes. Each record has a stable UUID. Local SQLite
row IDs and file paths are translated when importing. Normal `PREFIX-number` issue references
stay stable: each device reserves a block of 1,000 numbers before creating shared issues.
Numbers may have gaps. A device that exhausts its block must connect to reserve another before
creating more issues; it can continue editing existing records offline.

Machine folder links, notification cursors, agent routing preferences, credentials and queued
game commands are not shared. Locations remain issue metadata; importing them never sends
commands to a game. Build paths imported from another device are evidence only. Register a
build locally with `build set` before using Open or Run, even if the imported path exists.

## Setup and automatic synchronization

Use the `sync enable` CLI command separately on each device. Supply the local project, the
repository's credential-free remote URL, and the public name and email for data commits.
Authentication uses the existing Git credential helper or SSH setup. Credentials do not belong
in the remote URL, project records, or command examples.

For an existing `mygame` desk project, run this separately on each PC, replacing the remote
and commit identity with your own:

```sh
pair-desk sync enable --project mygame --remote git@github.com:your-account/mygame.git --author-name "Your public name" --author-email "you@example.com"
pair-desk sync status --project mygame --json
pair-desk sync run --project mygame
```

For GitLab, use its Git remote instead, for example `git@gitlab.example.com:team/mygame.git`.
This uses Git transport, not a GitHub/GitLab issues API or a separate provider token.
Git must be installed and able to authenticate to that remote without interactive prompts.
Run `pair-desk sync --help` for the available controls. Setup does not require switching your
code checkout to the data branch.

The first device seeds an absent data branch from one local project. A new device joins the
existing branch into an empty local project. Two independently populated desks are not matched
automatically by their issue titles or numbers: unrelated histories require deliberate migration.

Server and MCP sessions run a background synchronization worker. It observes database changes
from all writers, including the CLI, and periodically imports remote updates. Ordinary CLI use
also schedules bounded background synchronization. A cross-process lock serializes sync work
for each local desk. Local writes do not wait for a remote server to become available.

Persistent server and MCP workers attempt synchronization on startup and about every 30 seconds.
Short CLI commands schedule one hidden, deduplicated background process that coalesces writes
for 30 seconds before attempting synchronization and exits within a 120-second budget.
Use `sync run` when an immediate attempt is needed. Normal updates are eventually visible;
the background schedule is not an instant delivery guarantee.

`sync status` reports synchronization state and pending conflicts. `sync run` requests an
immediate attempt. `sync disable` stops sharing locally without deleting the remote branch or
the local desk. Disabling one PC does not stop other PCs from publishing.

## Concurrent changes and recovery

Pair Desk compares the last synchronized baseline, the current local records and the fetched
remote records. Independent comments and changes to different fields combine. Concurrent
changes to the same field, ordered plan or current handoff need explicit conflict resolution.
Deletion versus modification is a conflict too. Timestamps never decide which description,
verdict or plan wins. Local status and both conflict inputs remain available while sharing waits.

Inspect conflicts with `sync status --project mygame --json`. After reviewing the conflicting
values, `sync resolve --project mygame --take local` or `--take remote` selects that side for
the stored conflicts, while retaining non-conflicting edits from both sides. Then run
`sync run --project mygame` to publish the resolved state. These controls do not grant owner
verdict authority. If local records changed after the conflict was captured, refresh the
conflicts before choosing a side.

Structural conflicts involving issue parent/merge relationships cannot be resolved by choosing
an entire record set: that could discard unrelated comments and history. Both graphs are
retained. Repair the relationships through ordinary desk operations, refresh the stored conflict
against that new local state, and retry synchronization. No missing record is silently turned
into a deletion to make a graph fit.

Protected owner verdicts are preserved as historical data from the trusted repository. Sync
does not add an agent tool for marking checks passed, and generic conflict resolution cannot
manufacture a passed verdict. Repository write access is a trust boundary: a person or agent
with direct Git write access can edit the portable history. The data branch is not a
cryptographically authenticated owner-approval service.

Git operations happen outside database write transactions. Imports validate the complete
record graph and attachment integrity before applying one transaction, and check that local
records have not changed during network activity. An outbound journal and per-device
acknowledgements recover a crash after a successful push but before the local checkpoint.
Repeated imports do not duplicate comments or history. Failed requests retain pending changes.

Remote writes use ordinary, explicit-ref pushes. Concurrent forward updates are fetched and
recomputed with a bounded retry count. Force pushes are never used. Administrators must not
rewrite or delete the shared data branch while devices use it; such changes are outside the
forward-only synchronization contract. A pre-push check cannot make an administrative rewind
atomic with an ordinary push.

## Offline use, backups and size

Offline PCs can read their cached project and save changes. Other PCs see those changes only
after a successful synchronization. No disconnected device can reliably claim global task
ownership or know whether another agent has started working on the same task.

Keep independent whole-desk backups. Git history is useful recovery evidence, but sharing also
propagates intentional deletions. Restoring a backup preserves records, baselines and pending
recovery information while disabling automatic sharing and clearing the restored device's
active issue-number range. Explicit re-enablement creates a new device binding and allocation,
so a restored PC cannot reuse the original PC's reserved numbers.

Attachment bytes are included in portable data and retained by Git history. This is intended
for ordinary issue screenshots and documents, not game builds, logs with secrets, or large
media libraries. Data validation and transport enforce payload limits and refuse oversized or
malformed content without applying partial records. Removing an attachment from the current
desk does not remove its historical Git objects.

Disabling sharing or uninstalling Pair Desk does not erase copies already pushed to a remote
or imported by another PC. Repository administration and independent backups remain under the
repository owner's control.
