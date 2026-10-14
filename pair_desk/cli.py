"""`python desk.py ...` - the web server, the agent CLI, the MCP server and the plugin hook."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sqlite3
import sys
import threading
import webbrowser
from pathlib import Path

from . import APP_NAME, VERSION, notify
from .context import resolve_project, save_link
from .paths import resolve_data_dir
from .store import (HANDOFF_SECTIONS, KINDS, LEAD_CLIENTS, NOTIFY_EVENTS, PLAN_STATES, PRIORITIES, SIZES, SOURCES, STATUSES,
                    DeskError, Store, handover_problem, location_commands, parse_build_state, split_handoff)

STATUS_LABEL = {
    "reported": "Reported", "open": "Open", "in_progress": "In progress", "to_check": "To check", "auto_check": "Auto check",
    "passed": "Passed", "failed": "Failed", "parked": "Parked", "closed": "Closed",
}
STEP_MARK = {"todo": "[ ]", "doing": "[~]", "done": "[x]", "dropped": "[-]"}


def _out_json(data) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _author(args) -> str:
    return getattr(args, "author", None) or os.environ.get("PAIR_DESK_AUTHOR") or "agent"


def _project(args, store: Store) -> str:
    if getattr(args, "project", None):
        return args.project
    linked = resolve_project(store.data_dir)
    if linked:
        return linked
    projects = store.list_projects()
    if len(projects) == 1:
        return projects[0]["slug"]
    raise DeskError("--project is required (this folder is not linked to a project; see `desk.py link`)")


def _read_attachments(paths) -> list[dict]:
    import base64
    out = []
    for p in paths or []:
        f = Path(p)
        out.append({"filename": f.name, "mime": mimetypes.guess_type(f.name)[0],
                    "data_base64": base64.b64encode(f.read_bytes()).decode("ascii")})
    return out


def _print_plan(issue: dict, indent: str = "  ") -> None:
    plan = issue.get("plan") or {}
    prog = issue.get("plan_progress") or {}
    if not plan.get("steps") and not plan.get("verification"):
        print(f"{indent}No plan yet.")
        return
    print(f"{indent}Plan {prog.get('done', 0)}/{prog.get('total', 0)}:")
    for n, st in enumerate(plan.get("steps", []), 1):
        extra = (f"  ({st['commit']})" if st.get("commit") else "") + (f"  -- {st['note']}" if st.get("note") else "")
        print(f"{indent}  {n}. {STEP_MARK.get(st['state'], '[?]')} {st['text']}{extra}")
    if plan.get("verification"):
        print(f"{indent}Verification: {plan['verification']}")


def _build_line(build: dict) -> str:
    """`label  path  (commit abc, built <time>)`; a version-string build is not repeated as its path."""
    parts = [build["label"]] + ([build["path"]] if build["path"] != build["label"] else [])
    extra = ([f"commit {build['commit']}"] if build.get("commit") else []) + [f"built {build['built_at']}"]
    return "  ".join(parts) + f"  ({', '.join(extra)})"


def _print_issue(issue: dict) -> None:
    if issue.get("redirected_from"):
        print(f"({issue['redirected_from']} was merged into {issue['id']})")
    print(f"{issue['id']}  {issue['title']}")
    print(f"  {STATUS_LABEL[issue['status']]} | {issue['kind']} | {issue['priority']}"
          + (f" | size {issue['size']}" if issue.get("size") else "")
          + (f" | milestone: {issue['milestone']}" if issue.get("milestone") else "")
          + (f" | area: {issue['area']}" if issue["area"] else "")
          + (f" | tags: {', '.join(issue['tags'])}" if issue["tags"] else ""))
    if issue.get("parent_info"):
        pi = issue["parent_info"]
        print(f"  part of {pi['id']} ({STATUS_LABEL[pi['status']]}): {pi['title']}")
    if issue.get("merged_into"):
        print(f"  merged into {issue['merged_into']}")
    print(f"  source: {issue['source']}  created {issue['created_at']}  updated {issue['updated_at']}"
          + (f"  ref: {issue['external_ref']}" if issue["external_ref"] else ""))
    if issue.get("build"):
        print(f"  build: {_build_line(issue['build'])}")
    loc = issue.get("location") or {}
    if loc:
        commands = location_commands(loc)
        for n, c in enumerate(commands, 1):
            head = "command" if len(commands) == 1 else f"command {n}"
            print(f"  {head}: {c['command']}" + (f"   ({c['label']})" if c.get("label") else ""))
        if loc.get("seed") is not None:
            print(f"  world seed: {loc['seed']}")
        rest = {k: v for k, v in loc.items() if k not in ("command", "commands")}
        if rest:
            print(f"  location: {json.dumps(rest, ensure_ascii=False)}")
    if issue.get("body"):
        print()
        for line in issue["body"].splitlines():
            print(f"  {line}")
    if issue.get("children"):
        print()
        print(f"  Children {issue['child_done']}/{issue['child_count']} done:")
        for ch in issue["children"]:
            print(f"    {ch['id']:<9} {STATUS_LABEL[ch['status']]:<12} {ch['title']}")
    if issue.get("merged_sources"):
        print(f"  Merged into this: {', '.join(m['id'] for m in issue['merged_sources'])}")
    if (issue.get("plan") or {}).get("steps"):
        print()
        _print_plan(issue)
    if issue.get("attachments"):
        print()
        for a in issue["attachments"]:
            print(f"  [attachment {a['id']}] {a['filename']} ({a['mime']}, {a['size']} bytes)")
    timeline = [(c["created_at"], 0, c) for c in issue.get("comments", [])]
    timeline += [(a["created_at"], 1, a) for a in issue.get("activity", []) if a["action"] != "created"]
    if timeline:
        print()
        for _, kind, e in sorted(timeline, key=lambda t: (t[0], t[1])):
            if kind == 0:
                verdict = f" [{e['verdict'].upper()}]" if e["verdict"] else ""
                print(f"  -- {e['author']}{verdict} {e['created_at']}")
                for line in (e["text"] or "").splitlines():
                    print(f"     {line}")
            elif e["action"] == "status":
                print(f"  .. {e['actor']} moved {e['detail'].get('from')} -> {e['detail'].get('to')} {e['created_at']}")
            elif e["action"] == "edited":
                print(f"  .. {e['actor']} edited {', '.join(e['detail'].get('fields', []))} {e['created_at']}")
            elif e["action"] == "command_sent":
                print(f"  .. {e['actor']} sent to game: {e['detail'].get('command')} {e['created_at']}")
            elif e["action"] == "command_delivered":
                print(f"  .. picked up by {e['detail'].get('client')} {e['created_at']}")
            elif e["action"] == "plan":
                d = e["detail"]
                what = (f"set the plan ({len(d.get('steps', []))} steps)" if d.get("op") == "set"
                        else f"step {d.get('index')}" + (f" {d.get('from')} -> {d.get('to')}" if d.get("to") else "")
                        + (f" commit {d['commit']}" if d.get("commit") else "")
                        + (f" note: {d['note']}" if d.get("note") else ""))
                print(f"  .. {e['actor']} plan: {what} {e['created_at']}")
            elif e["action"] == "merged":
                print(f"  .. {e['actor']} merged {e['detail'].get('from')} into this: {e['detail'].get('title')} {e['created_at']}")
            elif e["action"] == "build":
                d = e["detail"]
                print(f"  .. {e['actor']} build {d.get('label')}" + (f" (was {d['previous']})" if d.get("previous") else "")
                      + f" {e['created_at']}")
            elif e["action"] == "merged_into":
                print(f"  .. {e['actor']} merged this into {e['detail'].get('into')} {e['created_at']}")
            else:
                print(f"  .. {e['actor']} {e['action']} {json.dumps(e['detail'], ensure_ascii=False)} {e['created_at']}")


# -- commands -------------------------------------------------------------------------------

def _health(port: int, timeout: float = 0.6) -> dict | None:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if data.get("ok") else None
    except (OSError, ValueError):
        return None


def _pid_file(data_dir: Path) -> Path:
    return Path(data_dir) / "server.pid"


def _spawn_server(data_dir: Path, port: int, lan: bool = False):
    """Start the desk server as a detached background process that outlives its caller; returns the process."""
    import subprocess
    data_dir.mkdir(parents=True, exist_ok=True)
    log = open(data_dir / "server.log", "ab")
    cmd = [sys.executable, str(Path(__file__).resolve().parent.parent / "desk.py"), "--data", str(data_dir),
           "serve", "--port", str(port)]
    if lan:
        cmd.append("--lan")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(cmd, **kwargs)
    finally:
        log.close()


AUTOSTART_ENV = "PAIR_DESK_AUTOSTART"


def ensure_server(data_dir: Path) -> str:
    """Session start: make sure the web desk is up and current, without waiting for it. Not running: start it
    detached. Running an older version that this desk started (its pid file): stop it and start this one, so a
    plugin update or /reload-plugins takes effect. Another program on the port, or a newer desk, is left alone.
    Returns what it did (for tests and the log); `PAIR_DESK_AUTOSTART=0` turns it off."""
    import signal
    if os.environ.get(AUTOSTART_ENV, "1") == "0":
        return "disabled"
    port = int(os.environ.get("PAIR_DESK_PORT") or 8765)
    health = _health(port, 0.3)
    if health and health.get("version") == VERSION:
        return "running"
    if health:
        if _version_tuple(health.get("version")) >= _version_tuple(VERSION):
            return "newer-running"
        try:
            pid = int(_pid_file(data_dir).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return "foreign-running"
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        _pid_file(data_dir).unlink(missing_ok=True)
        import time
        for _ in range(20):
            if not _health(port, 0.1):
                break
            time.sleep(0.05)
    proc = _spawn_server(data_dir, port)
    _pid_file(data_dir).write_text(str(proc.pid), encoding="ascii")
    return "restarted" if health else "started"


def _version_tuple(text) -> tuple:
    try:
        return tuple(int(part) for part in str(text).split("."))
    except ValueError:
        return (0,)


def cmd_serve_detached(args, data_dir: Path) -> int:
    """Start the server as a background process that outlives this command, then return."""
    import time
    url = f"http://127.0.0.1:{args.port}/"
    if _health(args.port):
        print(f"{APP_NAME} is already running at {url}")
        return 0
    proc = _spawn_server(data_dir, args.port, args.lan)
    for _ in range(50):
        if _health(args.port, 0.3):
            _pid_file(data_dir).write_text(str(proc.pid), encoding="ascii")
            print(f"{APP_NAME} is running at {url} (pid {proc.pid}; log: {data_dir / 'server.log'})")
            if args.open:
                webbrowser.open(url)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    print(f"{APP_NAME} did not come up on port {args.port}; see {data_dir / 'server.log'}", file=sys.stderr)
    return 1


def cmd_stop(args, data_dir: Path) -> int:
    import signal
    pid_file = _pid_file(data_dir)
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        print("No background desk recorded (it was not started with `serve --detach`).")
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped {APP_NAME} (pid {pid})")
    except OSError as e:
        print(f"Process {pid} is not running ({e})")
    pid_file.unlink(missing_ok=True)
    return 0


def cmd_serve(args, data_dir: Path) -> int:
    if args.detach:
        return cmd_serve_detached(args, data_dir)
    from .server import make_server
    store = Store(data_dir)
    try:
        srv = make_server(store, args.port, lan=args.lan, verbose=args.verbose)
    except OSError as e:
        print(f"{APP_NAME}: cannot listen on port {args.port}: {e}", file=sys.stderr)
        print("Another desk may already be running there; open it, or pick another --port.", file=sys.stderr)
        store.close()
        return 2
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{APP_NAME} {VERSION} on {url}" + ("  (LAN access enabled)" if args.lan else ""))
    print(f"Data folder: {data_dir}")
    print("Ctrl+C to stop.")
    sys.stdout.flush()
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        store.close()
    return 0


def cmd_projects(args, store: Store) -> int:
    projects = store.list_projects()
    if args.json:
        _out_json({"projects": projects})
        return 0
    if not projects:
        print("No projects yet. Create one: desk.py new-project --slug mygame --name MyGame --prefix MG")
    for p in projects:
        c = p["counts"]
        seed = f"  default seed {p['default_seed']}" if p.get("default_seed") is not None else ""
        build = f"  build {p['build']['label']}" if p.get("build") else ("  no current build" if p.get("builds_enabled") else "")
        print(f"{p['slug']:<20} {p['prefix']:<6} {p['name']:<24} {p['issue_count']:>5} issues  "
              f"auto_check {c['auto_check']}, to_check {c['to_check']}, reported {c['reported']}, failed {c['failed']}{seed}{build}")
    return 0


def cmd_project_set(args, store: Store) -> int:
    """Project settings: name and default seed; `--backfill-seed` gives the default seed to existing
    agent work and checks that have none."""
    slug = _project(args, store)
    changes = {}
    if args.name is not None:
        changes["name"] = args.name
    if args.default_seed is not None:
        changes["default_seed"] = None if args.default_seed.strip().lower() in ("", "none", "clear") else args.default_seed
    if args.notify is not None:
        changes["notify"] = args.notify
    if args.lead is not None:
        changes["lead"] = args.lead
    p = store.update_project(slug, changes)
    result = {"project": p}
    if args.backfill_seed:
        if p["default_seed"] is None:
            raise DeskError("--backfill-seed needs a default seed (give --default-seed)")
        result["backfill"] = store.backfill_seed(slug, p["default_seed"], actor=_author(args))
    if args.json:
        _out_json(result)
        return 0
    seed = p["default_seed"] if p["default_seed"] is not None else "none"
    on = [k for k, v in p["notify"].items() if v]
    print(f"{p['slug']}: name {p['name']}, default seed {seed}, notify: {', '.join(on) or 'nothing'}, "
          f"lead: {p['lead'] or 'every agent'}")
    if "backfill" in result:
        changed = result["backfill"]["changed"]
        print(f"Gave seed {p['default_seed']} to {len(changed)} issue{'s' if len(changed) != 1 else ''}"
              + (f": {', '.join(changed)}" if changed else ""))
    return 0


def cmd_new_project(args, store: Store) -> int:
    p = store.create_project(args.slug, args.name, args.prefix)
    if args.json:
        _out_json(p)
    else:
        print(f"Created project {p['slug']} ({p['name']}), ids look like {p['prefix']}-1")
    return 0


def cmd_link(args, store: Store) -> int:
    store.get_project(args.project)
    f = save_link(store.data_dir, args.path or os.getcwd(), args.project)
    print(f"Linked {os.path.abspath(args.path or os.getcwd())} to {args.project} ({f})")
    return 0


def cmd_list(args, store: Store) -> int:
    filters = {k: getattr(args, k) for k in ("status", "kind", "area", "priority", "q", "since", "sort", "limit", "tag",
                                             "size", "milestone") if getattr(args, k, None)}
    if args.seed is not None:
        filters["seed"] = args.seed
    res = store.list_issues(_project(args, store), filters)
    if args.json:
        _out_json(res)
        return 0
    for i in res["issues"]:
        verdict = f" [{i['last_verdict']}]" if i.get("last_verdict") else ""
        prog = i.get("plan_progress") or {}
        plan = f" [plan {prog['done']}/{prog['total']}]" if prog.get("total") else ""
        part = f" (part of {i['parent']})" if i.get("parent") else ""
        print(f"{i['id']:<9} {STATUS_LABEL[i['status']]:<12} {i['priority']} {(i.get('size') or '-'):<1} {i['kind']:<6} "
              f"{(i['area'] or '-')[:16]:<16} {i['title']}{verdict}{plan}{part}")
    shown = len(res["issues"])
    print(f"-- {shown} of {res['total']}" if res["total"] > shown else f"-- {res['total']} issues")
    return 0


def cmd_show(args, store: Store) -> int:
    issue = store.get_issue(args.id)
    if args.json:
        _out_json(issue)
    else:
        _print_issue(issue)
    return 0


def _body_arg(args) -> str | None:
    if getattr(args, "body_file", None):
        return Path(args.body_file).read_text(encoding="utf-8")
    return getattr(args, "body", None)


def cmd_add(args, store: Store) -> int:
    data = {
        "title": args.title, "kind": args.kind, "status": args.status, "priority": args.priority,
        "area": args.area, "body": _body_arg(args), "external_ref": args.ref, "tags": args.tags,
        "source": args.source or "agent", "attachments": _read_attachments(args.attach),
        "size": args.size, "milestone": args.milestone,
    }
    if args.command:
        data["commands"] = _command_list(args.command, args.label)
    elif args.label:
        raise DeskError("--label names a --command: give the commands too")
    if args.location:
        data["location"] = json.loads(args.location)
    if args.seed is not None:
        data["location"] = {**(data.get("location") or {}), "seed": args.seed}
    if args.action:
        data["location"] = {**(data.get("location") or {}), "action": args.action}
    data = {k: v for k, v in data.items() if v not in (None, [], "")}
    if not data.get("status") and data.get("kind") == "check":
        data["status"] = "to_check"
    issue = store.create_issue(_project(args, store), data, actor=_author(args))
    if args.parent:
        issue = store.link_parent(issue["id"], args.parent, actor=_author(args))
    if args.json:
        _out_json(issue)
    else:
        print(f"Created {issue['id']}: {issue['title']} ({issue['status']})")
    return 0


def _command_list(commands: list[str], labels: list[str] | None, previous: list[dict] | None = None) -> list[dict]:
    """`--command` values in order, each with the `--label` at the same position. A command without a label
    keeps the label it already had on the issue (same text)."""
    labels = labels or []
    if len(labels) > len(commands):
        raise DeskError(f"{len(labels)} --label for {len(commands)} --command: each label names the command at its position")
    known = {c["command"]: c.get("label") for c in previous or []}
    out = []
    for n, command in enumerate(commands):
        label = labels[n] if n < len(labels) else known.get(command.strip())
        out.append({"command": command, **({"label": label} if label else {})})
    return out


def _edited_commands(args, store: Store) -> list[dict] | None:
    """The command list an edit sets: every `--command` in order (replacing the list), or with `--at N` the one
    `--command` replacing command N (N one past the end appends)."""
    if not args.command:
        if args.label or args.at is not None:
            raise DeskError("--label and --at name a --command: give the command too")
        return None
    current = location_commands(store.get_issue(args.id, full=False)["location"], seeded=False)
    if args.at is None:
        return _command_list(args.command, args.label, current)
    if len(args.command) != 1 or len(args.label or []) > 1:
        raise DeskError("--at replaces one command: give one --command (and at most one --label)")
    if not 1 <= args.at <= len(current) + 1:
        raise DeskError(f"--at {args.at}: the issue has {len(current)} command(s); pick 1 to {len(current) + 1}")
    old = current[args.at - 1] if args.at <= len(current) else {}
    label = (args.label or [old.get("label")])[0]
    entry = {"command": args.command[0], **({"label": label} if label else {})}
    return [*current[:args.at - 1], entry, *current[args.at:]]


def cmd_edit(args, store: Store) -> int:
    changes = {}
    for field, attr in (("title", "title"), ("kind", "kind"), ("priority", "priority"), ("area", "area"),
                        ("external_ref", "ref"), ("tags", "tags"), ("source", "source"),
                        ("size", "size"), ("milestone", "milestone")):
        val = getattr(args, attr, None)
        if val is not None:
            changes[field] = val
    body = _body_arg(args)
    if body is not None:
        changes["body"] = body
    commands = _edited_commands(args, store)
    if commands is not None:
        changes["commands"] = commands
    if args.location:
        changes["location"] = json.loads(args.location)
    if args.seed is not None:
        base = changes.get("location") or store.get_issue(args.id, full=False)["location"]
        changes["location"] = {**base, "seed": args.seed}
    if args.action is not None:
        base = changes.get("location") or store.get_issue(args.id, full=False)["location"]
        changes["location"] = {**{k: v for k, v in base.items() if k != "action"},
                               **({"action": args.action} if args.action.strip() else {})}
    issue = store.update_issue(args.id, changes, actor=_author(args))
    if args.parent is not None:
        parent = None if args.parent.strip().lower() in ("", "none") else args.parent
        issue = store.link_parent(issue["id"], parent, actor=_author(args))
    if args.json:
        _out_json(issue)
    else:
        print(f"Updated {issue['id']}")
    return 0


def cmd_comment(args, store: Store) -> int:
    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    res = store.add_comment(args.id, _author(args), text, args.verdict, _read_attachments(args.attach))
    if args.json:
        _out_json(res)
    else:
        print(f"Commented on {res['issue']['id']} (status {res['issue']['status']})")
    return 0


def cmd_status(args, store: Store) -> int:
    if args.status == "to_check" and _author(args) != "owner" and not getattr(args, "force", False):
        current = store.get_issue(args.id, full=False)
        problem = handover_problem(current, store.get_project(current["project"]))
        if problem:
            print(problem, file=sys.stderr)
            return 2
    issue = store.set_status(args.id, args.status, actor=_author(args))
    build = f" (build {issue['build']['label']})" if issue["status"] == "to_check" and issue.get("build") else ""
    print(f"{issue['id']} is now {issue['status']}{build}")
    return 0


def cmd_build(args, store: Store) -> int:
    """The project's current build: show it, set (publish) one, clear it, or open its folder / run it here."""
    if args.action in ("open", "run"):
        from . import launch
        build = (store.get_issue(args.issue, full=False) if args.issue else store.get_build(_project(args, store)))["build"]
        if not build:
            raise DeskError(f"{args.issue or 'the project'} has no build to {args.action}")
        res = (launch.reveal if args.action == "open" else launch.run)(build["path"])
        print(f"Started {res['started']}" if args.action == "run" else f"Opened {res['folder']}")
        return 0
    slug = _project(args, store)
    if args.action == "set":
        if not args.path:
            raise DeskError("give --path: the player exe or build folder, or the release's version string")
        res = store.set_build(slug, args.path, args.commit, args.label, args.built_at, _author(args))
        if args.json:
            _out_json(res)
            return 0
        b = res["build"]
        print(f"{'Build ' + b['label'] + ' is already' if res['unchanged'] else 'Published build ' + b['label'] + ','} "
              f"current for {res['project']}: {_build_line(b)}")
        n = len(res["stamped"])
        print(f"Stamped on {n} to_check issue{'s' if n != 1 else ''}" + (f": {', '.join(res['stamped'])}" if n else ""))
        return 0
    if args.action == "clear":
        res = store.clear_build(slug, off=args.off)
    else:
        res = store.get_build(slug)
    if args.json:
        _out_json(res)
    elif res["build"]:
        print(f"{res['project']} build: {_build_line(res['build'])}")
    elif res["builds_enabled"]:
        print(f"{res['project']} has no current build: to_check waits for the next one (build set --path ...).")
    else:
        print(f"{res['project']} does not use builds. Publish one with: build set --path <player or version> --commit <hash>")
    return 0


