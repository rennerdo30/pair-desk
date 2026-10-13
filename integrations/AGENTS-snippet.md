<!--
Pair Desk rule for a game repo's AGENTS.md (read by Codex, opencode and other tools).
Copy the section below into the repo's AGENTS.md, replace <slug> and <pair-desk>, and link the
repo to its desk project with a `.pair-desk.json` file ({"project": "<slug>"}) or
`<pair-desk> link --project <slug> --path <repo>`.

<pair-desk> is how this machine starts the desk: `<install folder>/bin/pair-desk` on macOS and
Linux, `<install folder>\bin\pair-desk.cmd` on Windows (or `python <install folder>/desk.py`).
The installers print it; the generated skills in ~/.agents/skills use it too.
-->

## Pair Desk (playtest checks and reports)

Pair Desk (web UI http://127.0.0.1:8765, project `<slug>`) is where the owner and every agent
trade playtest checks, reports and verdicts.

- **Session start:** read the project handoff (`get_handoff`), then failed checks (`list_issues` status `failed`, then `get_issue` for the
  owner's latest comment), then new reports (status `reported`). They outrank the TODO order.
- **Automatic verification:** use `auto_check` for all screenshots, logs, tests and checks you can run yourself. Completed plans enter it automatically. Record evidence; move to `to_check` only if owner review remains, otherwise close with evidence per project rules.
- **Clarify reports:** use `update_issue` to rewrite titles/descriptions and set metadata, including milestone; the owner's original wording stays in activity. CLI: `edit`.
- **After a change the owner should verify in game:** file a check with `create_issue` (kind `check`,
  status `to_check`): what to look at, what correct and broken look like, the commit hash, the game's
  location `commands` (one place per command, further places as further commands; and the world `seed` if the project uses seeds), and attach every screenshot you mention.
- **Builds:** hand an issue to the owner (`to_check`) only when a build that contains the fix exists.
  Publish each new build with `set_build` (the player path or version, and its commit); the desk stamps it
  on every `to_check` issue, so never paste the build path into comments.
- **When you fix a reported or failed item:** `comment` with what changed, then `auto_check` for agent verification; after evidence, `to_check` if manual owner review remains.
  Do not open a second issue for the same thing.
- **Plan before code, progress live:** `set_plan` on the issue before coding; tick steps with
  `update_step` / `progress` (with the commit hash) as they land; `to_check` only when every step is done.
- **Never mark anything `passed`.** Only the owner gives that verdict.
- **Before you stop:** update the handoff (`update_handoff` / `set_handoff`): state, where work
  stopped (account for `git status`), verified, next step, traps.

Tools: the `pair-desk` MCP server (Codex, opencode, Antigravity and the Claude Code plugin register it) and the
`pair-desk` skill. Without them, use the CLI:
`<pair-desk> list --status failed,reported --json`, `<pair-desk> show <ID> --json`, `add ...`,
`comment ...`, `status ...`.
