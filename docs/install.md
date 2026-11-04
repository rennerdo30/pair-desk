# Installing Pair Desk

How `pair-desk install` sets up each agent tool, how to install by hand, and what every integration adds. Back to the [README](../README.md).

## The installer

One command sets it up for every installed agent tool:

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

You need **Python 3.11 or newer**. The installer finds which of `claude`, `codex`, `opencode`, `agy`
and `muse`
are on your PATH. For each one it shows the exact steps it will run and asks before running them
(`--yes` skips the questions, `--dry-run` only shows them; name tools to limit it, e.g.
`install claude codex`). Then it offers to link the git repo you are in to a desk project, and
prints how to start the desk.

What it runs and touches:

| tool | what `install` does |
|---|---|
| Claude Code | only the official plugin commands: `claude plugin marketplace add rennerdo30/pair-desk`, `claude plugin marketplace update agent-pair-programming`, `claude plugin install agent-pair-programming@agent-pair-programming` |
| Codex | `integrations/codex/install.py`: a `pair-desk` MCP server and SessionStart and UserPromptSubmit hooks in `~/.codex/config.toml` (backed up first; nothing else in the file changes), and three skills in `~/.agents/skills/` |
| opencode | `integrations/opencode/install.py`: a `pair-desk` MCP server in `~/.config/opencode/opencode.json(c)` (backed up first), a small plugin in `~/.config/opencode/plugins/`, and the same skills |
| Google Antigravity (`antigravity`, CLI `agy`) | `integrations/antigravity/install.py`: an owned native plugin in `~/.gemini/config/plugins/pair-desk/` with MCP, session and model-invocation hooks, and three plugin skills |
| Meta Muse Code (`muse`) | `integrations/muse/install.py`: MCP and session/prompt hooks in `~/.config/muse/settings.json` (backed up first), and shared skills in `~/.agents/skills/` |

The agent integrations run Pair Desk by absolute path, so they need a copy that stays. Run from a git
clone, the installer uses the clone. Run through uvx, pipx or the bootstrap scripts, it first copies
Pair Desk to a per-user app folder (`%LOCALAPPDATA%\Programs\AgentPairProgramming` on Windows,
`~/.local/share/agent-pair-programming` elsewhere; `--app-dir` or `PAIR_DESK_APP` to change it).
Your desk's data lives elsewhere and is never touched (see [Data](data.md)).

**Update:** the same command with `update` (Claude Code: `claude plugin marketplace update` and
`claude plugin update`; other tools: a fresh copy and a re-run of their installers), then
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
For Antigravity and Muse Code, run `python integrations/antigravity/install.py` and
`python integrations/muse/install.py` (details below).

## Claude Code plugin