def cmd_attach(args, store: Store) -> int:
    for p in args.files:
        f = Path(p)
        a = store.add_attachment(args.id, f.name, mimetypes.guess_type(f.name)[0], f.read_bytes(),
                                 actor=_author(args))
        print(f"Attached {a['filename']} as attachment {a['id']}")
    return 0


def cmd_send(args, store: Store) -> int:
    command = args.command
    slug = args.project
    if args.issue:
        issue = store.get_issue(args.issue, full=False)
        slug = slug or issue["project"]
        if not command:
            commands = location_commands(issue["location"])
            if commands and not 1 <= args.at <= len(commands):
                raise DeskError(f"--at {args.at}: {issue['id']} has {len(commands)} location command(s)")
            command = commands[args.at - 1]["command"] if commands else None
    if not command:
        raise DeskError("nothing to send: give --command, or --issue with a location command")
    cmd = store.queue_command(slug or _project(args, store), command, args.issue, _author(args))
    print(f"Queued command {cmd['id']} (expires {cmd['expires_at']}): {cmd['command']}")
    return 0


def cmd_import(args, store: Store) -> int:
    with open(args.file, encoding="utf-8") as fh:
        payload = json.load(fh)
    # attachment_paths in the file resolve against the current folder, then the file's own folder.
    base = [os.getcwd(), str(Path(args.file).resolve().parent)]
    summary = store.import_data(payload, default_project=args.project, actor=_author(args), path_base=base)
    if args.json:
        _out_json(summary)
    else:
        print(f"Created {len(summary['created'])}, skipped {len(summary['skipped'])} duplicates, "
              f"{len(summary['errors'])} errors")
        for e in summary["errors"]:
            print(f"  item {e['index']}: {e['error']}")
    return 1 if summary["errors"] else 0


