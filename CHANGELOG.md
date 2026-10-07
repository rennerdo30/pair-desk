# Changelog

All notable changes to Pair Desk. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/). The version lives in
`pair_desk/__init__.py`, `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`.

## [Unreleased]

## [1.7.2] - 2026-10-08

### Fixed

- Deep links and additional tabs no longer compete with abandoned per-page live
  streams for the browser's HTTP/1.1 connections. Tabs share one stream across
  projects, close subscriptions on navigation, and fall back to polling when
  shared workers are unavailable. Stale navigation reads cannot replace detail.
- Triage, grouped backlog and board lists render a viewport window with keyed
  row updates. Keyboard movement, range selection and folded groups operate on
  logical issues rather than only the rendered rows. See the
  [browser performance report](docs/performance-1.7.2.md) for measured results.

## [1.7.1] - 2026-10-07

### Fixed

- Concurrent agent load: count list metadata through covering indexes after pagination,
  reuse one historical-text search for pages and facets, and keep MCP progress/step
  acknowledgements compact without loading complete comment threads. Existing HTTP,
  MCP and CLI response contracts remain unchanged. The [mixed-load report](docs/performance-1.7.1.md)
  includes per-endpoint throughput, latency and remaining tail regressions.

## [1.7.0] - 2026-10-05

### Fixed

- Large desks and concurrent agents: page issues before loading counts, share facet/search work,
  use bounded WAL readers and commit-aware caches, index issue commands, and return compact
  list metadata to the UI and MCP. HTTP reads support ETags; attachments stream with bounded
  memory. The UI cancels superseded list requests and skips unchanged redraws.

## [1.6.6] - 2026-10-02

### Fixed

- The version in the top bar is a small badge in the UI font, aligned with the name, instead of the browser's monospace fallback.

## [1.6.5] - 2026-10-02

### Added

- The web UI shows the running desk's version (`v1.6.5`) beside the name in the top bar, read from `/api/health`.

## [1.6.4] - 2026-10-02

### Added

- Outbox for a read-only desk (PD-4): a CLI write that SQLite refuses as read-only (a sandboxed agent job) is appended to `.cache/pair-desk-outbox.jsonl` in the job's worktree instead of failing, and `desk.py outbox show|replay <file>` lists or applies it from a session that can write the desk. Reads are never queued. 18 Codex sessions had lost their desk writes this way.

## [1.6.3] - 2026-10-02

### Added

- The web desk starts with the Claude Code session: the SessionStart hook checks `/api/health` and, when no desk answers, starts one detached (`serve --detach` behaviour) without waiting for it. A running desk of an older version that this desk started (its `server.pid`) is replaced, so a plugin update or `/reload-plugins` takes effect; a newer desk or another program on the port is left alone. Only when a desk database exists; `PAIR_DESK_AUTOSTART=0` turns it off.

## [1.6.2] - 2026-10-02

### Added

- `location.action` for checks that are an action, not a place ("Continue from the title", "Quit the game"). It stands in for a location command when an issue is handed to `to_check`; checks about a place still need their command. CLI `--action` on `add`/`edit` (an empty value clears it), MCP location schema, and the web UI shows it as "To do" and edits it with the location (PD-3).

### Fixed

- CLI `add` and `edit` take `--text` / `--text-file` as aliases of `--body` / `--body-file`, matching `comment`. A wrong flag now fails with a short error line instead of echoing a whole markdown body, which hid the error (PD-2).

## [1.6.1] - 2026-10-01

### Changed

- Issue timelines collapse consecutive runs of three or more build publications into a compact count, newest build label and time. A keyboard-accessible toggle reveals every original entry; comments and other activity separate runs, and one or two builds stay expanded. Stored activity and the API are unchanged.

## [1.6.0] - 2026-09-30

### Added

- MCP update_issue for issue text, metadata, milestone and location/commands, sharing CLI edit and HTTP PATCH logic. Owner report rewrites retain the original title/body in activity and show owner's original in the UI; later rewrites retain previous values too. Agents cannot use this tool to set status or passed.
- auto_check for agent verification (screenshots, logs, tests), separate from the owner's to_check. Completed plans enter auto_check; manual handover remains explicit and retains its plan/location/build requirements. Filters, counts, badges, status Board, CLI, MCP, SessionStart and agent instructions include the new queue.
- Milestone create/update tool schemas are covered by regression tests. Schema 6 preserves existing statuses and records; no existing to_check item is reassigned.

