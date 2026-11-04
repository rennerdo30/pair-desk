# Game integration and HTTP API

How a game files reports and picks up commands, and every HTTP endpoint. Back to the [README](../README.md).

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
| `PATCH /api/projects/{slug}` | `{name?, default_seed?, notify?, lead?}`: `default_seed` (integer, `null` clears) is the world seed that new agent issues and checks without a seed inherit; game reports keep what they send. `notify` is `{comments?, verdicts?, reports?, status?}`. `lead` (`claude`, `codex`, `antigravity`, `muse`; `null` or `"none"` clears) is the agent client whose top-level sessions alone get owner activity (see [Who hears the owner](install.md#claude-code-plugin)). |
| `GET /api/projects/{slug}/build` | `{project, build, builds_enabled}` |
| `POST /api/projects/{slug}/build` | `{path, commit?, label?, built_at?, author?}` publishes the current build (a build script can call it) → `{project, build, stamped: [ids], unchanged}`; the same build again changes nothing |
| `DELETE /api/projects/{slug}/build` | no current build (`?off=1`: stop using builds) → `{project, build, builds_enabled}` |
| `POST /api/projects/{slug}/build/open`, `.../build/run` | reveal the current build in the file manager, or start it (detached, in its own folder, no shell, no arguments) → `{ok, action, path, folder \| started}`. Acts only on the stored path (the body is ignored); 409 when the path is not on this machine, or for `run` a folder or not an executable. Needs the page's `X-Pair-Desk-Token`, a loopback client and a same-origin request, else 403. The build in `GET /api/projects/{slug}` and `GET /api/issues/{id}` carries `local: {exists, kind, open, run}`. |
| `POST /api/issues/{id}/build/open`, `.../build/run` | the same for the build stamped on the issue |
| `GET /api/projects/{slug}/issues` | filters: `status`, `kind`, `priority`, `area`, `source`, `milestone`, `size` (`S`, `M`, `L`, `none`) (each comma separated for several), `tag`, `q` (text search, see [Search](data.md#the-data-folder)), `boost_area` (with `q`: rank matches in this area first), `since` (updated at or after, ISO), `external_ref`, `seed` (location world seed), `merged` (`1`: only issues merged into another), `sort` (`triage` default, or `relevance` by default with `q`; `updated`, `created`, `oldest`, `priority`, `number`, `backlog`: priority, size, area), `limit` (default 500, max 5000), `offset`. Returns `{project, total, limit, offset, issues, counts: {status, kind, priority, area, seed, size, milestone}}`; each count ignores its own filter so chips show what selecting them would give. Optional `summary=1` omits issue `body` and full `plan`, keeping `plan_progress` and all other metadata; the default response is unchanged. With `q` the response also has `search: {mode}` (`ranked` or `substring`), and ranked issues carry `match: {field, excerpt}`. |
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