def cmd_export(args, store: Store) -> int:
    data = store.export_project(_project(args, store))
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"Exported {len(data['issues'])} issues to {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def _read_steps(args) -> list | None:
    """Plan steps from --step (repeatable) or --steps-file (JSON list, or one step per line with
    optional `- `, `1.` or `[x]` markers)."""
    if args.steps_file:
        raw = Path(args.steps_file).read_text(encoding="utf-8")
        if raw.lstrip().startswith("["):
            return json.loads(raw)
        import re
        steps = []
        marks = {"x": "done", "X": "done", "~": "doing", "-": "dropped", " ": "todo"}
        for line in raw.splitlines():
            m = re.match(r"^\s*(?:[-*]|\d+[.)])?\s*(?:\[([ xX~-])\])?\s*(.+?)\s*$", line)
            if m and m.group(2):
                steps.append({"text": m.group(2), "state": marks.get(m.group(1) or " ", "todo")})
        return steps
    return list(args.step) if args.step else None


def cmd_plan(args, store: Store) -> int:
    steps = _read_steps(args)
    if steps is None and args.verification is None:
        issue = store.get_issue(args.id)
    elif steps is None:
        issue = store.get_issue(args.id)
        issue = store.set_plan(args.id, issue["plan"]["steps"], args.verification, actor=_author(args))
    else:
        issue = store.set_plan(args.id, steps, args.verification, actor=_author(args))
    if args.json:
        _out_json({"id": issue["id"], "plan": issue["plan"], "progress": issue["plan_progress"]})
    else:
        print(f"{issue['id']}  {issue['title']}")
        _print_plan(issue)
    return 0


