# Large-desk performance measurements

Recorded 2026-10-04 on Windows, Python 3.14.7, SQLite 3.50.4 and headless Edge.
Baseline: `ede2b0b`; candidate: the `perf/desk-speed` changes accompanying this report.
All times below are **p50 / p95 in milliseconds**. These are local measurements, not
production acceptance or promises about another machine.

## Isolation and dataset

The live process/data path was identified from its command line. SQLite's online backup
API opened the live database with `mode=ro`; attachments and project configuration were
copied into the ignored worktree `.cache`. The immutable snapshot contains 3 projects,
1,757 issues (1,719 in the measured project), 11,098 comments, 2,921 attachments,
26,542 activity entries and 90 handoff versions. All attachment files were present and
matched recorded sizes: 2,270,337,367 bytes in total.

Snapshot database SHA-256:
`5e74d0dd456ed59606cca297573b6fd61cea537d78918b2006dfa68cd9b78efb`.
Every run restored a separate writable database from this snapshot. Only the owned
port-8799 server and owned MCP/browser processes were stopped. The live port-8765 server,
its agent processes and its data were neither migrated nor restarted.

## Causes and changes

- List queries evaluated correlated comment/attachment/child counts before pagination,
  sorted wide issue rows, and repeated the expensive search predicate for each facet.
  Materialized page IDs now bound the indexed aggregates to the selected rows; facets
  share one narrow candidate set. Own-filter exclusion, merged counts, pagination and
  all existing sorts retain their original semantics. Older system SQLite libraries omit
  the optional materialization hint and use compatible CTE syntax.
- All reads shared the writer's Python lock. WAL was already enabled; the problem was
  the application lock and repeated work, rather than missing WAL. A reusable pool of
  at most eight read-only connections now serves consistent multi-query snapshots.
  Writes remain transactional on the locked writer.
- Repeated reads rebuilt and serialized identical objects. Per-Store caches are bounded
  to 64 entries / 16 MiB, share misses, and cache response bytes. A dedicated SQLite
  `data_version` observer invalidates them for local and cross-process commits, even
  when timestamps do not change. Commands also invalidate at expiry; local build
  capabilities are checked against the current filesystem before serving cached detail.
- Commands lacked an issue index; verdict, child and facet lookups also benefit from
  additive indexes. MCP comments now fetch their new comment and counts rather than
  loading the complete history only to discard it.
- The default TCP accept backlog of five produced roughly 500 ms connection-retry tails
  for concurrent clients. It is now 128, and tiny responses disable Nagle buffering.
  Writer reservation retries retain the 10-second deadline with short jittered waits.
- The UI fetched descriptions and full plans for every row. Optional `summary=1` retains
  metadata and plan progress while omitting those unused fields. The default HTTP list
  contract stays unchanged, as does the MCP tool JSON contract. Conditional GET/HEAD
  responses use ETags; writes/errors are not cached. Superseded list requests abort;
  unchanged lists preserve the DOM, view changes still render, and late detail refreshes
  cannot navigate away from a newly opened handoff.
- Attachments used `read_bytes()`. They now stream in 256 KiB chunks; HEAD does not read
  their contents. No attachment content or stored records are rewritten.

## Hot endpoint measurements

Each HTTP sample consumes the complete response over a new loopback connection. HTTP
lists in the full-response rows use the original API shape. The UI rows pass `summary=1`
to both revisions (the baseline ignores this previously unknown option). Search uses
`terrain`; filters use the largest nonempty area and priorities p1/p2. Detail selects the
unmerged issue with the most comments plus attachments. MCP calls use 20 persistent
stdio processes; the hot comment phase writes only to 20 copied fixture issues.

Three warmups precede 40 serial samples. The concurrent phase uses 20 clients, ten calls
each, for 200 samples per endpoint. MCP peers have independent caches and their initial
misses are included. Measurements include parsing/transport in the client, not just SQL.

