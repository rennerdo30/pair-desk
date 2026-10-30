# Export and import selected topics

In a project's Triage, Backlog or Board view, choose **Export**. The popup starts
with the current list filters and search. Use the same status, kind, priority,
area, world and sort controls to find topics. Checkboxes retain their selection
when the search, filters or page changes. **Select this page** adds the visible
page; **Clear selection** clears all selections. **Export ZIP** downloads the set.

Parent/child groups and merged topics are exported together, including merge
provenance, so the dataset has no dangling relationships. This can add topics
outside the filter. The ZIP contains topic fields, plans, comments, verdicts,
activity, build evidence, command history and all attachment files. Missing
attachment files refuse export rather than producing an incomplete dataset.

Choose **Import** in the destination project and select the ZIP. Review the
source project, full topic list and attachment count, then choose **Import topics**.
Import appends copies with fresh destination numbers, remaps relationships and
attachment links, and preserves original rows in import provenance. It does not
overwrite or match existing topics. Reimporting the identical ZIP into the same
project is refused. A separately exported ZIP is a new dataset and can create
additional copies. Topic statuses and historical owner verdicts are retained;
the import requires the local desk page's confirmation token.

Project settings, project handoffs and machine configuration are outside a topic
dataset. Command history remains inert provenance, never queued. Imported build
paths require local registration with **build set** before opening or running.
Imports commit atomically; validation or write failures leave existing topics
and the topic counter untouched. Shared projects must have enough locally
reserved numbers; reconnect synchronization to reserve more if needed.

Archives support up to 5,000 related topics, 20,000 total records, 80 MiB ZIP size
and 160 MiB expanded contents, with the existing per-attachment size limit.
Versioned manifests list attachment sizes and SHA-256 checksums. Unsafe paths,
duplicate ZIP entries, missing files, unknown columns and broken references
are rejected before persistent writes. This portable format is separate from
whole-desk backups and the legacy JSON bulk-create command.