def cmd_step(args, store: Store) -> int:
    issue = store.update_step(args.id, args.index, args.state, args.commit, args.note, args.text, actor=_author(args))
    if args.json:
        _out_json({"id": issue["id"], "plan": issue["plan"], "progress": issue["plan_progress"]})
    else:
        prog = issue["plan_progress"]
        st = issue["plan"]["steps"][args.index - 1]
        print(f"{issue['id']} step {args.index}: {st['state']} ({prog['done']}/{prog['total']} done)")
    return 0


def cmd_parent(args, store: Store) -> int:
    parent = None if args.none or not args.parent else args.parent
    issue = store.link_parent(args.child, parent, actor=_author(args))
    print(f"{issue['id']} is now part of {issue['parent']}" if issue.get("parent") else f"{issue['id']} has no parent")
    return 0


def cmd_merge(args, store: Store) -> int:
    res = store.merge_issues(args.target, args.sources, actor=_author(args))
    if args.json:
        _out_json(res)
    else:
        print(f"Merged {', '.join(res['merged'])} into {res['target']}")
    return 0


def cmd_unmerge(args, store: Store) -> int:
    res = store.unmerge(args.id, actor=_author(args))
    print(f"Unmerged {res['source']} from {res['target']} (status {res['issue']['status']})")
    return 0


