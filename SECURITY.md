# Security policy

## Supported versions

Security fixes go into the latest release. Please update before reporting.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/rennerdo30/pair-desk/security/advisories/new)
("Report a vulnerability" on the Security tab). Do not open a public issue.

Include what you found, how to reproduce it, the version (`python desk.py --version`) and your
operating system. You can expect a first answer within a week. Once a fix is released, the
advisory is published and you are credited unless you prefer otherwise.

## Security model

Pair Desk is a local tool. Its design assumptions:

- **Loopback only by default.** `serve` binds `127.0.0.1`. Requests from non-loopback addresses,
  with a non-loopback `Host` header, or with a browser `Origin` other than the desk's own
  (`http://127.0.0.1[:port]`, `http://localhost[:port]`, or `null`) get `403`. This blocks other
  machines, DNS rebinding and cross-site requests from web pages you visit. Writes (POST, PATCH,
  DELETE) are also refused from a `null` Origin (a sandboxed frame of any site) and from requests
  the browser marks `Sec-Fetch-Site: cross-site`.
- **Opening a build's folder and running its player** (`/build/open`, `/build/run`) act only on a
  path the desk stored, never on one from the request. They need a loopback client (also with
  `--lan`), a same-origin request and the per-server token the desk writes into the page it serves
  (sent as `X-Pair-Desk-Token`, which a foreign page cannot send without a CORS preflight the desk
  refuses). Run starts the executable without a shell and without arguments. No MCP tool starts
  anything.
- **No authentication.** Anything running on your machine as any user that can reach
  `127.0.0.1` can read and write the desk. That is the intended trust boundary.
- **`--lan` removes the network restriction** and still has no authentication: anyone on the
  network can read, change and delete issues and attachments, and queue commands for your game.
  Use it only on a network you trust, and stop the desk afterwards.
- **Commands for the game** are text the owner or an agent queued; the game decides what to run.
  Games should only accept their own console commands from the desk.
- **The HTTP API never reads local files.** Attachment paths are accepted only from the CLI and
  the MCP server (local processes), limited to image and PDF types and checked by content.
- **Uploaded files are served with their sniffed type**; only images, video, PDF and plain text
  open inline, everything else downloads. HTML in markdown is always escaped, and the UI is
  served with a strict Content Security Policy.
- **The installers edit other tools' configuration** (Codex's `config.toml`, opencode's config,
  `~/.agents/skills`) only in their own `pair-desk` entries, after a timestamped backup.
- **Git sharing is opt-in per project.** Enabling it publishes portable desk records and
  attachments to the chosen repository's data branch using existing Git authentication.
  Repository readers can read that history, and repository writers are trusted to modify it.
  A history record's author name does not cryptographically authenticate an owner verdict.
  The HTTP server stays loopback-only by default; Git sharing does not expose a desk server.
- **Shared data is validated before import.** Remote paths, executable modes, symlinks and
  submodules are not checked out. Queued game commands, local routing settings and folder
  links are excluded. Imported build paths cannot grant Open or Run capability; they require
  explicit local registration. Malformed snapshots fail before changing project records.
- **Restored desks require a new device binding.** Restoring a backup disables automatic
  sharing and clears its active number allocation while preserving pending recovery data.
  It cannot silently continue publishing as the original PC.

Reports that need a non-default setup (`--lan`, a hostile local user) are still welcome; say so
in the report.
