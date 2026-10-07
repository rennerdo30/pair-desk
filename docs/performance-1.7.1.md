# Concurrent agent load in 1.7.1

Baseline: `0fb9320` (1.7.0). Candidate: the changes released as 1.7.1. Measured
on Windows, Python 3.14.7, on 2026-10-07. These are copied-data measurements,
not live deployment acceptance.

## Workload and isolation

The driver reads the source database through SQLite online backup with `mode=ro`,
copies attachments/configuration, and restores the same snapshot before each run.
Writable data, raw results and owned process logs are under the worktree `.cache`.
The server runs on port 8799. Only its owned server/MCP children are stopped.
The live server and live data folder were not restarted, migrated or rewritten.
Authorized issue comments were posted through the live HTTP API.

The snapshot contains 9 projects, 2,737 issues (2,055 in the measured project),
20,876 comments, 6,228 attachments, 38,053 activity entries and 177 handoff versions.
The detail target initially has 531 comments; the acknowledgement regression uses
exactly 300 historical comments. Snapshot database SHA-256:
`b1e22dce231f127e70cc030699908f4b8a68b73b02ce087aaaf4dc1c7f13596d`.

Twenty persistent stdio MCP clients run staggered list/get/comment/progress/handoff
rounds at one round per second, at least 30 rounds each. One CLI client makes 30
list/show pairs; one HTTP client makes 30 requests per endpoint over keep-alive.
This represents 21 agent clients plus the owner UI. Writes target copied histories.
Agents keep writing until the HTTP client finishes; phase time includes CLI completion.
Each run has 3,000 MCP requests, 270 HTTP reads and 60 CLI invocations (3,330 operations).
Startup, snapshot copying and teardown are outside the measured phase.

Before: 30.039 seconds, **110.856 requests/s**. After: 30.341 seconds,
**109.752 requests/s**. The arrival rate is held constant; this is not a claim
about maximum throughput.

## Latency under mixed load

Times are **p50 / p95 milliseconds**, including transport. CLI times also include
interpreter startup. Per-endpoint throughput divides sample count by phase duration.

| Endpoint | Samples before / after | Before p50 / p95 | After p50 / p95 | Before / after requests/s |
| --- | ---: | ---: | ---: | ---: |
| MCP list, 50 | 600 / 600 | 85.8 / 876.0 | 19.6 / 66.4 | 19.97 / 19.78 |
| MCP detail, initially 531 comments | 600 / 600 | 45.5 / 280.4 | 30.0 / 580.6 | 19.97 / 19.78 |
| MCP comment | 600 / 600 | 5.2 / 28.9 | 5.8 / 90.6 | 19.97 / 19.78 |
| MCP progress | 600 / 600 | 9.3 / 54.6 | 3.7 / 106.8 | 19.97 / 19.78 |
| MCP handoff | 600 / 600 | 1.0 / 13.0 | 1.1 / 11.0 | 19.97 / 19.78 |
| HTTP health | 30 / 30 | 0.3 / 0.4 | 0.3 / 1.6 | 1.00 / 0.99 |
| HTTP unfinished triage, 200 | 30 / 30 | 55.1 / 73.5 | 34.6 / 54.7 | 1.00 / 0.99 |
| HTTP full backlog, up to 5,000 | 30 / 30 | 189.7 / 269.1 | 117.5 / 166.4 | 1.00 / 0.99 |
| HTTP UI triage summary, 300 | 30 / 30 | 55.4 / 91.7 | 36.2 / 50.8 | 1.00 / 0.99 |
| HTTP UI backlog summary | 30 / 30 | 151.5 / 189.3 | 76.5 / 111.1 | 1.00 / 0.99 |
| HTTP search, 200 | 30 / 30 | 225.8 / 284.4 | 113.1 / 175.4 | 1.00 / 0.99 |
| HTTP full detail | 30 / 30 | 17.9 / 38.3 | 17.0 / 35.3 | 1.00 / 0.99 |
| HTTP project polling | 30 / 30 | 17.1 / 32.1 | 13.2 / 27.9 | 1.00 / 0.99 |
| HTTP handoff | 30 / 30 | 0.8 / 1.6 | 0.9 / 3.5 | 1.00 / 0.99 |
| CLI filtered list, 50 | 30 / 30 | 315.3 / 1090.7 | 302.0 / 1356.0 | 1.00 / 0.99 |
| CLI full detail | 30 / 30 | 307.5 / 557.3 | 326.0 / 1084.5 | 1.00 / 0.99 |