def cmd_suggest_groups(args, store: Store) -> int:
    res = store.suggest_groups(_project(args, store), args.limit or 20)
    if args.json:
        _out_json(res)
        return 0
    if not res["groups"]:
        print("No groups to suggest.")
    for g in res["groups"]:
        print(f"{g['action'].upper():<6} into {g['target']}  (score {g['score']}): {', '.join(g['issues'])}")
        for i in g["issues"]:
            print(f"    {i:<9} {g['titles'][i]}")
        print(f"    why: {'; '.join(g['reasons'])}")
    return 0


def cmd_handoff(args, store: Store) -> int:
    slug = _project(args, store)
    action = args.action
    if action == "show":
        h = store.get_handoff(slug, args.version)
        if args.json:
            _out_json(h)
        elif not h["version"]:
            print(f"No handoff for {slug} yet. Write one: desk.py handoff set --file HANDOFF.md")
        else:
            print(f"# Handoff {h['project']} v{h['version']} by {h['author']} at {h['created_at']}\n")
            sys.stdout.write(h["markdown"])
        return 0
    if action == "set":
        text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
        if text is None:
            raise DeskError("give --file or --text with the handoff markdown")
        h = store.set_handoff(slug, text, _author(args), args.note)
        print(f"Saved handoff {slug} v{h['version']}")
        return 0
    if action == "section":
        if not args.section:
            raise DeskError("give --section (" + ", ".join(HANDOFF_SECTIONS) + ")")
        text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
        if text is None:
            raise DeskError("give --text or --file with the section text")
        h = store.update_handoff(slug, args.section, text, _author(args))
        print(f"Saved handoff {slug} v{h['version']}")
        return 0
    if action == "history":
        res = store.handoff_history(slug)
        if args.json:
            _out_json(res)
        for v in res["versions"]:
            if not args.json:
                print(f"v{v['version']:<4} {v['created_at']}  {v['author']:<12} {v['size']:>6} chars  {v['note']}")
        return 0
    if action == "diff":
        if args.version is None:
            raise DeskError("give --version N (and optionally --to M) to diff")
        res = store.handoff_diff(slug, args.version, args.to)
        if args.json:
            _out_json(res)
        else:
            print(res["diff"] or "(no differences)")
        return 0
    raise DeskError(f"unknown handoff action {action}")


def cmd_mcp(args, data_dir: Path) -> int:
    from .mcp import McpServer
    McpServer(data_dir).serve()
    return 0


def session_summary(data_dir: Path, cwd: str | None) -> str | None:
    """One line for the SessionStart hook. Never creates anything; returns None when there is
    nothing set up for this folder."""
    db = Path(data_dir) / "desk.sqlite"
    if not db.is_file():
        return None
    slug = resolve_project(data_dir, cwd)
    if not slug:
        return None
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        row = conn.execute("SELECT id, name FROM projects WHERE slug=?", (slug,)).fetchone()
        if not row:
            return None
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM issues WHERE project_id=? GROUP BY status",
                                   (row[0],)).fetchall())
    finally:
        conn.close()
    reported, failed, waiting = counts.get("reported", 0), counts.get("failed", 0), counts.get("to_check", 0)
    automatic = counts.get("auto_check", 0)
    if not (reported or failed or waiting or automatic):
        return f"Pair Desk ({row[1]}): nothing new, no failed checks, nothing waiting for the owner."
    parts = [f"{reported} new report{'s' if reported != 1 else ''}",
             f"{failed} failed check{'s' if failed != 1 else ''}",
             f"{waiting} waiting for the owner",
             f"{automatic} auto_check waiting for agent verification (screenshots, logs, tests)"]
    return f"Pair Desk ({row[1]}): " + ", ".join(parts)


def handoff_lines(data_dir: Path, cwd: str | None) -> list[str]:
    """The handoff's Next step and Traps for the SessionStart context (read-only; [] when none)."""
    db = Path(data_dir) / "desk.sqlite"
    slug = resolve_project(data_dir, cwd) if db.is_file() else None
    if not slug:
        return []
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='handoffs'").fetchone():
            return []
        row = conn.execute("SELECT h.version, h.markdown, h.author, h.created_at FROM handoffs h JOIN projects p "
                           "ON p.id=h.project_id WHERE p.slug=? ORDER BY h.version DESC LIMIT 1", (slug,)).fetchone()
    finally:
        conn.close()
    if not row:
        return ["Pair Desk handoff: none written yet for this project (set_handoff before you stop)."]
    sections = dict(split_handoff(row[1])[1])
    out = [f"Pair Desk handoff v{row[0]} ({row[2]}, {row[3][:16].replace('T', ' ')} UTC):"]
    for name in ("Next step", "Traps"):
        body = " ".join((sections.get(name) or "").split())
        if body:
            out.append(f"- {name}: {body[:600]}" + ("…" if len(body) > 600 else ""))
    return out


def build_lines(data_dir: Path, cwd: str | None) -> list[str]:
    """The project's current build for the SessionStart context (read-only; [] when the project does not use
    builds, or the desk predates them)."""
    db = Path(data_dir) / "desk.sqlite"
    slug = resolve_project(data_dir, cwd) if db.is_file() else None
    if not slug:
        return []
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=2)
    try:
        row = conn.execute("SELECT build FROM projects WHERE slug=?", (slug,)).fetchone()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    state = parse_build_state(row[0] if row else None)
    if not state:
        return []
    b = state["current"]
    if not b:
        return ["Pair Desk build: none current, so to_check is refused until the next build is published (set_build)."]
    return [f"Pair Desk build: {_build_line(b)} is current; the desk stamps it on to_check issues. Publish each new "
            "build with set_build (never paste the path into comments)."]


