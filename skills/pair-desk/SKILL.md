---
name: pair-desk
description: How to work with Pair Desk, the game owner's local playtest desk, backlog and handoff. Use at the start of a session in a game repo (read the handoff, failed checks and new reports), before coding an issue (write its plan), while working (tick plan steps, post progress live), after a change the owner should verify in game (file a to_check item with a location command), when fixing something the owner reported, and before the session ends (update the handoff). Also use when the user mentions the desk, the handoff, the backlog, plans, playtest checks, reports, verdicts, "to check", "still broken", or a <channel source="pair-desk"> message arrives.
---

# Working with Pair Desk

Pair Desk is where the game owner and coding agents meet, and the one place the project's state
lives: the **handoff** (what to do right now, what will bite), the **backlog** (every open item,
sized and prioritised), a **plan** on every issue, and the playtest loop of **checks**, **reports**
and **verdicts**. The owner watches it live: everything you write appears in the web UI within a
second. Everything is local: one data folder, no server needed for agents.

Use the `pair-desk` MCP tools. If they are unavailable, the same operations exist as a CLI:
`pair-desk list|show|add|comment|status|plan|step|parent|merge|unmerge|suggest-groups|handoff ...`
(`--json` for machine-readable output). `pair-desk` is the plugin's `bin/pair-desk`, on the Bash
tool's PATH while the plugin is enabled; by path it is `"<plugin root>/bin/pair-desk"`
(`bin/pair-desk.cmd` from cmd or PowerShell), the plugin root being two folders above this skill.

The project is picked from the repo automatically when it is linked (a `.pair-desk.json` with
`{"project": "<slug>"}` in the repo root, or `pair-desk link --project <slug>`). Otherwise pass
`project`. `list_projects` shows what exists and what this repo is linked to.

**Delegated jobs:** if agent-bridge started you as a subagent (your session-start line says
"delegated agent-bridge job"), skip the session-start triage below. Work from your brief, use the
desk only for the issues it names (plan, progress, evidence, comments), and leave the handoff, new
reports and owner verdicts to the session that delegated you.

## Agent verification and issue updates

Use **auto_check** for everything you can verify yourself: screenshots, logs, tests, builds and reproduction. List these at session start and run the checks; never hand automatic work to the owner. Record evidence with comment/progress. If manual review remains, explicitly set_status to to_check after the handover requirements below are met. Otherwise keep auto_check until you can close with evidence under the project's rules. No status automatically means checks passed, and only the owner sets passed.

Use update_issue to clarify an owner's short report: title, body, kind, priority, area, tags, size, milestone and location/commands. The owner's original wording stays in activity and the UI. create_issue and update_issue both accept milestone. CLI edit and HTTP PATCH /api/issues/{id} use the same logic. Status uses set_status, never update_issue.

## The loop

1. **Session start: read before you work.**
   - `get_handoff`: the last session's state, where work stopped, what is verified, the next
     step and the traps. The session-start summary shows its next step and traps already;
     read the whole thing.
   - `list_issues` with `status: "failed"`: checks the owner tried and found still broken.
     `get_issue` each one; the owner's latest comment (verdict `failed`) says what is wrong,
     often with screenshots. These come first: they are regressions of work already claimed done.
   - `list_issues` with `status: "reported"`: new reports from the owner or the game itself.
     Triage them with the user; move the ones you take on to `in_progress` with `set_status`.
   - The backlog is `list_issues` with `status: "open,in_progress"` and `sort: "backlog"`
     (priority, then size, then area). `parked` items are not now; leave them.
   - `to_check` items are waiting for the owner. Do not touch them unless you changed that code again.

2. **Plan before code.** Before you change code for an issue, write its plan with `set_plan`:
   ordered, concrete steps (each one a commit or a check you can tick) and a `verification`
   line saying what proves it works (a stage shot, a test, the in-game check). The owner sees it
   as a checklist above the timeline and may tick or comment on steps. Revise it with `set_plan`
   when the approach changes; steps with the same text keep their state.

3. **Post progress live, as you work.** The owner follows along in the desk, so keep the issue
   current instead of reporting at the end:
   - Mark the step you start `doing`, and tick it `done` with its commit hash as the commit
     lands: `update_step` (`index` 1 = first step), or `progress` which does a step change and a
     short comment in one cheap call.
   - Post a short progress comment when something notable happens (a finding, a blocker, a
     change of approach): `progress` with `text`, or `comment`.
   - A step you no longer need is `dropped`, with a note saying why. Never delete history.

