# The web UI

A tour of the desk in the browser, its keyboard shortcuts and automatic verification. Back to the [README](../README.md).

## Layout

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
- **Copy as prompt:** copy an entry and its context as Markdown to paste into a coding agent.
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