## [1.5.1] - 2026-09-30

### Changed

- List rows no longer carry the build name: every waiting check is in the current build, which the filter bar
  names; the issue's Build card keeps the details.

## [1.5.0] - 2026-09-30

### Added

- *Open folder* and *Run* for a build: on the issue's Build card and in a popover on the filter bar's
  current-build chip. They show only when the build path is a file or folder on the machine the desk runs on
  (*Run* only for an executable file). `POST /api/projects/{slug}/build/open|run` and
  `/api/issues/{id}/build/open|run` act only on the stored path: Open reveals it in the file manager, Run starts
  it detached in its own folder without a shell or arguments. CLI `build open|run [--issue ID]`; no MCP tool.
- The build in project and issue responses carries `local: {exists, kind, open, run}`.

### Security

- Open and run need a loopback client, a same-origin request and the per-server token written into the served
  page (`X-Pair-Desk-Token`); anything else gets 403.
- Every write (POST, PATCH, DELETE) is refused from a `null` Origin and from requests marked
  `Sec-Fetch-Site: cross-site`.

## [1.4.0] - 2026-09-30

### Added

- A **current build** per project: `{number, label, path, commit, built_at}`, where `path` is the player exe or
  folder the owner plays, or a version string for a game that ships releases. Publishing one (MCP `set_build`,
  CLI `build set --path ... [--commit] [--label] [--built-at]`, `POST /api/projects/{slug}/build`) stamps it on
  every `to_check` issue with one compact `build` activity entry each (no comment); an issue that reaches
  `to_check` later (status change, filed as a check, the plan's last step) takes the build current then, and
  keeps it after the verdict. `build show`, `build clear [--off]`, `GET` / `DELETE /api/projects/{slug}/build`.
- The build shows in `list_projects`, `list_issues` (per issue), `get_handoff`, `show`, `projects` and the
  session-start context.
- Web UI: the current build in the filter bar (click copies the path), a build chip on waiting checks, and the
  build with its commit and *Copy path* on the issue, in both themes.

### Changed

- In a project that publishes builds, handing an issue over (`set_status` / `status` `to_check`, and the plan's
  last step) needs a current build; projects that never publish one behave as before.
- The skill and the AGENTS snippet: publish every build with `set_build` instead of pasting its path into
  comments. Existing desks migrate in place (schema 5).

## 1.3.1 - 2026-09-30

### Changed

- The skill: move an issue to `to_check` only when a build that contains the change exists, and name the build.

## [1.3.0] - 2026-09-30

### Added

- Several location commands per issue: `location.commands` is an ordered list of `{command, label?}`, one place
  each, and `location.command` stays the first entry (kept in sync; a client that sets only `command` replaces the
  first). The web UI lists every command with its label, its own *Copy* and *Send to game* and its own picked-up
  state, and the location editor adds, removes, reorders and labels them (`s` and `y` act on the first). The API
  and MCP `create_issue` / `set_location` take `commands`; the CLI takes repeated `--command` with `--label`, `edit
  --at N` replaces one command and `send --at N` sends one. The desk adds the world seed to every command.
  Existing desks migrate in place (schema 4): each single command becomes a one-entry list, nothing else changes.

### Changed

- The status follows the plan: a step started moves the issue to `in_progress`, the last step done or dropped moves it to `to_check`.
- The MCP tool descriptions and the skill ask for one place per command, with further places as further commands.
- An issue reaches `to_check` from an agent only with a location command (`set_status`, the CLI and the plan all check it); the new MCP tool `set_location` sets it on an existing issue.

### Fixed

- Game commands passed to the CLI from Git Bash (`--command "/goto 1 2"`) no longer arrive as `C:/Program Files/Git/goto 1 2`.
- Unsized icons follow the text; the picked-up checkmark is no longer huge.

## [1.2.0] - 2026-09-29

### Added

- `pair-desk install | update | uninstall [claude codex opencode] [--yes] [--dry-run]`: one
  installer for all three tools. It detects which are installed, shows the exact steps, asks per
  tool, runs the official `claude plugin ...` commands and the Codex and opencode integration
  installers, then offers to link the current git repo to a desk project.
- Python packaging (`pyproject.toml`, standard library only), so the installer runs straight from
  GitHub with `uvx` or `pipx run`; `install.sh` and `install.ps1` bootstrap it with nothing but
  Python 3.11+.
- Cross-platform launchers `bin/pair-desk` (macOS, Linux, Git Bash) and `bin/pair-desk.cmd`
  (Windows): they pick a working Python 3.11+ (`python3`, `python`, `py -3` or
  `$PAIR_DESK_PYTHON`) and skip the Windows Store `python3` stub and old system Pythons.
- `scripts/demo-desk.py`: a throwaway desk with neutral sample data; `scripts/ui-check.mjs` takes
  the README screenshots from it.
- Agent formatting rules in the `pair-desk` skill and the MCP tool descriptions; a *Merged* filter.
- GitHub scaffolding: CI on Linux, macOS and Windows with Python 3.11 to 3.13, issue and pull
  request templates, contributing, security and conduct documents.

### Changed

- The plugin's MCP server and hooks start the desk through the launchers instead of `python`,
  which does not exist on many macOS and Linux machines.
- The Codex and opencode installers choose the Python command per OS (the launcher on macOS and
  Linux, `python` or `py -3` on Windows); `--python` still overrides.
- The example project in docs, skills and tests is a neutral `mygame` (`MyGame`, ids `MG-1`).
  World seeds stay an optional per-project setting.

## 1.1.0 - 2026-09-29

### Added

- **Plans** on every issue: ordered steps (`todo`, `doing`, `done`, `dropped`) with commits and
  notes, a verification line, progress in the list; `set_status` refuses `to_check` while steps
  are open.
- **Groups and merges:** "part of" links with child progress and a close offer; merging
  duplicates into one timeline (with unmerge); `suggest_groups` proposals.
- **Backlog:** size (S, M, L), milestone and a `parked` status; a Triage/Backlog switch grouped by
  area.
- **Handoff:** one versioned markdown document per project (state, where work stopped, verified,
  next step, traps) with history and diffs, in the web UI, CLI, HTTP API and MCP tools.
- **Live updates:** a Server-Sent Events stream fed by a database watcher, so writes from the
  game, the CLI, MCP tools or another tab appear within a second, highlighted.
- **Owner activity reaches agents:** a `UserPromptSubmit` hook with per-session cursors, and the
  MCP server as a Claude Code channel that pushes owner comments, verdicts, reports and status
  changes into running sessions. Per-project notify settings.
- **Codex and opencode integrations:** installers for the MCP server, skills, a Codex
  SessionStart hook and an opencode plugin that adds the desk summary to the system prompt.
- A draggable list/detail splitter; multi-select with *Merge into* and *Group under*.

### Changed

- The SessionStart summary also shows the handoff's next step and traps.
- Store schema v3, migrated in place on open.

## 1.0.0 - 2026-09-29

### Added

- A 100% local playtest and issue desk shared by a game owner and coding agents: one data folder
  with a SQLite file and attachments, several games at once.
- Web UI with no build step and no network access: filter chips with counts, triage order, a
  detail pane with the location card and *Send to game*, pasted and dropped screenshots, a
  lightbox, verdict buttons that walk the queue, keyboard control, light and dark themes, a phone
  layout.
- JSON HTTP API for the game (reports with screenshots, a once-only command queue with a
  10-minute expiry), bound to `127.0.0.1` unless `--lan`.
- CLI for agents and scripts, JSON import deduplicated by `external_ref`, export.
- A stdio MCP server; agents cannot mark anything passed.
- The Claude Code plugin: MCP server, skills (`pair-desk`, `serve`, `triage`) and a SessionStart
  summary.
- Optional world seeds on locations and a per-project default seed; attachments by local path
  for agents.

[1.5.0]: https://github.com/rennerdo30/pair-desk/releases/tag/v1.5.0
[1.4.0]: https://github.com/rennerdo30/pair-desk/releases/tag/v1.4.0
[1.3.0]: https://github.com/rennerdo30/pair-desk/releases/tag/v1.3.0
[1.2.0]: https://github.com/rennerdo30/pair-desk/releases/tag/v1.2.0
