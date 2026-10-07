# Pair Desk

**A local playtest desk, backlog and handoff that a game developer shares with their coding agents
(Claude Code, Codex, opencode).**

You play the game; your agents write the code. Pair Desk is the table between you:

- Agents file **checks** ("verify the lava flows now end in rounded tips") with the game's own
  chat command that takes you to the spot.
- You press **Send to game**, the running game teleports you there, and you answer **Passed** or
  **Still broken**, with pasted screenshots.
- You and the game file **reports** (bugs, ideas, tasks). Agents read them and your verdicts at the
  start of their next session, and hear about new ones on their next prompt, or live.
- Every issue carries the agent's **plan** (steps ticked as commits land); each project keeps a
  versioned **handoff** (where work stopped, next step, traps) and a sized, prioritised **backlog**.
- Everything is **live**: what the game, an agent or another tab writes shows up within a second.

It is 100% local: one data folder with a SQLite file and your screenshots, a Python 3.11+
standard-library server, a plain HTML/JS web UI with no build step, no CDN and no telemetry. The
server listens on `127.0.0.1` only. It works for several games at once and knows nothing about
any particular engine: locations are your game's own console commands.

![Pair Desk: the triage list with a check waiting for the owner, its plan and location](docs/images/triage.png)

## Install

One command sets it up for every agent tool you have (Claude Code, Codex, opencode):

```bash
uvx --from git+https://github.com/rennerdo30/pair-desk pair-desk install
```

No uv? Any of these does the same:

```bash
pipx run --spec git+https://github.com/rennerdo30/pair-desk pair-desk install
curl -fsSL https://raw.githubusercontent.com/rennerdo30/pair-desk/main/install.sh | sh     # macOS, Linux
```

```powershell
irm https://raw.githubusercontent.com/rennerdo30/pair-desk/main/install.ps1 | iex             # Windows
```

You need **Python 3.11 or newer**. The installer finds which of `claude`, `codex` and `opencode`
are on your PATH. For each one it shows the exact steps it will run and asks before running them
(`--yes` skips the questions, `--dry-run` only shows them; name tools to limit it, e.g.
`install claude codex`). Then it offers to link the git repo you are in to a desk project, and
prints how to start the desk.

What it runs and touches:

| tool | what `install` does |
|---|---|
| Claude Code | only the official plugin commands: `claude plugin marketplace add rennerdo30/pair-desk`, `claude plugin marketplace update agent-pair-programming`, `claude plugin install agent-pair-programming@agent-pair-programming` |
| Codex | `integrations/codex/install.py`: a `pair-desk` MCP server and a SessionStart hook in `~/.codex/config.toml` (backed up first; nothing else in the file changes), and three skills in `~/.agents/skills/` |
| opencode | `integrations/opencode/install.py`: a `pair-desk` MCP server in `~/.config/opencode/opencode.json(c)` (backed up first), a small plugin in `~/.config/opencode/plugins/`, and the same skills |

Codex and opencode run Pair Desk by absolute path, so they need a copy that stays. Run from a git
clone, the installer uses the clone. Run through uvx, pipx or the bootstrap scripts, it first copies
Pair Desk to a per-user app folder (`%LOCALAPPDATA%\Programs\AgentPairProgramming` on Windows,
`~/.local/share/agent-pair-programming` elsewhere; `--app-dir` or `PAIR_DESK_APP` to change it).
Your desk's data lives elsewhere and is never touched (see [Data](#data)).

**Update:** the same command with `update` (Claude Code: `claude plugin marketplace update` and
`claude plugin update`; Codex and opencode: a fresh copy and a re-run of their installers), then
restart your agent sessions. In Codex, trust changed hooks once with `/hooks`.

**Uninstall:** the same command with `uninstall`. It asks per tool, removes only what it added,
and leaves the desk's data folder alone.

### By hand

Claude Code, inside a session:

```
/plugin marketplace add rennerdo30/pair-desk
/plugin install agent-pair-programming@agent-pair-programming
```

Then restart Claude Code (or run `/reload-plugins`). From a clone, `/plugin marketplace add
/path/to/agent-pair-programming` works too, and `claude --plugin-dir /path/to/agent-pair-programming`
loads it for one session.

