# Development

Tests, smoke checks, benchmarks and the source layout. Back to the [README](../README.md).

```
python -m unittest discover -s tests      # store, HTTP API, event stream, MCP server and channel, CLI, hooks, installers, migration
PAIR_DESK_MIGRATION_SOURCE=<copy of a desk.sqlite> python -m unittest test_plans_groups   # (from tests/) also migrate a real desk copy
python scripts/smoke.py                   # real server process on a free port, end to end
python scripts/ui-smoke.py                # optional: browser checks with temporary data and a free port
python scripts/demo-desk.py --data <empty dir>                           # a throwaway desk with sample data
node scripts/ui-check.mjs http://127.0.0.1:8799 [shots-dir] [mygame]    # optional: drives Edge/Chrome headless against a throwaway desk
node scripts/test-timeline.mjs             # timeline renderer regression checks
node scripts/test-ui-refresh.mjs           # refresh cancellation and view/navigation behavior
```

For isolated large-desk HTTP/MCP/browser benchmarks, read
[the performance report and reproduction steps](performance.md). The benchmark
driver uses a copied database and owns its test processes; it never restarts a live desk.
The [1.7.1 mixed-load report](performance-1.7.1.md) adds progress, handoff and CLI
traffic alongside 20 MCP clients, with per-endpoint throughput and latency.

CI runs the tests and the smoke test on Linux, macOS and Windows with Python 3.11 to 3.13; the
browser check is a separate, manually started workflow. See [CONTRIBUTING.md](../CONTRIBUTING.md).

Layout: `desk.py` (entry), `pair_desk/` (`store.py` holds every rule and the change feed;
`groups.py` the grouping proposals; `notify.py` the prompt block, cursors and channel plumbing;
`server.py` (with the event stream), `cli.py`, `mcp.py`, `context.py` are thin layers over it),
`web/` (the UI), `skills/`, `hooks/`, `bin/` (the launchers), `integrations/` (Codex, opencode and
the `install` command), `.claude-plugin/` (plugin manifest with the MCP server entry, and the
marketplace), `pyproject.toml` (the package; the wheel keeps this layout under
`agent_pair_programming/`). The MCP entry lives in `plugin.json` rather than a root `.mcp.json`
because the plugin root is the repo root: a root `.mcp.json` would also register as a project MCP
server for anyone opening this repo in Claude Code.