List/search medians improved by roughly 35-77%; UI backlog/search medians roughly
halved. This run does **not** establish universal tail improvements: MCP
detail/comment/progress and CLI p95 regressed, even where medians improved.
Every commit still invalidates read caches; SQLite remains a single writer.
Large detail payloads, process startup and write contention remain material costs.

## Changes and why

- List metadata used grouped comment scans that read historical text-bearing rows
  to find verdicts. Page IDs still come first; counts now use covering indexes,
  and the latest verdict uses one covering-index seek. Child counts also use a
  covering index. Two indexes are additive, with no schema-version change or
  record migration; missing optional indexes remain valid for read-only clients.
- Search previously evaluated historical-text predicates separately for pagination
  and facets. It now shares one matching-ID set within the same WAL snapshot,
  bound as one JSON parameter. This avoids temporary writes and older SQLite
  parameter limits. Literal/multiple-term search and facet rules are preserved.
- MCP progress/step updates loaded the full timeline before returning an
  acknowledgement/plan. They use the existing compact store response now.
  Store, HTTP and CLI defaults still return full detail; MCP fields are unchanged.
- Existing bounded readers, commit-aware caches, ETags, projection, pagination,
  busy timeout and streamed attachments are retained. No second storage path or
  lock layer was added.

The approach follows [SQLite covering-index guidance](https://www.sqlite.org/queryplanner.html#covidx):
adopt index-only metadata reads, retain existing
[WAL reader/writer separation](https://www.sqlite.org/wal.html), and measure
before adding a storage/cache framework. Plans on this copy use covering
comment-count, verdict and child indexes after pagination.

## Verification and limitations

- Full suite: 223 tests passed with the copied-data migration gate enabled, no
  skips. A later added large-search regression passed too: 1,101 matches under
  a 999-parameter limit. The focused performance module passed 24 tests before
  that additional regression.
- 3,790 baseline/candidate comparisons matched exactly: every issue detail,
  sorts/pagination/summary modes, status filters, searches and project overviews
  on all nine copied projects.
- Process smoke: 16/16. Timeline renderer and UI cancellation/refresh scripts
  passed. Each completed load run checked database integrity.
- An earlier suite run encountered a Windows socket abort and a missing plugin
  toast during host command stalls. Both tests passed on baseline and candidate
  when rechecked; the complete suite then passed unchanged.
- Earlier artifacts `before-mixed.json` / `after-mixed.json` are retained but
  excluded from this comparison: a duplicate sibling benchmark and host/process
  startup stalls distorted them. The table uses only `before-final.json` /
  `after-final.json`, sequentially after validation and the overlap ended.
  Background work elsewhere on the machine was not stopped; host scheduling
  variance remains uncontrolled.
- Unverified: live restart, the owner's existing browser/session, Linux/macOS,
  an actual older SQLite binary, long-duration/saturation traffic and peak memory.
  The forced older-SQLite query path passes on the current engine.

## Reproduce

Use an unused port and new snapshot destination. Keep measurements sequential,
without builds/tests beside them. Raw output includes private IDs and stays ignored.

```powershell
python scripts/bench-desk.py --snapshot-live "$env:LOCALAPPDATA/AgentPairProgramming" --snapshot .cache/perf-snapshot
git archive --format=zip --output=.cache/baseline-code.zip 0fb9320
Expand-Archive .cache/baseline-code.zip .cache/baseline-code
python scripts/bench-desk.py --code .cache/baseline-code --project <slug> --mixed-only --pace-ms 1000 --rounds 30 --out .cache/before-final.json
python scripts/bench-desk.py --project <slug> --mixed-only --pace-ms 1000 --rounds 30 --out .cache/after-final.json
```

## Owner restart

From the updated main checkout, stop its recorded background server and restart
it. These commands were **not** run by this job:

```powershell
python D:/Development/agent-pair-programming/desk.py --data "$env:LOCALAPPDATA/AgentPairProgramming" stop
python D:/Development/agent-pair-programming/desk.py --data "$env:LOCALAPPDATA/AgentPairProgramming" serve --detach --port 8765
```

For a foreground server, use Ctrl+C in its terminal instead of `stop`, then the
same start command. Reconnect persistent MCP clients, and update any installed
plugin copy to 1.7.1, so they load the compact progress path.