def _hook_input() -> dict:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError, AttributeError):
        return {}


def cmd_hook(args, data_dir: Path) -> int:
    """Plugin hooks. Must never block or fail the session, and never create a data folder.

    session-start: the counts line plus the handoff's next step and traps, and it starts this
    session's notification cursor. user-prompt-submit: owner activity since the session last
    looked (see notify.py).

    An agent-bridge delegated job (notify.delegated_job) gets one line pointing it at its brief and no
    owner activity; when the project names a lead client, only that client's top-level sessions get
    owner activity. So one session reacts to the owner instead of all of them."""
    try:
        data = _hook_input()
        cwd, session_id = data.get("cwd"), data.get("session_id")
        client = getattr(args, "client", "claude")
        if client == "antigravity":
            paths = data.get("workspacePaths")
            if not isinstance(paths, list) or not paths or not isinstance(paths[0], str):
                return 0
            cwd, session_id = paths[0], data.get("conversationId")
        elif client == "muse":
            session_id = session_id or os.environ.get("MUSE_SESSION_ID")
        if client != "claude" and session_id:
            session_id = f"{client}:{session_id}"

        def emit_context(event: str, context: str, line: str | None = None) -> None:
            if client == "antigravity":
                _out_json({"injectSteps": [{"systemMessage": {"systemMessage": context}}]})
            else:
                output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}
                if line:
                    output["systemMessage"] = line
                _out_json(output)
        job = notify.delegated_job()
        if args.event == "session-start":
            # The web desk starts with the session (and is replaced when an older version runs), never blocking it.
            try:
                if (Path(data_dir) / "desk.sqlite").is_file():
                    ensure_server(Path(data_dir))
            except Exception:  # noqa: BLE001 - autostart must never break the session
                pass
            line = session_summary(data_dir, cwd)
            if line and job:
                emit_context("SessionStart", f"{line}. You are a delegated agent-bridge job ({job}): work from your "
                             "brief and use the desk only for the issues it names. Leave triage, new reports and "
                             "owner verdicts to the session that delegated you.")
            elif line:
                slug = resolve_project(data_dir, cwd)
                with Store(data_dir) as store:
                    lead = store.get_project(slug).get("lead")
                    notify.mark_seen(data_dir, slug, session_id, store.event_cursor())
                lines = [line + ". Read the handoff (get_handoff) and failed and reported items "
                                "(list_issues) before starting; see the pair-desk skill."]
                if not notify.receives_activity(lead, client):
                    lines.append(f"Pair Desk lead: the {lead} session handles the owner's new activity for this "
                                 "project; leave new reports and verdicts to it unless the owner asks you.")
                emit_context("SessionStart", "\n".join([*lines, *build_lines(data_dir, cwd),
                                                        *handoff_lines(data_dir, cwd)]), line)
        elif args.event == "user-prompt-submit":
            if job or not (Path(data_dir) / "desk.sqlite").is_file():
                return 0
            slug = resolve_project(data_dir, cwd)
            if not slug:
                return 0
            with Store(data_dir) as store:
                try:
                    project = store.get_project(slug)
                except DeskError:
                    return 0
                if not notify.receives_activity(project.get("lead"), client):
                    return 0
                block = notify.prompt_context(store, slug, session_id)
            if block:
                emit_context("UserPromptSubmit", block)
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0


# -- parser ---------------------------------------------------------------------------------