| Endpoint | Before serial | After serial | Before 20 clients | After 20 clients |
| --- | ---: | ---: | ---: | ---: |
| Health | 1.2 / 11.3 | 0.9 / 11.1 | 2.1 / 503.1 | 6.9 / 10.7 |
| Projects | 1.4 / 11.0 | 0.9 / 11.2 | 5.8 / 505.1 | 7.4 / 11.4 |
| Project overview | 11.5 / 22.5 | 1.4 / 11.8 | 122.8 / 706.7 | 11.3 / 12.3 |
| Triage, unfinished, 200 | 89.9 / 102.7 | 1.2 / 11.3 | 1277.8 / 1840.8 | 8.4 / 11.8 |
| Triage, all, 500 | 99.7 / 110.5 | 2.3 / 12.2 | 1445.7 / 3310.5 | 11.4 / 16.4 |
| Backlog, full, 5,000 limit | 150.0 / 170.1 | 4.6 / 14.8 | 2181.8 / 3164.0 | 32.1 / 49.4 |
| UI triage, summary, 300 | 94.1 / 104.9 | 1.4 / 11.5 | 1200.4 / 1621.1 | 9.0 / 12.1 |
| UI backlog, summary, 5,000 limit | 156.2 / 173.0 | 11.1 / 12.6 | 2768.8 / 3906.1 | 9.5 / 13.1 |
| Area + priority filter, 200 | 68.9 / 77.0 | 1.3 / 11.6 | 1081.2 / 1417.7 | 8.9 / 14.1 |
| Search, 200 | 316.1 / 550.5 | 11.1 / 11.8 | 6079.3 / 6917.3 | 8.9 / 12.8 |
| Rich detail + comments/attachment metadata | 4.4 / 14.9 | 10.5 / 11.6 | 28.0 / 536.5 | 10.1 / 16.6 |
| Latest handoff | 1.2 / 11.2 | 1.0 / 11.4 | 4.1 / 504.8 | 7.7 / 11.3 |
| HTML document | 1.2 / 11.2 | 1.5 / 11.3 | 4.1 / 503.5 | 10.6 / 12.7 |
| MCP list, 50 | 34.0 / 39.1 | 0.4 / 0.5 | 287.7 / 422.7 | 0.7 / 89.7 |
| MCP get, rich detail | 6.4 / 8.4 | 3.0 / 3.6 | 31.8 / 59.6 | 23.8 / 47.8 |
| MCP comment | 1.3 / 1.4 | 1.4 / 1.5 | 1.2 / 335.2 | 10.8 / 90.6 |

Small endpoints show scheduler/transport jitter, sometimes around 10 ms, and some higher
medians after allowing more clients to enter concurrently. The large-list/search and
connection-tail improvements are much greater than this jitter. No requests failed in
the completed measured runs.

## Browser initial load

The browser-only pair uses the same original snapshot on both revisions, with no MCP
load. Each cache mode has 20 measured reloads after a warmup. The timer runs inside the
page from navigation until issue rows exist and two animation frames have passed;
DevTools polling time is excluded. A 200 ms pause after each measurement lets the live
stream catch-up finish before the next reload. Both runs had zero page exceptions.

| Browser: navigation to first painted issue rows | Before | After |
| --- | ---: | ---: |
| Browser cache disabled | 177.7 / 212.4 | 88.3 / 118.8 |
| Browser cache enabled | 208.1 / 277.6 | 84.7 / 144.6 |

The owner's reported 1.2-second interactive load was not independently reproduced by
this headless loopback test. These timings do not prove subjective responsiveness in the
owner's existing browser profile.

## Matched ongoing read/write load

Twenty MCP processes each perform list/get/comment rounds at one round per second,
staggered evenly. Comments target copied issues with rich histories, rather than empty
fixtures. The owner HTTP client reuses one keep-alive connection, matching normal browser
transport, and makes 30 reads of each listed endpoint. Agents perform
at least 30 rounds each and keep writing until the owner finishes, so slow owner reads
do not get an idle tail. MCP sample counts consequently differ between revisions;
arrival rate and owner request counts are held constant. Call order rotates across
processes; both revisions use the same script and restored snapshot.

| During 20 MCP agents writing continuously | Before | After |
| --- | ---: | ---: |
| Health (owner HTTP) | 0.4 / 0.6 | 0.4 / 2.1 |
| Triage, unfinished, 200 (owner HTTP) | 223.6 / 453.6 | 37.7 / 96.9 |
| Backlog, full, 5,000 limit (owner HTTP) | 331.4 / 461.2 | 173.6 / 421.5 |
| UI triage, summary, 300 (owner HTTP) | 250.6 / 403.2 | 42.5 / 127.4 |
| UI backlog, summary, 5,000 limit (owner HTTP) | 309.5 / 435.9 | 123.7 / 263.0 |
| Search, 200 (owner HTTP) | 998.5 / 2065.1 | 120.9 / 259.1 |
| Rich detail + comments/attachment metadata (owner HTTP) | 7.7 / 18.0 | 7.1 / 18.8 |
| Project overview (owner HTTP) | 21.0 / 47.3 | 10.4 / 26.7 |
| MCP list, 50 | 167.6 / 1069.2 | 20.2 / 41.2 |
| MCP get, rich detail | 10.6 / 20.5 | 8.9 / 15.4 |
| MCP comment | 4.7 / 11.6 | 2.4 / 4.1 |

MCP samples per method: 1394 before, 600 after. Phase duration: 69.8s before, 30.0s after.

Every commit invalidates read caches, so this measures cold work as well as hits. Full
backlog responses and rich detail can still be expensive under continuous writes;
SQLite remains a single-writer database. Faster read throughput also increases offered
write load in an unpaced saturation run. The paced comparison above holds agent arrival
rate constant; the separate hot 20-client comment results should not be mistaken for a
universal improvement to write latency.