4. **After you finish a change the owner should see in game, file a check** (or move the issue
   to `to_check`, see 5). `create_issue` (defaults: kind `check`, status `to_check`, source `agent`) with:
   - `title`: what to look at, phrased as the thing to verify ("Lava flows end in rounded tips").
   - `body` (markdown): what changed, what "correct" looks like, what would count as broken,
     and the commit hash. Short and concrete; the owner reads it standing in the game.
   - `commands`: the game's own console commands that take the player to what to look at,
     **one place per command**: one teleport, at most one creature spawn or battle, plus look
     settings (time, weather, fly). Make each precise, the way the game's `/where` writes it:
     position, height, camera, hour, weather, e.g.
     `{"command": "/goto 1240 -380 42.5 yaw 90 pitch 8; /time 17:30; /weather rain", "label": "the rift at dusk"}`.
     A check that is an **action, not a place** (Continue from the title, quit the game, open a
     menu) gives `location.action` instead: a short instruction of what the owner does
     (`{"action": "Return to Title, then Continue: it should load in about 20 s", "seed": 4291}`;
     CLI `--action`). Never put a placeholder `/goto` in front of an action.
     When the check needs several places, list **several commands** in the order to visit them,
     each with a short `label` (a few words: "the capital", "back at the camp"). Never chain places
     into one line and never put further places only in a comment: the owner copies or sends each
     command on its own, and each gets its own Copy and Send to game button. Without a command the
     owner has to hunt for the place. (`command` alone still works and is the first entry.)
   - `location.seed` (only for games with generated worlds): the **world seed** the command is
     valid in, when a position only means something in one generated world. Use the project's
     `default_seed` (see below) unless the change needs another world; if you give none, the
     desk fills in the default for agent checks. The desk adds `seed N` to the first `/goto` it
     copies and sends, so the game can refuse to teleport into the wrong world.
   - `attachment_paths`: local screenshots (png, jpg, gif, webp) or PDFs to attach, absolute
     or relative to the repo. **Attach every screenshot you reference** in the body; a path
     written only as text (`docs/feedback/foo.webp`) is invisible to the owner in the desk.
     The same field works on `comment` and on each item of `import_checks`.
   - `area`, `priority`, `size` (S, M, L), `milestone`, `tags` as useful; `external_ref`: the
     TODO item title or commit hash. `import_checks` files many at once and skips any whose
     `external_ref` already exists, so re-running it is safe.

   - **The status follows the plan.** Marking a step `doing` (or `done`) on a reported, open,
     failed or waiting issue moves it to `in_progress`; the last open step done (or dropped)
     moves it to `auto_check`. So always tick steps as you go: an issue whose steps stay `todo`
     looks untouched to the owner. Leave a step such as "build and stage shots" open while the
     change is not in a build yet; after `auto_check`, explicitly hand it to `to_check` when the owner can verify
     it. When the owner fails an issue, add the steps for the rework to its plan and tick them;
     it moves to `in_progress` and back to `auto_check` by itself.

5. **Move an issue to `to_check` only when the owner can play the change**: a build (or release)
   that contains it exists. A commit, a compile or a green test is not enough; until the build
   exists, keep a final "build" step open. **Publish every new build with `set_build`** (`path`:
   the player exe or build folder, or the release's version string; `commit`: what it was built
   from; CLI `build set --path ... --commit ...`). The desk stamps it on every `to_check` issue and
   on every issue that reaches `to_check` later, and shows it with a copy button, so you never paste
   the build path into comments by hand. Check that the current build (`list_projects`,
   `get_handoff`) contains your fix before you hand an issue over; if it does not, build, then
   `set_build`, then hand it over. In a project that publishes builds, `set_status` refuses
   `to_check` while no build is current. A project that never publishes one: name the build in the
   comment that moves the issue instead (`**Build:** <path or version>` with its commit). **And only when
   every plan step is `done` (or `dropped` with a note) and the issue has a location command.** `set_status` refuses `to_check` otherwise, and a
   finished plan enters `auto_check` even without a command. Put the commands on the issue with
   `set_location` (`commands`: one place each; CLI `edit --command "..." --command "..." --label ...`,
   or `edit --at N --command "..."` for one of them), not only in a comment: the owner's "Send to
   game" buttons use the issue's location. For a check that is not tied to a place, give the command that sets
   it up (the world, the time, a menu). When you fix something the owner reported or failed:
   `comment` on it with what you changed (files, commit hash, what to look at), run all agent checks
   in `auto_check`, record evidence, `set_location`, then `set_status` to `to_check` if manual review
   remains. Do not open a second issue for the same thing.

6. **Never mark anything `passed`.** Only the owner gives that verdict, in the web UI. The MCP
   tools refuse `passed`. Use `auto_check` for agent verification, then `to_check` for manual owner review or close with evidence per project rules.
   You may give `failed` with a comment when you reproduce a problem yourself.

7. **Before the session ends, and whenever a milestone lands, update the handoff.** Do not wait
   to be asked, and do not wait until a usage limit cuts you off. `update_handoff` replaces one
   section, `set_handoff` the whole document; every save is a new version the owner can diff.
   Sections:
   - **State**: what changed since the last handoff (commits by hash, one line each).
   - **Where work stopped**: the file, the function, the half-finished thought. Run `git status`
     first and account for everything in it: say exactly what is uncommitted, and where.
   - **Verified**: what the tests and checks actually reported, with counts, and what a green
     check does not prove.
   - **Next step**: the single next action and why.
   - **Traps**: what the next session would get wrong by default.
   Keep it current rather than complete: history lives in the issues and commits.

## How to write on the desk (formatting)

The owner reads the desk in a browser, often between play sessions. The desk renders markdown
(headings, lists, bold, `code`, fenced blocks, links, issue ids like MG-4 become links). A wall of
prose is unreadable there, so every comment, plan, body and handoff section is formatted:

