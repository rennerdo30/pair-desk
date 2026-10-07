# Tabs and large lists in 1.7.2 (PD-8)

Baseline web assets: `9b957d9` (1.7.1). Candidate: this branch's 1.7.2 changes.
Measured on Windows on 2026-10-08, using headless Edge 154 and Python 3.14.
These are browser checks against copied data, not acceptance of the owner's live server.

## Isolation and method

The worktree is `D:/Development/apw-wt/pd8`. The owner's full data folder was
copied into `.cache/data-copy`; the database was then replaced with a consistent
SQLite online backup through a read-only source connection. It contains 2,079
AnimaSky issues. Neither the owner server on 8765 nor foreign processes were
stopped or restarted. Only authorized PD-8 comments and plans were written to
the real desk. No AnimaSky source was changed.

Candidate server: 8808. Baseline server: 8809, serving unchanged 1.7.1 web assets
from `.cache/baseline-web/web`. Both read the same copied database and attachment
folder. Browser profiles, fixtures, screenshots and network captures are local
to the worktree's ignored `.cache`. There are no added runtime dependencies.

`scripts/test-tabs-list.mjs` drives real browser tabs over DevTools. It measures
three warm navigations per version with the same requested 1500 × 950 window,
software rendering (`--disable-gpu`), default 300-row first page, and then all
2,079 issues loaded through Show more. Full-list measurements use the Number
sort and All filter. A title edit is restored after measuring each live update.
Scrolling advances at six CSS pixels per millisecond for two seconds; intervals
come from animation-frame timestamps. Click timing ends at the detail's DOM
arrival. Live timing includes the HTTP write, watcher interval and row update.

The final baseline and candidate runs were serial, without concurrent test runs.
Other work on this machine continued, so CPU scheduling and tails were not
controlled. This is a small sample and not a device-performance guarantee.

## Final measurements

Raw reports: `.cache/pd8-before.json`, `.cache/pd8-after.json`. Each row below is
the median of three samples; ranges make the machine's variability explicit.

| Measure | 1.7.1 median (range) | 1.7.2 median (range) |
| --- | ---: | ---: |
| First list, ms | 956 (258–1,567) | 339 (249–414) |
| Click to detail, ms | 206 (100–392) | 44 (41–45) |
| Live write to updated row, ms | 970 (838–1,270) | 453 (379–513) |
| Scroll frames/s | 5.9 (2.3–21.1) | 60.0 (60.0–60.0) |
| Scroll p95 frame interval, ms | 233 (67–5,133) | 16.7 (16.7–16.8) |
| Rendered rows with full project loaded | 2,079 | 12 |
| Nodes removed/added by one live edit | 2,079 / 2,079 top-level rows | 11 / 11 children inside one retained row |

Earlier, less variable samples recorded a baseline scroll range of 20–26 fps,
with first-list median 325 ms and click median 97 ms; a candidate round recorded
60 fps, first-list median 299 ms and click median 51 ms. The large final baseline
tails should not be interpreted as a precise speedup attributable only to this
change. The repeatable results are bounded row counts, local row patching,
60 fps candidate scrolling in these runs, and working navigation across tabs.

## Root cause and reproduction

Two fresh tabs by themselves work on the baseline. After repeated first-tab
navigations, earlier per-page EventSource requests continue receiving change
bytes. There is no `pagehide` disposal in 1.7.1. Additional tabs eventually
exhaust the origin's six HTTP/1.1 sockets: Edge NetLog records
`SOCKET_POOL_STALLED_MAX_SOCKETS_PER_GROUP` for the copied desk origin.
List and issue requests remain pending while JavaScript and the page remain
responsive. The threshold varied between the third and fourth visible tab
because earlier navigation streams occupied the other connections. The owner's
exact existing-tab/navigation history was not inspected.

The fix shares one global EventSource in a SharedWorker, filters project changes
at each port, releases subscriptions on pagehide, reconnects after back/forward
restoration, and expires crashed clients' leases. Browsers without shared workers
use the existing short polling path. `/api/events` extends the existing ChangeHub;
the project-specific event route is unchanged. No second feed implementation or
cross-tab localStorage election is introduced. Route sequence checks and cancelled
detail reads also prevent stale navigation responses from replacing the chosen
issue or handoff. Browser-managed ETag revalidation remains intact.

Candidate evidence: eight tabs opened their deep links, one shared worker,
only `/api/events` as the live-stream URL, and zero socket-pool stalls in
`.cache/pd8-after-checks-netlog.json`. A second eight-tab run disables
SharedWorker and verifies visible-tab polling updates (`--checks --poll`).

## List design and checks

One window model serves triage, grouped backlog and board columns. Fixed 68-pixel
list rows, 104-pixel board cards and 36-pixel group geometry remove row measurement
from scroll handling. Board cards give titles two lines rather than squeezing a
full triage row into a narrow column.
The renderer reads viewport geometry once, writes in a scheduled frame, uses
binary search to find visible items, and retains nodes by issue id. Board columns
are windowed horizontally as well. Group headings retain their pinned behaviour.
Logical cursor, selection and range anchors survive recycling and live reordering;
keyboard movement unfolds and materializes an offscreen destination. ARIA positions
and the active descendant are maintained. Existing pagination is retained.

Verified:

- Eight-tab deep links and clicks, plus repeated navigations on the same browser.
- Keyboard movement to logical row 80, selection after scrolling/recycling,
  and an 81-row shift range including offscreen issues.
- Backlog headings and folding, board windows, and returning to triage.
- Row geometry has no gaps or overlaps. Candidate screenshots were inspected.
- Existing Python API tests: 10/10; live-notification tests including the new global
  stream check: 14/14; performance/cache/ETag tests: 25/25.
- Existing refresh and timeline scripts pass; new routing and shared-worker checks pass.
- Version manifests agree at 1.7.2; JavaScript syntax and diff whitespace checks pass.

The existing full browser script gives 101/105 on the candidate and 99/105 on
unchanged baseline assets. The same four build-run collapse/geometry/native
keyboard checks fail on both; the baseline additionally missed one highlight and
handoff timing check. These are recorded on PD-8. No existing check was weakened.
The build-run failures remain outside this change.

Not verified: owner's exact two-tab history, deployment after merge/restart,
other browser engines, a physical mobile browser, screen-reader acceptance or
dedicated quiet-machine timing. The owner's server still runs the previous release.

## Research and adoption

- [GitHub's large-review performance work](https://github.blog/engineering/architecture-optimization/the-uphill-climb-of-making-diff-lines-performant/):
  adopt measured DOM reduction and virtualization for large surfaces.
- [GitHub's accessibility analysis](https://github.blog/engineering/accessibility-considerations-behind-code-search-and-code-view/):
  preserve logical keyboard navigation and selection; avoid deriving state from
  only the materialized DOM. Screen-reader acceptance still needs verification.
- [Linear's large-workspace performance work](https://linear.app/changelog/2021-05-20-improving-performance-for-large-workspaces):
  adopt measurement at realistic issue counts and preserve fast navigation.
  This does not establish which virtualization implementation Linear uses.
- [MDN EventSource connection limits](https://developer.mozilla.org/en-US/docs/Web/API/EventSource):
  avoid permanent streams per tab on HTTP/1.1; share the connection and dispose it.

These sources and the implementation decisions are also recorded on PD-8.