class _DeskArgumentParser(argparse.ArgumentParser):
    """argparse with errors that stay readable: a long value (a markdown body passed with a flag the command does
    not take) is shortened in the message, so the error is not lost behind the echoed text (PD-2)."""

    def error(self, message):
        first, _, rest = message.partition("\n")
        if len(first) > 200 or rest:
            message = first[:200] + f"... ({len(message)} characters; see --help for the flags this command takes)"
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default=argparse.SUPPRESS, help="data folder (default: %%LOCALAPPDATA%%\\AgentPairProgramming or ~/.agent-pair-programming)")

    p = _DeskArgumentParser(prog="pair-desk", description=f"{APP_NAME} {VERSION}: local playtest and issue desk",
                            parents=[common])
    p.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND", parser_class=_DeskArgumentParser)

    def add(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[common], description=help_text)

    s = add("serve", "run the web UI and HTTP API")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--lan", action="store_true", help="listen on all interfaces and accept LAN clients")
    s.add_argument("--open", action="store_true", help="open the browser")
    s.add_argument("--verbose", action="store_true", help="log every request")
    s.add_argument("--detach", action="store_true", help="start in the background and return (see `stop`)")

    add("stop", "stop a desk started with `serve --detach`")

    s = add("projects", "list projects")
    s.add_argument("--json", action="store_true")

    s = add("new-project", "create a project (a game)")
    s.add_argument("--slug", required=True)
    s.add_argument("--name")
    s.add_argument("--prefix", help="issue id prefix, e.g. MG")
    s.add_argument("--json", action="store_true")

    s = add("project-set", "change a project's settings (name, default world seed)")
    s.add_argument("--project", help="project slug (default: the linked project)")
    s.add_argument("--name")
    s.add_argument("--default-seed", help="world seed agent checks inherit when they give none; 'none' clears it")
    s.add_argument("--backfill-seed", action="store_true",
                   help="also give the default seed to existing agent work and checks without one")
    s.add_argument("--notify", help="owner events that notify agent sessions, comma separated: "
                                    + ",".join(NOTIFY_EVENTS) + " (or 'none')")
    s.add_argument("--lead", help="the agent client whose top-level sessions alone get owner activity: "
                                  + ",".join(LEAD_CLIENTS) + " (or 'none' for every agent)")
    s.add_argument("--author")
    s.add_argument("--json", action="store_true")

    s = add("link", "link a repo folder to a project (for the hook and the MCP server)")
    s.add_argument("--project", required=True)
    s.add_argument("--path", help="folder to link (default: current folder)")

    s = add("list", "list issues")
    s.add_argument("--project")
    s.add_argument("--status", help="one or more, comma separated: " + ",".join(STATUSES))
    s.add_argument("--kind")
    s.add_argument("--area")
    s.add_argument("--priority")
    s.add_argument("--tag")
    s.add_argument("--seed", help="only issues whose location is in this world seed")
    s.add_argument("--q", "--search", dest="q")
    s.add_argument("--since", help="ISO time; updated at or after")
    s.add_argument("--sort", choices=["triage", "updated", "created", "oldest", "priority", "number", "backlog"])
    s.add_argument("--size", help="S, M, L or none (comma separated for several)")
    s.add_argument("--milestone")
    s.add_argument("--limit", type=int)
    s.add_argument("--json", action="store_true")

    s = add("show", "show one issue with comments and activity")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")

    def issue_fields(s, creating: bool):
        s.add_argument("--title", required=creating)
        s.add_argument("--kind", choices=KINDS)
        if creating:
            s.add_argument("--status", choices=STATUSES)
        s.add_argument("--priority", choices=PRIORITIES)
        s.add_argument("--area")
        # --text / --text-file as on `comment`: one name for an issue's markdown across commands (PD-2).
        s.add_argument("--body", "--text", dest="body", help="markdown body (--text is the same)")
        s.add_argument("--body-file", "--text-file", dest="body_file", help="read the markdown body from a file")
        s.add_argument("--command", action="append",
                       help="a game console command that takes the player to ONE place; repeat it for further places "
                            "(each its own command, in order)" + ("" if creating else "; replaces the list unless --at"))
        s.add_argument("--label", action="append",
                       help="a few words naming the place of the --command at the same position, e.g. 'the capital'")
        if not creating:
            s.add_argument("--at", type=int, metavar="N", help="replace only command N (1-based; one past the end appends)")
        s.add_argument("--location", help='JSON, e.g. {"x":1,"y":2,"z":3,"place":"Harbor","seed":1234}')
        s.add_argument("--seed", help="world seed of the location (default for agent checks: the project's)")
        s.add_argument("--action", help="for a check that is an action, not a place: what the owner does "
                                        "('Continue from the title', 'Quit the game'); stands in for a --command ('' clears)")
        s.add_argument("--ref", help="external reference (TODO.md item title, commit hash)")
        s.add_argument("--tags", help="comma separated")
        s.add_argument("--source", choices=SOURCES)
        s.add_argument("--size", help="effort estimate: " + ", ".join(SIZES) + " ('' clears)")
        s.add_argument("--milestone", help="milestone or group in the backlog")
        s.add_argument("--parent", help="the issue this one is part of" + ("" if creating else " ('none' unlinks)"))
        s.add_argument("--author")
        s.add_argument("--json", action="store_true")

    s = add("add", "create an issue")
    s.add_argument("--project")
    issue_fields(s, True)
    s.add_argument("--attach", nargs="*", help="files to attach")

    s = add("edit", "edit issue fields")
    s.add_argument("id")
    issue_fields(s, False)

    s = add("comment", "comment on an issue (optionally with a verdict)")
    s.add_argument("id")
    s.add_argument("--author")
    s.add_argument("--text")
    s.add_argument("--text-file")
    s.add_argument("--verdict", choices=["passed", "failed"])
    s.add_argument("--attach", nargs="*")
    s.add_argument("--json", action="store_true")

    s = add("status", "set an issue's status")
    s.add_argument("id")
    s.add_argument("status", choices=STATUSES)
    s.add_argument("--force", action="store_true", help="hand an issue to the owner (to_check) without the plan and location checks")
    s.add_argument("--author")

    s = add("build", "the project's current build: show, set (publish; stamps every to_check issue), clear, "
                     "open (its folder) or run (the player) on this machine")
    s.add_argument("action", nargs="?", default="show", choices=["show", "set", "clear", "open", "run"])
    s.add_argument("--issue", help="open / run: the build stamped on this issue instead of the current one")
    s.add_argument("--project")
    s.add_argument("--path", help="set: the player exe or build folder, or a version string for a release")
    s.add_argument("--commit", help="set: the commit the build was made from")
    s.add_argument("--label", help="set: a short name (default: a version string itself, else the short commit)")
    s.add_argument("--built-at", help="set: when it was built, ISO time (default: now)")
    s.add_argument("--off", action="store_true", help="clear: stop using builds for this project")
    s.add_argument("--author")
    s.add_argument("--json", action="store_true")

    s = add("attach", "attach files to an issue")
    s.add_argument("id")
    s.add_argument("files", nargs="+")
    s.add_argument("--author")

    s = add("send", "queue a command for the running game")
    s.add_argument("--project")
    s.add_argument("--issue", help="use this issue's location command (and link the command to it)")
    s.add_argument("--at", type=int, default=1, metavar="N", help="with --issue: send its command N (default 1)")
    s.add_argument("--command")
    s.add_argument("--author")

    s = add("import-json", "bulk create issues from a JSON file (skips duplicate external_ref)")
    s.add_argument("file")
    s.add_argument("--project", help="default project for items without one")
    s.add_argument("--author")
    s.add_argument("--json", action="store_true")

    s = add("export", "export a project with comments, attachments metadata and activity as JSON")
    s.add_argument("--project")
    s.add_argument("--json", action="store_true", help="accepted for symmetry; export is always JSON")
    s.add_argument("--out", help="write to this file instead of stdout")

    s = add("plan", "show or write an issue's plan (steps and a verification line)")
    s.add_argument("id")
    s.add_argument("--step", action="append", help="a plan step; repeat for each, in order (replaces the plan)")
    s.add_argument("--steps-file", help="JSON list of steps, or one step per line (`- [x] text` marks done)")
    s.add_argument("--verification", help="what proves it works")
    s.add_argument("--author")
    s.add_argument("--json", action="store_true")

    s = add("step", "update one plan step (1 = first)")
    s.add_argument("id")
    s.add_argument("index", type=int)
    s.add_argument("state", nargs="?", choices=PLAN_STATES)
    s.add_argument("--commit")
    s.add_argument("--note")
    s.add_argument("--text")
    s.add_argument("--author")
    s.add_argument("--json", action="store_true")

    s = add("parent", "make CHILD part of PARENT (or --none to unlink)")
    s.add_argument("child")
    s.add_argument("parent", nargs="?")
    s.add_argument("--none", action="store_true")
    s.add_argument("--author")

    s = add("merge", "merge SOURCES into TARGET (timelines move, sources close and redirect)")
    s.add_argument("target")
    s.add_argument("sources", nargs="+")
    s.add_argument("--author")
    s.add_argument("--json", action="store_true")

    s = add("unmerge", "undo a merge of this issue")
    s.add_argument("id")
    s.add_argument("--author")

    s = add("suggest-groups", "propose merges and groups of related open items (nothing is applied)")
    s.add_argument("--project")
    s.add_argument("--limit", type=int)
    s.add_argument("--json", action="store_true")

    s = add("handoff", "the project handoff: show, set, section, history, diff")
    s.add_argument("action", nargs="?", default="show", choices=["show", "set", "section", "history", "diff"])
    s.add_argument("--project")
    s.add_argument("--file", help="markdown file (set, section)")
    s.add_argument("--text", help="markdown text (set, section)")
    s.add_argument("--section", help="section name for `section`: " + ", ".join(HANDOFF_SECTIONS))
    s.add_argument("--note", help="what changed (set)")
    s.add_argument("--version", type=int, help="version to show, or the old side of a diff")
    s.add_argument("--to", type=int, help="new side of a diff (default: latest)")
    s.add_argument("--author")
    s.add_argument("--json", action="store_true")

    from integrations.installer import add_arguments as add_installer_arguments
    add_installer_arguments(sub, common)

    add("mcp", "run the stdio MCP server (used by the Claude Code plugin)")
    s = add("hook", "plugin hook entry point")
    s.add_argument("event", choices=["session-start", "user-prompt-submit"])
    s.add_argument("--client", choices=["claude", "codex", "antigravity", "muse", "opencode"], default="claude",
                   help="the calling agent: its native hook format (Codex and opencode use Claude Code's) and "
                        "the name a project's lead setting matches")
    s = add("outbox", "replay desk writes queued where the desk was read-only (a sandboxed agent job, PD-4)")
    s.add_argument("action", choices=["show", "replay"])
    s.add_argument("path", help="the outbox file (.cache/pair-desk-outbox.jsonl in the job's worktree)")
    return p