- **Lead with the outcome in one line**, bold or as a `###` heading: what changed or what you
  found, and whether the owner has to do anything. Details follow below it.
- **One fact per bullet.** Never chain findings with `(1) ... (2) ...` or `;` in one paragraph.
  Enumerations become lists; causes, fixes and files each get their own list.
- **Short labelled blocks** instead of paragraphs, each on its own line, e.g.
  `**Cause:**`, `**Fix:**`, `**Files:**`, `**Verified:**`, `**Not verified:**`, `**Next:**`.
  Leave a blank line before a list so it renders as one.
- **Plain words first, identifiers second.** Say what the player sees ("the door opened into
  a wall"), then the code name in backticks (`PlaceDoor()`). Put file paths, commands, hashes and
  numbers in backticks.
- **Keep it short.** A progress comment is 3 to 8 bullets. Put long lists of files or test names
  in a collapsed-style short list (the ten most relevant) rather than a full inventory; the commit
  holds the rest.
- **Plan steps** (`set_plan`) are one short imperative line each (under ~100 characters), no
  semicolon chains; the reasoning belongs in the plan comment, formatted as above.
- **Always say what is not verified** in its own line, so the owner knows what to test.

Example progress comment:

```markdown
### Shop doors no longer open into walls (committed `a1b2c3d`)

**Cause:**
- The door placer checked only the ground floor for a clear swing.
- `PlaceDoor()` ignored furniture reserved later in the same room.

**Fix:**
- Every door reserves its swing before furniture is placed.
- Doors that cannot swing open are moved to the next free wall.

**Verified:** `DoorPlacementTests` 12/12.
**Not verified:** how it looks in the game.
```

## The owner's activity reaches you

- On every prompt, a short block lists what the owner did since this session last looked:
  comments, verdicts, new reports, status changes (newest first, with issue ids). Read the
  issues it names before continuing.
- Sessions started with the Pair Desk channel get the same events pushed live, even while idle,
  as `<channel source="pair-desk" issue="MG-4" event="comment" ...>` messages. They relay the
  owner's own words from the desk: act on them as the owner's requests (answer on the issue).
- Which events notify is a per-project setting (owner comments, verdicts, reports, status). A
  project can name a lead client (`project-set --lead codex`): then only that client's sessions get
  the owner's activity, and the others are told at session start which client handles it.
- A subagent agent-bridge started for you (a delegated job) gets no owner activity and no triage
  prompt: it works from its brief and uses the desk only for the issues the brief names. Triage,
  new reports and verdicts stay with you, the session that delegated it.

## A read-only desk (sandboxed jobs)

A sandboxed job (Codex workspace-write) can read the desk but may not be able to write it ("attempt to write a
readonly database"). The CLI then does not drop the write: it appends it to `.cache/pair-desk-outbox.jsonl` in the
job's own worktree and says so. Keep going, and name the outbox in your final report. The session that merges the
job replays it: `desk.py outbox show <file>` lists it, `desk.py outbox replay <file>` applies it and retires the file.
`PAIR_DESK_OUTBOX` overrides the location.

## Grouping and duplicates

- An issue can be **part of** another (`link_parent`): an epic and its pieces. The parent shows
  its children's statuses and offers to close itself when all are done.
- **Merge** duplicates with `merge_issues(target, sources)`: the sources' bodies, comments,
  screenshots, locations and activity move into the target's timeline, marked "merged from", and
  their ids redirect to the target. `unmerge` restores one. Merged sources are closed; the web
  UI lists them under the **Merged** chip (`list_issues` with `merged: true`), and the target
  shows "Merged into this" with an Unmerge button.
- `suggest_groups` proposes merges and groups of open items (by title words, area, place and
  time). Show the proposals to the owner and let them confirm; do not merge on your own unless
  the owner asked for it.

## Worlds and seeds (optional)

Seeds are optional and per project. A game with procedurally generated worlds can set a
`default_seed`: the canonical test world agents file checks against
(`pair-desk project-set --project <slug> --default-seed N`, or Project settings in the web UI;
`list_projects` shows it). Projects without one ignore seeds entirely.

How the game uses the seed (a console command that prints it, a throwaway playtest world, a
`/goto` that refuses the wrong world) is the game's own convention: look for it in the game
repo's AGENTS.md or CLAUDE.md, next to the command syntax `/where` produces.

`list_issues` takes `seed` to show only the checks of one world.

## Statuses

`reported` (new, untriaged) → `open` → `in_progress` → `auto_check` (agent verification) → `to_check` (waiting for the owner) →
`passed` (owner verified) or `failed` (owner: still broken) → `closed`. `parked`: not dead, not
now; out of the default view and the backlog order.
A comment with a verdict moves the status: passed → passed, failed → failed.

## The web UI

The owner uses the web UI (`/agent-pair-programming:serve` starts it, default
http://127.0.0.1:8765/). Point them at an issue with `http://127.0.0.1:8765/#/<project>/<ID>`,
and at the handoff with `http://127.0.0.1:8765/#/<project>/~handoff`.