Codex and opencode, from a clone: `python integrations/codex/install.py` and
`python integrations/opencode/install.py` (details in [Codex and opencode](#codex-and-opencode)).

## Quick start

```bash
git clone https://github.com/rennerdo30/pair-desk && cd agent-pair-programming
python desk.py serve --open                 # the web UI at http://127.0.0.1:8765/
```

On macOS and Linux, `bin/pair-desk` runs the same thing with whichever of `python3` or `python` is
3.11+; on Windows, `bin\pair-desk.cmd`, or double-click `start-desk.cmd`. Inside Claude Code with
the plugin, `/agent-pair-programming:serve` starts the desk in the background.

1. **Create a project** for your game: *New project…* in the project menu, or
   `python desk.py new-project --slug mygame --name MyGame --prefix MG` (issues become `MG-1`, `MG-2`, …).
2. **Link your game repo** so agents working there find the project: put
   `{"project": "mygame"}` in a `.pair-desk.json` at the repo root, or run
   `python desk.py link --project mygame --path /path/to/mygame`.
3. **Work.** Your agents read the desk at session start (the plugin's hook does it), write a plan
   before they code, and file a check with a location command when something is ready for you.
   You open the check, press *Send to game* (or copy the command), look, and answer.
4. Optional: teach your game to **file reports and pick up commands** (see
   [Game integration protocol](#game-integration-protocol)), so a key press in the game files a bug
   with a screenshot and the exact spot.

Want to look around first? `python scripts/demo-desk.py --data /tmp/pd-demo` writes a desk with
sample data; open it with `python desk.py --data /tmp/pd-demo serve --open`.

| | |
|---|---|
| ![The backlog grouped by area](docs/images/backlog.png) | ![The project handoff](docs/images/handoff.png) |
| **Backlog:** open work by priority, size and area | **Handoff:** versioned, with diffs, updated by agents |

![A check the owner found still broken, in the dark theme](docs/images/dark.png)

## Security model

- **Loopback only.** `serve` binds `127.0.0.1`. Requests from other addresses, with a non-loopback
  `Host` header, or from a web page on another origin get `403`, which also blocks DNS rebinding
  and cross-site requests from sites you visit. Writes are also refused from an opaque `null` origin
  (a sandboxed frame) and from requests the browser marks cross-site.
- **Opening a build's folder or running it** works only on the path the desk stored, only from this
  machine, and only from the desk's own page: it carries a token the server writes into the page it
  serves, which no other page can read. Run starts the executable without a shell or arguments.
- **No accounts, no authentication.** Anything that runs on your computer can use the desk; that is
  the trust boundary.
- **`--lan` is for trusted networks only.** `python desk.py serve --lan` listens on all interfaces
  (e.g. to use the desk from your phone). It still has **no authentication**: anyone on that network
  can read, change and delete your issues and screenshots and queue commands for your game. Stop it
  when you are done.
- The HTTP API never reads files from disk; only the local CLI and MCP tools attach files by path
  (images and PDFs, checked by content). HTML in markdown is escaped and the UI runs under a strict
  Content Security Policy. Agents cannot mark anything *passed*: only you give that verdict.

See [SECURITY.md](SECURITY.md) for details and how to report a vulnerability.

## Data

One folder holds everything: `%LOCALAPPDATA%\AgentPairProgramming` on Windows,
`~/.agent-pair-programming` on macOS and Linux, or `--data <dir>` / the `PAIR_DESK_DATA`
environment variable.

| file | what |
|---|---|
| `desk.sqlite` (+ `-wal`, `-shm`) | projects, issues, comments, activity, plans, handoffs, command queue |
| `attachments/<project>/<yyyy-mm>/` | uploaded files, named by random id |
| `projects.json` | optional repo-path → project links (see *Linking a repo*) |
| `notify-cursors.json`, `channels/` | what each agent session has already been told |
| `server.log`, `server.pid` | only when started with `--detach` |

**Backup:** stop the desk (or make sure nothing is writing) and copy the whole folder; restore by
copying it back. `python desk.py export --project mygame --out mygame.json` writes a readable JSON
export (attachments as metadata; the files are in the folder).

## Running the server

```
python desk.py serve                 # http://127.0.0.1:8765/
python desk.py serve --open          # and open the browser
python desk.py serve --port 9000 --data /path/to/desk-data
python desk.py serve --detach        # start in the background and return; `python desk.py stop` ends it
python desk.py serve --lan           # all interfaces, no authentication: trusted networks only
```

## The web UI

- **Top bar:** project switcher (and *New project…*), search (titles, bodies, comments, areas,
  tags, refs, ids), *New issue*, theme (system / light / dark), keyboard help.
- **List:** quick-add bar (`Title !p1 @area +bug #tag`), status chips with counts (default view:
  every unfinished status; *Parked* and the done ones have their own chips), kind, priority, area
  and sort. Shift-click a chip to show only it. Status pills are coloured the same everywhere.
  Rows show the plan progress (`3/6`), size, milestone, "part of MG-4", and a parent's children
  progress.
- **Triage / Backlog** switch (`b`): the backlog shows open and in-progress work by priority, then
  size (S, M, L), then area, in collapsible area groups with counts and sizes. It replaces TODO.md's
  ordered backlog.
- **Select and merge:** tick the row checkboxes, or shift-click rows for a range, or `x` on the
  cursor row. The selection bar offers *Merge into…* (`m`) and *Group under…*; the dialog picks the
  target (the oldest by default).
- **Large lists:** triage, backlog and board render the visible rows with a small scroll buffer.
  Keyboard movement and selection include loaded issues outside that window; live edits keep
  unchanged rows in place. *Show more* still loads another page of issues.
- **Live updates:** tabs share one event stream across projects through a browser shared worker
  (short polling when unavailable), and release their subscriptions when navigating away.
  A comment, status change, plan tick, merge, game report or handoff save from anywhere appears
  within a second, and rows and
  timeline entries that just changed are highlighted. The footer says `● live`; if the stream
  drops, the page polls every few seconds until it is back.
- **Splitter** between the list and the detail: drag it, or focus it and use Left/Right
  (Shift for bigger steps); double-click (or Home) restores the default. Each pane keeps a minimum
  width; the ratio is remembered in this browser. Phones keep the one-pane layout.
- **Current build:** once a project publishes builds, the filter bar names the current one (red
  *No current build* when it was cleared); click it for a popover with its path and *Copy path*,
  *Open folder* and *Run*. The issue shows
  the build it was handed over in, with its commit and the same buttons, above the location.
  *Open folder* shows the build in the file manager and *Run* starts it; they appear only when the
  path is a file or folder on the machine the desk runs on (*Run* only for an executable, never for a
  folder or a version string).
- **Handoff** (the *Handoff* link, `h`, or `#/<project>/~handoff`): the project handoff rendered
  by section, *Edit* for the markdown, a version picker (author and time of each save) and the
  changes since the previous version. It updates live while open.
- **Detail** (side panel on wide screens, full screen on phones): status select, inline title edit
  (click it), kind/priority/area/tags/source/reference fields that save on change, markdown
  description, **location card** with every location command (one place each, numbered, with its label) in a
  monospace box, each with its own *Copy* and *Send to game* (shows "Waiting for the game…" until the game picks
  that command up, then who picked it up and when); *Edit* edits the list (add, remove, reorder, label),
  size, milestone and *Part of* fields, the **plan** checklist (tick a step, or *Note* to comment
  on it; progress bar and verification line), the parent line, the **children** with their
  statuses (and *Close MG-4* once every child is done), *Merged into this* with *Unmerge*,
  attachment gallery with a lightbox, and the timeline of comments and activity (plan changes,
  links and merges included; merged-in entries are marked "merged from MG-n", and each merged
  report shows as a card with its title, description and location).
- **Composer:** *Passed*, *Still broken*, *Comment*. Paste (Ctrl+V) or drop screenshots anywhere
  in the open issue; drop onto the attachments box to attach to the issue itself.
- A verdict moves the status (passed → *Passed*, failed → *Failed*). After *Passed*, the desk
  opens the next item of the current view, so a queue of checks goes fast.

Keyboard: `j`/`k` move, `Enter` open, `Esc` close (or clear the selection), `p` passed, `f` still
broken, `c` comment, `s` send the first command to the game, `y` copy the first command, `e` edit description, `x` select,
`m` merge the selection, `b` triage/backlog, `h` handoff, `n` new issue, `q` quick add, `/` search,
`1`-`9` one status, `0` all statuses, `r` refresh, `t` theme, `?` help.

Project settings (the project menu) hold the name, an optional default world seed (for games with
generated worlds) and which owner events notify agent sessions.

## Automatic verification and report updates

`auto_check` means **to be checked automatically by the agent** (screenshots, logs, tests and builds). A completed plan enters this state even without a location or published build. Agents record their evidence, then explicitly move to `to_check` only for the owner's manual review, or close with evidence according to project rules. Nothing marks automatic checks passed by itself; `passed` remains owner-only. Existing waiting checks remain `to_check` on upgrade.

The UI includes Auto check filters, counts, badges and a Board with status columns. SessionStart names the agent verification queue separately from owner work. MCP `update_issue`, HTTP `PATCH /api/issues/{id}` (agent callers supply `actor`), and CLI `edit` share the store's update logic. Agents can clarify an owner's short report; the first previous title/body appear as **owner's original** in the timeline, and later rewrites also keep previous field values in activity. Old edits cannot recover wording that earlier versions never recorded. Both MCP create and update accept milestone; update cannot set status or passed.

`python desk.py edit MG-1 --title "Clear title" --body "Reproduction and expected result" --milestone "Beta" --author agent`

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

## Game integration protocol

1. **Filing reports.** The game `POST`s to `/api/projects/{slug}/issues` with `source: "game"`,
   `author: "game"`, a `location` whose `command` is the chat command text that brings a player back
   to the spot (position, view, time, weather as the game's own commands express them), and
   optionally a screenshot as `attachments[].data_base64`. Status defaults to `reported`.
2. **Send to game.** While the game's dev console is enabled it polls
   `GET /api/projects/{slug}/commands/next?client=<name>` about every 2 seconds. `200` returns
   `{id, command, issue, ...}`: run `command` as if typed into chat (it may contain several
   commands separated by `;`). `204` means nothing is queued. Each command is delivered once;
   commands expire 10 minutes after they were queued, so a game started later does not replay old
   teleports. The UI shows "Picked up by `<name>`" as soon as the poll takes it.
3. **Being offline is normal.** When the desk is not running the requests fail to connect; the game
   should back off quietly (e.g. retry every 10 s) and never block play.
4. **One place per command.** An issue's `location.commands` is an ordered list of
   `{command, label?}`: each command takes the owner to ONE place (one teleport, at most one creature, plus
   look settings such as time and weather), and the label names it in a few words ("the harbor", "back at
   the camp"). A check that needs several places lists several commands; the owner runs each on its own
   (pasted into the console, or its own *Send to game* button), so places are never chained into one line.
   `location.command` always equals the first entry, for games that read one command; the desk keeps them
   in sync (a client that sets only `command` replaces the first entry). A game with a check command can
   offer the others by number (MyGame: `/check MG-12` runs the first and lists the rest, `/check MG-12 2`
   runs the second).
5. Keep `slug` configurable in the game (it is `mygame` for MyGame). Use `127.0.0.1`, not
   `localhost`, to avoid IPv6 resolution delays.

## HTTP API

JSON in and out; errors are `{"error": "..."}` with 400 (invalid), 403 (not local), 404, 405, 409 (duplicate).
No authentication. Requests from non-loopback addresses, with a non-loopback `Host` header, or with a
browser `Origin` other than `http://127.0.0.1[:port]`, `http://localhost[:port]` or `null` get 403
(unless `--lan`). Clients that send no `Origin` (the game, curl, agents) are fine.

| method and path | what |
|---|---|
| `GET /api/health` | `{ok, version, name}` |
| `GET /api/projects` | `{projects: [{slug, name, prefix, issue_count, counts: {status: n}}]}` |
| `POST /api/projects` | `{slug, name, prefix}` → the project (201) |
| `GET /api/projects/{slug}` | project plus `counts`, `areas`, `tags`, `last_change` |
| `PATCH /api/projects/{slug}` | `{name?, default_seed?, notify?}`: `default_seed` (integer, `null` clears) is the world seed that new agent issues and checks without a seed inherit; game reports keep what they send. `notify` is `{comments?, verdicts?, reports?, status?}`. |
| `GET /api/projects/{slug}/build` | `{project, build, builds_enabled}` |
| `POST /api/projects/{slug}/build` | `{path, commit?, label?, built_at?, author?}` publishes the current build (a build script can call it) → `{project, build, stamped: [ids], unchanged}`; the same build again changes nothing |
| `DELETE /api/projects/{slug}/build` | no current build (`?off=1`: stop using builds) → `{project, build, builds_enabled}` |
| `POST /api/projects/{slug}/build/open`, `.../build/run` | reveal the current build in the file manager, or start it (detached, in its own folder, no shell, no arguments) → `{ok, action, path, folder \| started}`. Acts only on the stored path (the body is ignored); 409 when the path is not on this machine, or for `run` a folder or not an executable. Needs the page's `X-Pair-Desk-Token`, a loopback client and a same-origin request, else 403. The build in `GET /api/projects/{slug}` and `GET /api/issues/{id}` carries `local: {exists, kind, open, run}`. |
| `POST /api/issues/{id}/build/open`, `.../build/run` | the same for the build stamped on the issue |
| `GET /api/projects/{slug}/issues` | filters: `status`, `kind`, `priority`, `area`, `source`, `milestone`, `size` (`S`, `M`, `L`, `none`) (each comma separated for several), `tag`, `q`, `since` (updated at or after, ISO), `external_ref`, `seed` (location world seed), `merged` (`1`: only issues merged into another), `sort` (`triage` default, `updated`, `created`, `oldest`, `priority`, `number`, `backlog`: priority, size, area), `limit` (default 500, max 5000), `offset`. Returns `{project, total, limit, offset, issues, counts: {status, kind, priority, area, seed, size, milestone}}`; each count ignores its own filter so chips show what selecting them would give. Optional `summary=1` omits issue `body` and full `plan`, keeping `plan_progress` and all other metadata; the default response is unchanged. |
| `POST /api/projects/{slug}/issues` | issue fields (`commands` is shorthand for `location.commands`, `command` for its first entry), plus `author` and `attachments: [{filename, mime, data_base64}]` → the full issue (201) |
| `GET /api/issues/{id}` | issue plus `comments` (with their attachments), `attachments`, `activity`, `commands` (last 10 sent for it) |
| `PATCH /api/issues/{id}` | any issue fields, plus `actor` for the activity entry. `commands` replaces the location's command list; `command` replaces its first entry (empty text removes it). |
| `DELETE /api/issues/{id}` | removes it with comments and files |
| `POST /api/issues/{id}/comments` | `{author, text, verdict?, attachments?}` → `{comment, issue}` (201) |
| `POST /api/issues/{id}/attachments` | JSON `{filename, mime, data_base64, comment_id?}` or `{attachments: [...]}`, or `multipart/form-data` (any file fields, optional `comment_id`, `author`) → `{attachments}` (201) |
| `GET /api/attachments/{attId}` | the file. Images, mp4/webm, PDF and plain text open inline; everything else downloads. |
| `DELETE /api/attachments/{attId}` | removes one |
| `POST /api/projects/{slug}/commands` | `{command, issue?, author?}` → the queued command (201) |
| `GET /api/projects/{slug}/commands/next?client=<name>` | oldest undelivered, unexpired command, marked delivered to `client`; **204** when there is none |
| `GET /api/projects/{slug}/commands?limit=` | recent commands with `state` |
| `GET /api/projects/{slug}/events` | Server-Sent Events: `hello` on connect, then `change` (`{changes: [{issue, type: comment\|activity\|handoff, action, actor, id}]}`) for every write to this project from any process (web, game, CLI, MCP), and `refresh` for writes without a feed row (a deletion, a setting). Keep-alive comment every 15 s. |
| `GET /api/events` | The same event protocol across all projects, for the browser's shared live connection. Each change names its `project`; `hello.project` is null. |
| `POST /api/issues/{id}/plan` | `{steps, verification?, actor?}` → the issue. Steps are text or `{text, state, commit, note}`; steps whose text matches keep their state. |
| `PATCH /api/issues/{id}/plan/steps/{n}` | `{state?, commit?, note?, text?, actor?}` (n = 1 for the first step; `""` clears commit or note) → the issue |
| `POST /api/issues/{id}/parent` | `{parent: "MG-4" \| null, actor?}` → the issue (no loops, same project) |
| `POST /api/issues/{id}/merge` | `{sources: ["MG-5", ...], actor?}`, `{id}` is the target → `{target, merged, issue}`; 409 when a source is already merged |
| `POST /api/issues/{id}/unmerge` | `{actor?}` → `{source, target, issue}` |
| `GET /api/projects/{slug}/suggest-groups` | `?limit=` → `{groups: [{action: merge\|group, target, issues, titles, score, reasons, hint}]}`; proposals only |
| `GET /api/projects/{slug}/handoff` | `?version=` → `{version, versions, markdown, sections, author, note, created_at}` (version 0: none yet) |
| `POST /api/projects/{slug}/handoff` | `{markdown, author, note?}` saves the whole document, or `{section, text, author}` one section → the new version |
| `GET /api/projects/{slug}/handoff/history` | `{versions: [{version, author, note, created_at, size}]}` |
| `GET /api/projects/{slug}/handoff/diff` | `?from=N&to=M` (to defaults to the latest) → `{diff}` (unified) |
| `GET /api/commands/{id}` | `{id, command, issue, created_at, expires_at, delivered_at, client, state}`; `state` is `pending`, `delivered` or `expired` |

Successful GET/HEAD responses for JSON reads and the web assets include an `ETag` and require
revalidation (`Cache-Control: private, no-cache`). Send `If-None-Match` to receive `304` when
unchanged. Writes and errors are not cached. Read caches detect committed writes from other
CLI/MCP processes, including writes with unchanged timestamps. Build controls also check the
current filesystem state. Attachment bytes stream in bounded chunks; HEAD returns metadata only.

Example: the game files a report with a screenshot in one call.

```
curl -X POST http://127.0.0.1:8765/api/projects/mygame/issues -H "Content-Type: application/json" -d '{
  "title": "Stuck in the rock near the harbor", "kind": "bug", "source": "game", "author": "game",
  "location": {"command": "/goto 1240 12 -380 yaw 90", "x": 1240, "y": 12, "z": -380, "yaw": 90, "place": "Harbor", "time": "17:30"},
  "attachments": [{"filename": "f12.png", "mime": "image/png", "data_base64": "iVBORw0KGgo..."}]}'
```

## Claude Code plugin

Install it with `pair-desk install claude` or by hand (see [Install](#install)). It needs Python 3.11+
(`python3` or `python`; the plugin's `bin/pair-desk` launcher finds it, or set `PAIR_DESK_PYTHON`).
On Windows the hooks run through Git Bash, which Claude Code uses when Git for Windows is
installed; without it the MCP tools and skills still work, but the session-start summary and the
prompt hook do not.

What it adds:

- **MCP server `pair-desk`** (stdio, declared in `.claude-plugin/plugin.json` and launched as `bin/pair-desk mcp`;
  same data folder as the web UI). Tools: `list_projects`, `list_issues`, `get_issue` (follows merge
  redirects), `update_issue` (title, body, kind, priority, area, tags, size, milestone, location/commands; keeps owner wording in activity; status uses `set_status`), `create_issue` (defaults for agents: kind `check`, status `to_check`, source `agent`;
  takes `size`, `milestone` and `commands`), `comment`, `set_status`, `set_location` (`commands` replaces the list, a
  lone `command` the first entry), `set_build(path, commit?, label?, built_at?)` (publishes the current build and
  stamps it on every `to_check` issue; the build also shows in `list_projects`, `list_issues` and `get_handoff`),
  `queue_command`, `import_checks` (bulk,
  deduplicated by `external_ref`); plans: `set_plan(id, steps, verification)`,
  `update_step(id, index, state?, commit?, note?, text?)` and `progress(id, text?, step?, state?,
  commit?)` (a step change and a short comment in one cheap call, answered with one line); groups:
  `link_parent(child, parent)`, `merge_issues(target, sources)`, `unmerge(id)`,
  `suggest_groups(project)` (proposals only); handoff: `get_handoff`, `set_handoff(markdown)`,
  `update_handoff(section, text)`.
  Agents cannot mark anything `passed`; the tools refuse it. Only the owner gives that verdict.
  `set_status` also refuses `to_check` while plan steps are still `todo` or `doing`, or while the issue has
  no location command (at least one); `set_location` sets them. In a project that publishes builds it also
  needs a current build.
- **Skills:** `pair-desk` (how an agent works with the desk), `/agent-pair-programming:serve`
  (starts the web UI in the background and prints the URL), `/agent-pair-programming:triage`
  (summarises new reports and failed checks for the current project).
- **SessionStart hook:** one line for the project linked to the session's folder, e.g.
  `Pair Desk (MyGame): 3 new reports, 2 failed checks, 14 waiting for the owner`, plus the
  handoff's *Next step* and *Traps* (and the current build, for a project that publishes builds) as
  context. It starts this session's notification cursor.
- **UserPromptSubmit hook:** before each prompt, a block of at most 12 lines with the owner's
  activity since this session last looked, newest first with issue ids: owner comments and plan
  notes, verdicts, new reports from the owner or the game, owner status changes and merges, e.g.
  `- MG-4 owner: STILL BROKEN (2 min ago): "the east flow is still square"`. Cursors are kept per
  session (and per project, for a session that has none yet) in `notify-cursors.json` in the data
  folder; sessions unused for 14 days are forgotten. The project's `notify` setting filters the
  events. Both hooks never create a data folder and print nothing when no desk or link exists.
- **Channel (live push into a running session):** the MCP server declares the Claude Code
  `claude/channel` capability and the plugin lists it under `channels`. In a session started with
  the channel loaded it sends every owner event of the linked project as a
  `notifications/claude/channel` message, which reaches the session even while it is idle and
  shows up as `<channel source="pair-desk" issue="MG-4" event="comment" project="mygame">…`.
  It waits on the desk server's event stream when one runs (port `PAIR_DESK_PORT`, default 8765),
  so a push follows a write immediately, and checks the database every 3 s otherwise. Channels are
  a Claude Code research preview; a plugin channel that is not on the approved list has to be
  loaded with the development flag:

  ```
  claude --dangerously-load-development-channels plugin:agent-pair-programming@agent-pair-programming
  ```

  The server looks for that flag (or `--channels` naming the plugin) on its ancestor processes;
  `PAIR_DESK_CHANNEL=on|off` overrides the detection. While it pushes, it keeps a heartbeat file
  in `channels/<claude pid>.json` in the data folder, and the prompt hook of that session stays
  quiet so nothing is announced twice. Without the flag, the prompt hook is the notification path.

### Linking a repo

Either put a `.pair-desk.json` in the game repo root:

```json
{ "project": "mygame" }
```

or keep the link outside the repo: `python desk.py link --project mygame --path /path/to/mygame`
(writes `projects.json` in the data folder). The marker file wins when both exist; subfolders of a
linked path match too.

## Codex and opencode

Two installers under `integrations/` give Codex CLI and opencode the same MCP server, skills and
session-start summary as the Claude Code plugin (`pair-desk install codex opencode` runs them for
you). They need Python 3.11+, are idempotent (a second run prints `Nothing to change.`), back up
the config file they edit to `<file>.pair-desk-backup-<yyyymmdd-hhmmss>` before any change, touch
only the `pair-desk` entries, and take `--dry-run` (show, write nothing) and `--uninstall`.

They write the command that starts the desk (`<desk>` below) with absolute paths: on macOS and
Linux the `bin/pair-desk` launcher (it picks `python3` or `python` each time it starts), on Windows
`python` or `py -3` (whichever is a Python 3.11+) plus `desk.py`. `--python <command>` overrides it.

```
python integrations/codex/install.py [--dry-run] [--uninstall]
python integrations/opencode/install.py [--dry-run] [--uninstall]
```

**Codex** (`$CODEX_HOME`, default `~/.codex`):

- `[mcp_servers.pair-desk]` in `config.toml`: `<desk> mcp`. Codex starts it in the
  session folder, so `linked_project` works as in Claude Code.
- `[[hooks.SessionStart]]` (startup, resume, clear) in `config.toml`, running
  `<desk> hook session-start`; its summary line becomes developer context. Codex runs
  a new or changed hook only after you review it: start `codex`, run `/hooks` and trust it
  (`codex exec --dangerously-bypass-hook-trust` skips that for one run).
- Both live between `# >>> pair-desk ... >>>` / `# <<< pair-desk <<<` comments; uninstall also finds
  them if Codex rewrote the file, and replaces a `pair-desk` entry made with `codex mcp add`.

**opencode** (`~/.config/opencode`, or `$XDG_CONFIG_HOME/opencode`):

- `mcp.pair-desk` (`type: local`, command array) in `opencode.jsonc` or `opencode.json`, whichever
  exists. A `.jsonc` file is rewritten as plain JSON, so its comments are dropped (the backup keeps
  them; the installer says so).
- `plugins/pair-desk.js`, a one-line re-export of `integrations/opencode/pair-desk-plugin.js` (edits
  in the repo apply on the next opencode start). In folders linked to a project it runs the
  session-start hook once per session, adds a `<pair-desk>` block (the rules and the summary line) to
  the system prompt through `experimental.chat.system.transform`, and shows the line as a toast on
  `session.created`. In other folders it adds nothing.

**Skills, both tools:** `pair-desk`, `pair-desk-serve`, `pair-desk-triage` in `~/.agents/skills/`
(the user skill folder Codex and opencode both read; Claude Code does not, so nothing doubles up).
They are generated from `skills/` with the absolute desk command in place of the plugin's `pair-desk`;
re-run an installer after the skills change. A marker file records which installers use them, so
uninstalling one tool keeps them for the other, and a same-named skill someone else wrote is left
alone (`--force` replaces it).

**For a repo's `AGENTS.md`:** copy the section in `integrations/AGENTS-snippet.md`.

**Verify:**

```
codex mcp list                       # pair-desk  .../bin/pair-desk mcp  enabled
opencode mcp list                    # ✓ pair-desk connected
codex exec --ephemeral -s read-only "Call the pair-desk MCP tool list_projects and show the slugs"
opencode run "Call the pair-desk MCP tool list_projects and show the slugs"
```

**What they cannot do (vs. the Claude Code plugin):** Codex skills have no slash-command form, so
`/agent-pair-programming:serve` becomes "use the pair-desk-serve skill" (Codex and opencode both pick
skills by description). The Codex hook is inactive until you trust it with `/hooks`. opencode has no
session-start context hook: the plugin injects the summary into the system prompt instead, computed
at the session's first model request and fixed for that session (so it stays cache-friendly but does
not refresh mid-session), and the toast shows only in the TUI, not in `opencode run`.

## CLI (for agents and scripts)

`python desk.py <command>`, `bin/pair-desk <command>` (`bin\pair-desk.cmd` on Windows), or
`pair-desk <command>` when installed as a package. Works on the data folder directly; no server needed. `--data` works before or after the command.
`--project` can be left out inside a linked repo (or when only one project exists).

```
python desk.py projects [--json]
python desk.py new-project --slug mygame --name MyGame --prefix MG
python desk.py project-set --project mygame [--name ...] [--default-seed 1234 | --default-seed none] [--backfill-seed] [--notify comments,verdicts,reports,status | --notify none]
python desk.py link --project mygame [--path /path/to/mygame]
python desk.py list --project mygame [--status to_check,failed] [--kind check] [--area terrain] [--q lava] [--seed 1234] [--since 2026-09-01] [--size S,M] [--milestone "World 1"] [--sort backlog] [--json]
python desk.py show MG-12 [--json]
python desk.py add --project mygame --title "Lava tips are round" --kind check [--status to_check] [--priority p1]
                   [--area terrain] [--body "..." | --body-file notes.md] [--command "/goto 1 2 3" [--command ...]] [--label "the harbor" ...]
                   [--location '{"place":"Ember Rift"}'] [--seed 1234] [--ref "commit 5ea28d6"] [--tags lava,terrain] [--attach shot.png]
                   [--size S] [--milestone "World 1"] [--parent MG-4] [--json]
python desk.py edit MG-12 [--title ...] [--priority p0] [--command ... [--command ...] [--label ...]] [--body ...]
python desk.py edit MG-12 --at 2 --command "/goto biome glacier" [--label "the glacier"]   # replace (or append) one command
python desk.py comment MG-12 --author claude --text "Fixed in 5ea28d6" [--verdict failed] [--attach shot.png]
python desk.py status MG-12 to_check [--author claude]
python desk.py build [show] [--project mygame] [--json]
python desk.py build set --path D:/builds/mygame/MyGame.exe --commit 5ea28d6 [--label "nightly 12"] [--built-at 2026-09-30T14:02Z]
python desk.py build clear [--off]            # no current build (to_check waits for the next one); --off stops using builds
python desk.py build open|run [--issue MG-12]  # show the current (or the issue's) build in the file manager, or start it
python desk.py attach MG-12 shot1.png shot2.png
python desk.py send --issue MG-12 [--at 2]     # queue the issue's command (the first, or N) for the game (or --project p --command "...")
python desk.py import-json checks.json [--project mygame]
python desk.py plan MG-12 [--step "Round the tips" --step "Clamp to ground" | --steps-file plan.md] [--verification "stage shot 12"]
python desk.py step MG-12 2 done [--commit 5ea28d6] [--note "..."] [--text "..."]      # 1 = first step; states todo|doing|done|dropped
python desk.py parent MG-7 MG-4 | python desk.py parent MG-7 --none
python desk.py merge MG-4 MG-9 MG-11           # merge MG-9 and MG-11 into MG-4
python desk.py unmerge MG-9
python desk.py suggest-groups [--project mygame] [--json]
python desk.py handoff [show] [--version N] | handoff set --file HANDOFF.md [--note "..."]
python desk.py handoff section --section "Next step" --text "..." | handoff history | handoff diff --version 3 [--to 5]
python desk.py export --project mygame [--out file.json]
python desk.py install|update|uninstall [claude codex opencode] [--yes] [--dry-run] [--link SLUG]   # see Install
```

`add` with `--kind check` defaults to status `to_check`. Author defaults to `$PAIR_DESK_AUTHOR` or `agent`.

`--steps-file` takes a JSON list of steps, or one step per line (`- [x] done step`, `- [~] doing`,
`- [-] dropped`, `1. todo`). `import-json` accepts `size`, `milestone` and status `parked` on each item.

`import-json` takes a list of issues, or `{"project": "...", "projects": [{slug, name, prefix}], "issues": [...]}`.
Each issue may carry `project`, `comments: [{author, text, verdict}]` and `attachment_paths: [...]`
(local png/jpg/gif/webp/pdf files up to 25 MB, checked by extension and content; relative paths resolve
against the current folder, then the JSON file's folder). The MCP tools `create_issue`, `comment` and
`import_checks` take `attachment_paths` too (relative to the repo). The HTTP API never reads local paths.
`project-set --backfill-seed` gives the default seed to existing agent issues and checks that have none. Issues whose `external_ref`
already exists in their project are skipped, so seeding the same TODO list twice is safe.

## Development

```
python -m unittest discover -s tests      # store, HTTP API, event stream, MCP server and channel, CLI, hooks, installers, migration
PAIR_DESK_MIGRATION_SOURCE=<copy of a desk.sqlite> python -m unittest test_plans_groups   # (from tests/) also migrate a real desk copy
python scripts/smoke.py                   # real server process on a free port, end to end
python scripts/ui-smoke.py                # optional: browser checks with temporary data and a free port
python scripts/demo-desk.py --data <empty dir>                           # a throwaway desk with sample data
node scripts/ui-check.mjs http://127.0.0.1:8799 [shots-dir] [mygame]    # optional: drives Edge/Chrome headless against a throwaway desk
node scripts/test-timeline.mjs             # timeline renderer regression checks
node scripts/test-ui-refresh.mjs           # refresh cancellation and view/navigation behavior
```

For isolated large-desk HTTP/MCP/browser benchmarks, read
[the performance report and reproduction steps](docs/performance.md). The benchmark
driver uses a copied database and owns its test processes; it never restarts a live desk.
The [1.7.1 mixed-load report](docs/performance-1.7.1.md) adds progress, handoff and CLI
traffic alongside 20 MCP clients, with per-endpoint throughput and latency.

CI runs the tests and the smoke test on Linux, macOS and Windows with Python 3.11 to 3.13; the
browser check is a separate, manually started workflow. See [CONTRIBUTING.md](CONTRIBUTING.md).

Layout: `desk.py` (entry), `pair_desk/` (`store.py` holds every rule and the change feed;
`groups.py` the grouping proposals; `notify.py` the prompt block, cursors and channel plumbing;
`server.py` (with the event stream), `cli.py`, `mcp.py`, `context.py` are thin layers over it),
`web/` (the UI), `skills/`, `hooks/`, `bin/` (the launchers), `integrations/` (Codex, opencode and
the `install` command), `.claude-plugin/` (plugin manifest with the MCP server entry, and the
marketplace), `pyproject.toml` (the package; the wheel keeps this layout under
`agent_pair_programming/`). The MCP entry lives in `plugin.json` rather than a root `.mcp.json`
because the plugin root is the repo root: a root `.mcp.json` would also register as a project MCP
server for anyone opening this repo in Claude Code.

## License

[MIT](LICENSE). Changes are listed in [CHANGELOG.md](CHANGELOG.md).