A separate matched-rate repeat opening a fresh owner connection for every request had
variable HTTP tails: UI triage was 129.1 / 171.7 before and 51.0 / 414.0 after;
UI backlog was 199.4 / 273.7 before and 139.4 / 362.8 after. Even health (no database
read) moved from 1.5 / 11.4 to 2.5 / 90.2, and full backlog from 204.5 / 263.6 to
200.7 / 908.9. Those regressions are retained here: this change does not establish an
across-the-board improvement to new-connection tails under ongoing agent writes.

## Migration, compatibility and validation

Startup adds `commands_issue`, `attachments_comment`, `comments_verdict`, `issues_children`
and `issues_facets` with `CREATE INDEX IF NOT EXISTS`. Schema version remains **6**;
there are no table/column/data transformations and no release version changes. Current
schema read-only agents can skip unavailable optional index creation. Older releases
can reopen the indexed database, so rollback does not require a reverse migration.
Index creation temporarily reserves the SQLite writer; deployment and the first live
startup remain the main session's responsibility. Back up the complete data folder
before normal deployment, including attachment files.

Validation on the copy:

- 222 Python tests passed, including the real-desk-copy migration gate (no skipped tests).
  The 23 new tests cover query/filter/sort/count semantics, older SQLite query syntax,
  bounded caches/single-flight,
  external commits, reader/writer snapshots, command expiry, retry timeout recovery,
  additive/read-only indexes, compact MCP comments, ETags/build files and streaming.
- All 276 baseline/candidate list comparisons and all 1,757 full issue-detail comparisons
  matched exactly; project overviews and handoffs matched. Table-row digests were unchanged
  by startup/index installation. SQLite integrity and foreign-key checks passed.
- Process-level smoke: 16/16 passed. Timeline renderer and refresh behavior checks passed.
  Both plugin/marketplace manifest validation commands passed.
- Headless UI: **101/105** on both original and candidate. The same four existing collapsed
  timeline layout/keyboard checks fail on this Edge installation. The candidate's board
  and live handoff checks pass after correcting a view-redraw regression found during
  validation. Full browser acceptance is therefore still open.

The older-SQLite query path was forced on the current engine; an older SQLite binary was
not run. Unverified: deployment/live restart, the owner's actual browser session,
long-duration production mixes, LAN latency, Linux/macOS execution of this change, and
peak RSS during simultaneous very large attachment downloads. No live migration, release, tag, push,
plugin update or project handoff update was performed.

## Reproduce without touching a running desk

Run from a clean worktree with Python 3.11+; browser timing additionally needs Node 22+
and Edge/Chrome. Find the actual live data folder from the running server command line
or path configuration. Replace the placeholders below; choose an unused test port.
The snapshot destination must not exist and must be outside the live folder. The writable
benchmark destination must be a separate child of this worktree's `.cache`.

```powershell
python scripts/bench-desk.py --snapshot-live <live-data-folder> --snapshot .cache/perf-snapshot
git archive --format=zip --output=.cache/baseline-code.zip <baseline-commit>
Expand-Archive .cache/baseline-code.zip .cache/baseline-code
python scripts/bench-desk.py --code .cache/baseline-code --project mygame --out .cache/before.json --browser
python scripts/bench-desk.py --project mygame --out .cache/after.json --browser
python scripts/bench-desk.py --code .cache/baseline-code --project mygame --mixed-only --pace-ms 1000 --rounds 30 --out .cache/before-mixed.json
python scripts/bench-desk.py --project mygame --mixed-only --pace-ms 1000 --rounds 30 --out .cache/after-mixed.json
$env:PAIR_DESK_MIGRATION_SOURCE = (Resolve-Path .cache/perf-snapshot/desk.sqlite).Path
python -m unittest discover -s tests
python scripts/smoke.py
node scripts/test-timeline.mjs
node scripts/test-ui-refresh.mjs
```

Run these sequentially, avoiding unrelated builds/profiling while timing. The driver
refuses an occupied port, restores only the copied database, records latency JSON and
owns/cleans up its subprocesses. Do not instantiate another Store on the measurement
copy during a run. Raw timings, logs, copied data and screenshots stay under ignored
`.cache`; this report contains aggregate measurements only.

The recorded artifacts are `.cache/before-complete.json` / `after-complete2.json`,
`before-browser-pair.json` / `after-browser-pair.json`, and
`before-paced-final.json` / `after-paced-final.json`. The additional new-connection
mixed results are retained as `before-paced-new-connections.json` /
`after-paced-new-connections.json`. These files contain private dataset identifiers and
are deliberately not committed.

One discarded repeat encountered a Windows client `WSAEADDRINUSE` connection-allocation
error after repeated new-connection runs. Its processes were cleaned up, sockets were
allowed to expire, and the matched workload was rerun successfully. An earlier aggressive
browser reload attempt also stalled without a page exception; the final paired browser
runs use the post-measurement live-catch-up pause described above. These discarded attempts
are not mixed into the latency samples.
