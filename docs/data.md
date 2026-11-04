# Data, search and backup

Where the desk keeps everything, how search ranks, backup and restore, and the data model. Back to the [README](../README.md).

## The data folder

One folder holds everything: `%LOCALAPPDATA%\AgentPairProgramming` on Windows,
`~/.agent-pair-programming` on macOS and Linux, or `--data <dir>` / the `PAIR_DESK_DATA`
environment variable.

| file | what |
|---|---|
| `desk.sqlite` (+ `-wal`, `-shm`) | projects, issues, comments, activity, plans, handoffs, command queue, the search index |
| `attachments/<project>/<yyyy-mm>/` | uploaded files, named by random id |
| `projects.json` | optional repo-path → project links (see [Linking a repo](install.md#linking-a-repo)) |
| `notify-cursors.json`, `channels/` | what each agent session has already been told |
| `server.log`, `server.pid` | only when started with `--detach` |

**Search:** `q` (web search box, `list_issues`, the MCP `search` tool, `list --q`) uses a full-text
index (SQLite FTS5, built on first open and kept current by triggers). Words match whole and
stemmed (`worker` finds `workers`), every word must match, `"quoted words"` match together and
`word*` matches a prefix. Results rank best first: a hit in the title counts most, then the details
(id, area, tags, ref, milestone, place and commands), the body, and comments least; words side by
side rank above words apart. Each ranked result names the field that matched with a short excerpt.
When no issue has the whole words, or the filters leave none of them, words match as prefixes and then
as plain substrings (`mode: "substring"`, unranked), which also finds part of a code name (`Store` in
`WorldStore`). The index holds copies only. Every Python that opens the desk should have FTS5 in its
SQLite (the python.org, Homebrew and common Linux builds do): an open without it drops the index
triggers so its writes still work, every process then searches substrings, and the next open with
FTS5 rebuilds the index. Until that process exits, an FTS5 open in the meantime would put the
triggers back and fail its writes.

**Backup and moving the desk:** `pair-desk backup [--out file.zip]` writes the whole desk to one zip
while it keeps running: a consistent copy of the database (SQLite online backup), every attachment
file and the repo links (`projects.json`), with a manifest of counts and checksums. The default is
`<data>/backups/pair-desk-<time>.zip`; keep a copy on another disk or machine. The search index is
left out and rebuilt on the first open. Screenshots are stored as they are, so the zip is about the
size of the attachments folder.

`pair-desk restore file.zip` checks the manifest, every checksum and the database before it changes
anything, and refuses a backup from a newer Pair Desk. It restores into an empty data folder; a folder
that already has a desk needs `--replace`, which moves it to `<data>.before-restore-<time>` (never
deleted) and refuses while a desk started with `serve --detach` runs there. Stop the desk and close
agent sessions on that folder first. `--check` only checks the file. To move a desk to another
machine: `backup`, copy the zip, `restore` there (it lists repo links whose folders do not exist
there; link them again with `pair-desk link`), then `pair-desk install`.

**Selected topics:** the web UI's project toolbar has **Export** and **Import**.
Export uses the list filters, search and persistent checkboxes to download a ZIP
with complete topic history and attachments. Import previews the dataset and
appends new copies without overwriting existing topics. See [topic transfer](topic-transfer.md).

`python desk.py export --project mygame --out mygame.json` writes one project as readable JSON
(attachments as metadata).

### Sharing a project across PCs

Projects can optionally synchronize through a dedicated `pair-desk-data` branch in their own
Git repository. Each PC keeps its local database and works offline; background synchronization
exchanges project records without touching code branches, staged changes or working files.
No always-on Pair Desk host is needed. Sharing remains disabled until explicitly configured.

See [Git project sharing](git-sync.md) for setup, conflict handling, repository visibility,
offline issue-number reservations, attachment storage and backup recovery.

## Data model

**Project:** `slug` (`mygame`), `name` (`MyGame`), `prefix` (`MG`; ids are `MG-1`, `MG-2`, …;
numbers are never reused), `default_seed`, and `notify`: `{comments, verdicts, reports, status}`
booleans (all on by default) choosing which owner events reach agent sessions.
`build` is the **current build** (or null): `{number, label, path, commit?, built_at, set_at, set_by}`, where
`path` is the player executable or build folder the owner plays, or a version string for a game that ships
releases, and `number` counts the builds the project has published. `builds_enabled` is true once the project
has published one. Publishing a build (`set_build`, `build set`, `POST .../build`) stamps it on every issue in
`to_check` with one `build` activity entry each ("published build X (was Y)", no comment), and an issue that
reaches `to_check` later (explicit status change or filed as a manual check) gets the build current then.
While a project with builds has none current (`build clear`), agents cannot move issues to `to_check`;
`build clear --off` stops using builds. Projects that never publish one work exactly as without the feature.

**Issue:**

| field | values |
|---|---|
| `id` | `MG-42` |
| `title`, `body` | text; body is markdown (headings, lists, task lists, code, tables, quotes, links, attachment images `![shot](attachment:12)`, issue refs like `MG-3`). HTML is always escaped. |
| `kind` | `bug` \| `check` \| `idea` \| `task` (default `bug`) |
| `status` | `reported` \| `open` \| `in_progress` \| `auto_check` \| `to_check` \| `passed` \| `failed` \| `parked` \| `closed` (default `reported`). `parked`: not dead, not now; hidden from the default view and the backlog. |
| `priority` | `p0` \| `p1` \| `p2` \| `p3` (default `p2`) |
| `area` | free text, suggested from existing values |
| `tags` | list of strings (or a comma separated string on input) |
| `location` | `{command, seed, x, y, z, yaw, pitch, place, time, weather, extra}`, all optional. `command` is the game's own chat command text, e.g. `/goto 1240 -380 yaw 90; /time 17:30`. `seed` is the integer world seed the place is in (a `/goto` only lands right in the same generated world); the desk adds `seed N` to the first `/goto` of the command it shows, copies and sends. Unknown keys move into `extra`. |
| `source` | `owner` \| `agent` \| `game` (default `owner`) |
| `external_ref` | free text: a TODO.md item title, a commit hash. Used to skip duplicates on import. |
| `size` | `S` \| `M` \| `L` \| empty: effort estimate for the backlog order |
| `milestone` | free text (max 120): the milestone or group the item belongs to |
| `build` | the build the issue was handed to the owner in, or null: `{number, label, path, commit?, built_at}`. Set when it reaches `to_check` and restamped by every newer build while it waits there; kept after the verdict (it says which build passed or failed). |
| `plan` | `{steps: [{text, state, commit?, note?}], verification, updated_at}`; `state` is `todo` \| `doing` \| `done` \| `dropped`. `plan_progress` is `{done, total}` (dropped steps do not count). |
| `parent` | the id of the issue this one is part of, or null; a parent reports `child_count`, `child_done` and (in detail) `children` |
| `merged_into` | set on a merged report: its id redirects there (`GET` returns the target with `redirected_from`), and comments or edits sent to it land on the target |
| `created_at`, `updated_at`, `closed_at` | UTC ISO times; `closed_at` is set while the status is `passed` or `closed` |

**Comment:** `author` (free text: `owner`, an agent name, `game`), `text` (markdown), optional
`verdict` (`passed` \| `failed`; moves the issue status), attachments.
**Attachment:** `filename`, `mime` (image types are sniffed from the bytes), `size`, `url`.
Max 50 MB each. **Activity:** status changes, edits, attachments, commands sent and picked up,
plan changes (`plan`), builds (`build`, and `build` on the status change that handed it over), links (`parent`, `child`), merges (`merged` on the target with the source's
title, body and location; `merged_into` on the source; `unmerged`). Comments, attachments and
activity moved by a merge carry `merged_from`.

**Handoff:** one markdown document per project with `## ` sections *State*, *Where work stopped*,
*Verified*, *Next step* and *Traps*. Every save is a new version (`version`, `author`, `note`,
`created_at`); older versions stay readable and diffable.

**Migrations:** opening an older desk adds the new columns and tables in place (one write
transaction, re-checked inside it, so the server and a hook opening it at once do not collide).
Nothing is rewritten or removed, and older code keeps working on a migrated file.