Install it with `pair-desk install claude` or by hand (see [The installer](#the-installer)). It needs Python 3.11+
(`python3` or `python`; the plugin's `bin/pair-desk` launcher finds it, or set `PAIR_DESK_PYTHON`).
On Windows the hooks run through Git Bash, which Claude Code uses when Git for Windows is
installed; without it the MCP tools and skills still work, but the session-start summary and the
prompt hook do not.

What it adds:

- **MCP server `pair-desk`** (stdio, declared in `.claude-plugin/plugin.json` and launched as `bin/pair-desk mcp`;
  same data folder as the web UI). Tools: `list_projects`, `list_issues`, `search(q, area?, status?, kind?, limit?)`
  (ranked full-text search with the matching field and an excerpt per result; see [Search](data.md#the-data-folder)), `get_issue` (follows merge
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
- **Who hears the owner:** one session should react to the owner, not every agent in the folder.
  - *Delegated jobs:* in a session agent-bridge started as a subagent (`AGENT_BRIDGE_JOB_ID` or
    `AGENT_BRIDGE_PARENT_JOB` set), SessionStart gives the summary line with "work from your brief"
    instead of the triage prompt and handoff, the prompt hook adds nothing, the channel stays off, and
    the MCP instructions and opencode rules say the same. The session that delegated the job handles
    the owner.
  - *Lead client:* `pair-desk project-set --lead codex` (or `lead` in `PATCH /api/projects/{slug}`)
    gives owner activity (prompt hook and channel) only to that client's top-level sessions; the
    others are told at session start which client handles it. `--lead none` restores the default,
    where every session is notified. opencode cannot lead: it reads the desk only at session start.
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

## Antigravity and Muse Code

```bash
python desk.py install antigravity muse --dry-run
python desk.py install antigravity muse --yes
python desk.py update antigravity muse --yes
python desk.py uninstall antigravity muse --yes
```

**Antigravity:** detection looks for `agy`, not the IDE launcher. Its native plugin contains
`plugin.json`, `mcp_config.json`, `hooks.json`, and the three Pair Desk skills. `SessionStart`
injects the linked project's summary and handoff; `PreInvocation` delivers new owner activity
before the next model call. The hook translates native `workspacePaths`/`conversationId` input
to Pair Desk's context lookup and returns `injectSteps`. It uses the first workspace path.
Restart agy and inspect `agy plugin list`, `/mcp` and `/hooks`; approve tool calls through your
existing policy. The installer adds no permission grants.

**Muse Code:** the installer merges `mcp_servers.pair-desk`, `hooks.SessionStart`, and
`hooks.UserPromptSubmit` into `$XDG_CONFIG_HOME/muse/settings.json` (default
`~/.config/muse/settings.json`). A new file includes `schema_version: 1`. An existing `mcpServers`
map is also supported; having both spellings is rejected before any write. The server is
`optional`, so an unavailable desk does not prevent Muse from starting, and writes are attributed
to `muse`. The entry sets `framing: auto`; the desk accepts JSONL or Content-Length framing and
replies in the client's format. This works with both Muse's TUI and headless configuration
parsers. The hooks deliver the summary/handoff at session start and owner activity on the next
prompt. The entry pins the data directory through an explicit `PAIR_DESK_DATA` server variable;
Muse's TUI can start MCP servers without the normal home-directory variables. Shared skills
use the existing ownership markers, so uninstalling Muse keeps skills still used by Codex or
opencode. Restart Muse, inspect `/mcp` and `muse skills list`, then
ask it to call `list_projects`. User hooks execute from your settings without a separate trust
step; MCP calls keep your existing approval policy.

Both installers support `--dry-run`, timestamped config backups, and idempotent updates. Native
hook commands quote arguments literally, including Windows paths with spaces. Foreign plugin
folders and same-named MCP entries are refused; unrelated settings, servers and hook groups are
preserved. JSONC comments are retained in backups when a Muse settings rewrite drops them.
Antigravity uninstall removes the active manifest, MCP and hooks, and leaves inert metadata
backups and an ownership marker so it can be reinstalled. Neither integration wakes an idle
agent: owner activity is delivered when the relevant hook next runs.

Direct installers also accept `--desk` and `--python`; Antigravity accepts `--plugin-dir`, and
Muse accepts `--config-dir`, `--skills-dir` and `--no-skills` for isolated setups.

Validated on Windows with Antigravity CLI 1.3.5 (`agy plugin validate`) and Muse Code 1.4.7
(native skill validation and required-MCP startup using the offline echo provider), on
2026-10-14. Unit tests cover hook context and owner-event delivery; authenticated model sessions
and native macOS/Linux runs have not been checked. The formats follow the official
[Antigravity plugins](https://antigravity.google/docs/plugins),
[Antigravity hooks](https://antigravity.google/docs/hooks), and
[Muse extension documentation](https://dev.meta.ai/docs/muse-code/extending).

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
  `<desk> hook session-start --client codex`; its summary line becomes developer context.
  `[[hooks.UserPromptSubmit]]` runs `<desk> hook user-prompt-submit --client codex` for the owner's
  new activity (see the UserPromptSubmit hook above). Codex runs
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
