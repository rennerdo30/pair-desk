# CLI

Every command of `pair-desk` / `python desk.py`, for agents and scripts. Back to the [README](../README.md).

## Running the server

```
python desk.py serve                 # http://127.0.0.1:8765/
python desk.py serve --open          # and open the browser
python desk.py serve --port 9000 --data /path/to/desk-data
python desk.py serve --detach        # start in the background and return; `python desk.py stop` ends it
python desk.py serve --lan           # all interfaces, no authentication: trusted networks only
```

## Commands

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
python desk.py backup [--out file.zip]
python desk.py restore file.zip [--replace] [--check]
python desk.py install|update|uninstall [claude codex opencode antigravity muse] [--yes] [--dry-run] [--link SLUG]   # see install.md
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