# -- outbox (PD-4) ------------------------------------------------------------------------------
# A sandboxed agent job (Codex workspace-write) may read the desk but not write it: SQLite answers "attempt to
# write a readonly database". Such a write is not lost: the CLI appends it to an outbox inside the job's own
# workspace, and the session that merges the job replays it with `desk.py outbox replay <file>`.

READ_ONLY_COMMANDS = {"projects", "list", "show"}
OUTBOX_ENV = "PAIR_DESK_OUTBOX"


def _read_only_error(e: Exception) -> bool:
    text = str(e).lower()
    return "readonly" in text or "read-only" in text or "unable to open" in text or "disk i/o" in text


def _outbox_path() -> Path:
    forced = os.environ.get(OUTBOX_ENV)
    if forced:
        return Path(forced)
    here = Path.cwd()
    for folder in [here, *here.parents]:
        if (folder / ".git").exists():
            return folder / ".cache" / "pair-desk-outbox.jsonl"
    return here / ".cache" / "pair-desk-outbox.jsonl"


def _queue_in_outbox(argv: list[str], error: Exception) -> int:
    from datetime import datetime, timezone
    path = _outbox_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"argv": list(argv), "cwd": str(Path.cwd()),
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "error": str(error)}, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"error: the desk is read-only here ({error}) and the outbox could not be written ({e}); "
              "put the desk text in your final report instead", file=sys.stderr)
        return 1
    print(f"The desk is read-only here; queued in {path}. The session that merges this job replays it "
          f"(desk.py outbox replay {path}). Mention the outbox in your final report.")
    return 0


def cmd_outbox(args, data_dir: Path) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"No outbox at {path}")
        return 0
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry.get("argv"), list):
                raise ValueError("no argv")
            entries.append(entry)
        except ValueError as e:
            print(f"line {number}: unreadable ({e})", file=sys.stderr)
    if args.action == "show":
        for entry in entries:
            print(entry.get("at", "?"), " ".join(entry["argv"])[:200])
        print(f"-- {len(entries)} queued writes")
        return 0
    failed = 0
    for entry in entries:
        argv = [a for a in entry["argv"] if a != "outbox"]
        code = main(argv)
        if code != 0:
            failed += 1
            print(f"replay failed ({code}): {' '.join(argv)[:200]}", file=sys.stderr)
    if failed == 0:
        from datetime import datetime
        done = path.with_name(path.name + ".replayed-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        path.rename(done)
        print(f"Replayed {len(entries)} writes; outbox moved to {done}")
        return 0
    print(f"Replayed {len(entries) - failed} of {len(entries)}; the outbox stays for another try", file=sys.stderr)
    return 1


STORE_COMMANDS = {
    "projects": cmd_projects, "new-project": cmd_new_project, "project-set": cmd_project_set, "link": cmd_link,
    "list": cmd_list,
    "show": cmd_show, "add": cmd_add, "edit": cmd_edit, "comment": cmd_comment, "status": cmd_status, "build": cmd_build,
    "attach": cmd_attach, "send": cmd_send, "import-json": cmd_import, "export": cmd_export,
    "plan": cmd_plan, "step": cmd_step, "parent": cmd_parent, "merge": cmd_merge, "unmerge": cmd_unmerge,
    "suggest-groups": cmd_suggest_groups, "handoff": cmd_handoff,
}
def cmd_installer(args, data_dir: Path) -> int:
    from integrations.installer import run
    return run(args, data_dir)


RAW_COMMANDS = {"serve": cmd_serve, "stop": cmd_stop, "mcp": cmd_mcp, "hook": cmd_hook, "outbox": cmd_outbox,
                "install": cmd_installer, "update": cmd_installer, "uninstall": cmd_installer}


def _msys_root(env: dict | None = None) -> str | None:
    """The Windows folder Git Bash (MSYS) maps `/` to, or None outside an MSYS shell."""
    env = os.environ if env is None else env
    if os.name != "nt" or not env.get("MSYSTEM"):
        return None
    exe = env.get("EXEPATH", "")
    if not exe:
        return None
    root = os.path.normpath(exe)
    if os.path.basename(root).lower() in ("bin", "usr", "cmd", "mingw64", "mingw32"):
        root = os.path.dirname(root)
    return root.replace("\\", "/").rstrip("/")


def undo_msys_paths(argv: list[str], root: str | None) -> list[str]:
    """Git Bash rewrites an argument that starts with `/` into a Windows path under its install folder before
    Python sees it, so `--command "/goto 1 2"` arrives as `C:/Program Files/Git/goto 1 2`. Game commands and
    titles start with `/`, never with Git's own folder, so that prefix is turned back into `/` (also after
    `--opt=`)."""
    if not root:
        return list(argv)
    prefixes = {root + "/", root.replace("/", "\\") + "\\"}
    out = []
    for arg in argv:
        head, sep, value = arg.partition("=") if arg.startswith("--") else ("", "", arg)
        for prefix in prefixes:
            if value.lower().startswith(prefix.lower()):
                value = "/" + value[len(prefix):]
                break
        out.append(head + sep + value)
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    argv = undo_msys_paths(sys.argv[1:] if argv is None else argv, _msys_root())
    args = build_parser().parse_args(argv)
    data_dir = resolve_data_dir(getattr(args, "data", None))
    if args.cmd in RAW_COMMANDS:
        return RAW_COMMANDS[args.cmd](args, data_dir)
    try:
        with Store(data_dir) as store:
            return STORE_COMMANDS[args.cmd](args, store)
    except sqlite3.OperationalError as e:
        if args.cmd in READ_ONLY_COMMANDS or not _read_only_error(e):
            print(f"error: {e}", file=sys.stderr)
            return 1
        return _queue_in_outbox(argv, e)
    except DeskError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
