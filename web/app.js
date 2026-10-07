// Pair Desk web UI. Plain ES modules, no build step, no network access beyond this desk.
import { esc, renderMarkdown } from "./md.js";
import { ListWindow, ROW_HEIGHT, BOARD_ROW_HEIGHT, GROUP_HEIGHT } from "./list-window.js";

// ------------------------------------------------------------------------------ constants
const STATUSES = ["to_check", "auto_check", "reported", "failed", "open", "in_progress", "parked", "passed", "closed"];
const STATUS_LABEL = {
  reported: "Reported", open: "Open", in_progress: "In progress", to_check: "To check", auto_check: "Auto check",
  passed: "Passed", failed: "Failed", parked: "Parked", closed: "Closed",
};
const DONE = ["passed", "closed"];
const SIZES = ["S", "M", "L"];
const PLAN_STATES = ["todo", "doing", "done", "dropped"];
const NOTIFY_EVENTS = { comments: "Owner comments and plan notes", verdicts: "Verdicts (passed / still broken)", reports: "New reports (owner and game)", status: "Status changes, merges and grouping" };
const HANDOFF = "~handoff";
// The list/detail split (a fraction of the width for the list), remembered per viewer.
const SPLIT_DEFAULT = 0.54, SPLIT_MIN_LIST = 340, SPLIT_MIN_DETAIL = 400;
const KINDS = ["bug", "check", "idea", "task"];
const KIND_LABEL = { bug: "Bug", check: "Check", idea: "Idea", task: "Task" };
const PRIORITIES = ["p0", "p1", "p2", "p3"];
const PRIORITY_LABEL = { p0: "P0 urgent", p1: "P1 high", p2: "P2 normal", p3: "P3 low" };
const SOURCES = ["owner", "agent", "game"];
const SORTS = { triage: "Triage order", updated: "Recently updated", created: "Newest", oldest: "Oldest", priority: "Priority", number: "Number", backlog: "Backlog order" };
// The backlog view: open work by priority, then size, then area, grouped by area.
const BACKLOG_STATUSES = ["open", "in_progress"];
const DEFAULT_FILTERS = () => ({ status: ["to_check", "auto_check", "reported", "failed", "open", "in_progress"], kind: [], priority: [], area: "", seed: "", merged: false, q: "", sort: "triage" });
// The game command grammar: statements separated by ";", the teleport names its world with "seed N".
const GOTO_COMMAND = "/goto", SEED_TOKEN = "seed", COMMAND_SEPARATOR = ";";
const PAGE = 300;
const AUTHOR = "owner";

const ICON = {
  bug: '<svg viewBox="0 0 16 16" aria-hidden="true"><ellipse cx="8" cy="9.5" rx="3.6" ry="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 5V14M4.4 8H2M14 8h-2.4M4.6 11.5l-2 1.5M11.4 11.5l2 1.5M5.5 5.3L4 3.5M10.5 5.3L12 3.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
  check: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2" y="2" width="12" height="12" rx="3" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 8.3l2 2 4-4.3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  idea: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8a4.4 4.4 0 0 0-2.6 8c.5.4.8 1 .8 1.6v.4h3.6v-.4c0-.6.3-1.2.8-1.6A4.4 4.4 0 0 0 8 1.8z" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6.3 14h3.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  task: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4h8M6 8h8M6 12h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="2.8" cy="4" r="1" fill="currentColor"/><circle cx="2.8" cy="8" r="1" fill="currentColor"/><circle cx="2.8" cy="12" r="1" fill="currentColor"/></svg>',
  comment: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 3.5h11v7.5H7l-3 2.5V11H2.5z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
  clip: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10.5 4.5l-5 5a1.4 1.4 0 0 0 2 2l5.3-5.3a2.8 2.8 0 0 0-4-4L3.5 7.5a4.2 4.2 0 0 0 6 6l4-4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
  pin: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 14.5s4.5-4.2 4.5-7.8a4.5 4.5 0 0 0-9 0c0 3.6 4.5 7.8 4.5 7.8z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="8" cy="6.7" r="1.6" fill="currentColor"/></svg>',
  copy: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5" y="5" width="8.5" height="8.5" rx="2" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M3 10.5V4a1.5 1.5 0 0 1 1.5-1.5H10" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
  send: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 8L13.5 2.5 11 13.5 7.5 9.5z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M7.5 9.5l6-7" stroke="currentColor" stroke-width="1.4"/></svg>',
  pass: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  fail: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  back: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M10 3L5 8l5 5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  close: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  trash: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  file: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.8h5l3 3v9.4H4z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M9 1.8v3h3" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>',
  plan: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 4.2l1.3 1.3 2.2-2.4M2.5 10.2l1.3 1.3 2.2-2.4M8 4.5h5.5M8 10.5h5.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  tree: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 2.5v9a1.5 1.5 0 0 0 1.5 1.5h2.5M3.5 6.5h4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><rect x="8.5" y="4.5" width="5" height="4" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/><rect x="8.5" y="10.5" width="5" height="4" rx="1.2" fill="none" stroke="currentColor" stroke-width="1.3"/></svg>',
  merge: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v3.2c0 1.5 1 2.3 2.4 2.8L8 9l1.6-.5C11 8 12 7.2 12 5.7V2.5M8 9v4.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M6 11.8L8 13.8l2-2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  chev: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  up: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 10l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  down: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  plus: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>',
  doc: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.8h5l3 3v9.4H4z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M6 8h4M6 10.5h4M6 5.5h1.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/></svg>',
  build: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.8l5.6 3.1v6.2L8 14.2l-5.6-3.1V4.9z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M2.6 4.9L8 7.9l5.4-3M8 7.9v6.3M5.2 3.3l5.5 3.1" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>',
  folder: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.8 4.2c0-.7.5-1.2 1.2-1.2h3l1.5 1.6h5.5c.7 0 1.2.5 1.2 1.2v6.3c0 .7-.5 1.2-1.2 1.2H3c-.7 0-1.2-.5-1.2-1.2z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',
  play: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 2.8v10.4L13 8z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',
  link: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M6.5 9.5l3-3M7 4.5l1-1a2.8 2.8 0 0 1 4 4l-1 1M9 11.5l-1 1a2.8 2.8 0 0 1-4-4l1-1" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>',
};

// ------------------------------------------------------------------------------ state
const S = {
  projects: [],
  slug: null,
  project: null,          // overview: counts, areas, tags, last_change
  filters: DEFAULT_FILTERS(),
  list: { issues: [], total: 0, counts: {} },
  limit: PAGE,
  cursor: -1,
  openId: null,
  issue: null,
  editing: null,          // "title" | "body" | "location" while an inline editor is open
  drafts: new Map(),      // issue id -> { text, files: [{file, url}] }
  polls: new Map(),       // command id -> timer
  locDraft: null,         // the location editor's command rows: [{command, label}]
  listSeq: 0,
  dialogFiles: [],
  view: "list",           // "list" | "backlog" | "board"
  selected: new Set(),    // issue ids ticked for merge / grouping
  anchor: -1,             // row index shift-click ranges start from
  collapsed: new Set(),   // backlog area groups folded away
  rowStamp: new Map(),    // id -> updated_at of the last list, to highlight rows that just changed
  seen: new Set(),        // timeline entries of the open issue already shown (to highlight new ones)
  handoff: null,          // the project handoff while its panel is open
  live: null,             // EventSource of the open project
  liveOk: false,
  stepNote: null,         // plan step index whose note editor is open
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ------------------------------------------------------------------------------ utilities
/** The server's per-run token, written into index.html as it is served: it lets this page (and no other)
 *  open a build's folder or run it. */
const DESK_TOKEN = document.querySelector('meta[name="pair-desk-token"]')?.content || "";
const RESPONSE_ETAG = Symbol("response-etag");

async function api(method, path, body, headers = {}, signal) {
  const opts = { method, headers: { ...headers }, signal };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new Error("The desk server is not reachable. Is `python desk.py serve` still running?");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch((e) => { if (e.name === "AbortError") throw e; return {}; });
  if (!res.ok) throw new Error(data.error || `${res.status} ${res.statusText}`);
  if (data && typeof data === "object") data[RESPONSE_ETAG] = res.headers.get("ETag");
  return data;
}

function toast(msg, kind = "ok", ms = 3200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}
const fail = (e) => toast(e.message || String(e), "error", 5200);

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 45) return "just now";
  if (s < 90) return "1 min ago";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 5400) return "1 h ago";
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 172800) return "yesterday";
  if (s < 86400 * 30) return `${Math.round(s / 86400)} days ago`;
  return new Date(iso).toLocaleDateString();
}
const fullTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const clockTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(`pairdesk.${key}`));
    localStorage.setItem(`pairdesk.${key}`, JSON.stringify(value));
  } catch (e) { return null; }
  return null;
}

function mdCtx() {
  const prefixes = S.projects.map((p) => p.prefix);
  return {
    prefixes,
    slugFor: (key) => (S.projects.find((p) => key.startsWith(p.prefix + "-")) || {}).slug || S.slug,
  };
}
const md = (text) => renderMarkdown(text, mdCtx());

/** The location command with the world seed on its first /goto statement ("/goto 1 2; /time 6" with
 *  seed 1234 reads "/goto 1 2 seed 1234; /time 6"). Unchanged without a seed or a /goto, or when the
 *  statement already names one. The Python twin is command_with_seed in pair_desk/store.py. */
function commandWithSeed(command, seed) {
  if (!command || seed === undefined || seed === null || seed === "") return command || "";
  const parts = command.split(COMMAND_SEPARATOR);
  for (let n = 0; n < parts.length; n++) {
    const words = parts[n].split(/\s+/).filter(Boolean);
    if (!words.length || words[0].toLowerCase() !== GOTO_COMMAND) continue;
    if (words.slice(1).some((w) => w.toLowerCase() === SEED_TOKEN)) return command;
    const body = parts[n].replace(/\s+$/, "");
    parts[n] = `${body} ${SEED_TOKEN} ${seed}${parts[n].slice(body.length)}`;
    return parts.join(COMMAND_SEPARATOR);
  }
  return command;
}

/** The location's commands in order, each {command, label} with the world seed on its first /goto: one place
 *  per command. A location stored before the list has only `command`. The Python twin is location_commands. */
function gameCommands(loc) {
  const list = Array.isArray(loc?.commands) ? loc.commands : loc?.command ? [{ command: loc.command }] : [];
  return list.filter((c) => c && c.command).map((c) => ({ command: commandWithSeed(c.command, loc.seed), label: c.label || "" }));
}

/** The command the game should run for location command `n` (0 = the first; with its seed), or "". */
const gameCommand = (loc, n = 0) => gameCommands(loc)[n]?.command || "";

/** The newest command sent to the game with exactly this text (the issue's `commands` queue, newest first). */
const sentFor = (issue, command) => (issue?.commands || []).find((c) => c.command === command);

/** A build's short name (its label; the desk always gives one) and its hover text: path, commit, build time. */
const buildName = (b) => b?.label || b?.path || "";
const buildTitle = (b) => [b.path, b.commit ? `commit ${b.commit}` : "", b.built_at ? `built ${fullTime(b.built_at)}` : ""].filter(Boolean).join("\n");

/** The project's current build in the filter bar: click copies its path. Nothing for a project that never
 *  published one; a warning when builds are used but none is current (agents cannot hand issues over). */
function currentBuildHtml() {
  const p = S.project;
  if (!p?.builds_enabled) return "";
  const b = p.build;
  if (!b) return `<span class="build-current none" title="No current build: agents cannot move issues to To check until the next build is published">${ICON.build}<span>No current build</span></span>`;
  return `<span class="build-wrap">
    <button type="button" class="build-current" data-build-pop="1" aria-haspopup="dialog" aria-expanded="${!!S.buildPop}" title="Current build&#10;${esc(buildTitle(b))}">
      ${ICON.build}<span class="build-name">${esc(buildName(b))}</span>${b.commit && b.commit !== b.label ? `<code>${esc(b.commit.slice(0, 10))}</code>` : ""}<span class="build-ago">${ago(b.built_at)}</span></button>
    <div class="build-pop" id="build-pop" role="dialog" aria-label="Current build"${S.buildPop ? "" : " hidden"}>
      <div class="build-head">${ICON.build}<strong class="build-name">${esc(buildName(b))}</strong>
        ${b.commit && b.commit !== b.label ? `<code class="build-commit" title="Commit">${esc(b.commit)}</code>` : ""}
        <span class="muted" title="${esc(fullTime(b.built_at))}">built ${ago(b.built_at)}</span></div>
      ${b.path !== b.label ? `<div class="cmd-box"><code>${esc(b.path)}</code></div>` : ""}
      <div class="build-actions">${buildActionsHtml(b, "data-build-act")}</div>
    </div></span>`;
}

/** Copy, and Open folder / Run when the path is a file or folder on the machine the desk runs on (the server
 *  says so in `local`); a version string only copies. `attr` names the data attribute the click handler reads. */
function buildActionsHtml(b, attr) {
  const local = b.local || {};
  return `<button class="btn btn-sm" ${attr}="copy" title="Copy ${b.path === b.label ? "the version" : "the path"}">${ICON.copy}${b.path === b.label ? "Copy" : "Copy path"}</button>
    ${local.open ? `<button class="btn btn-sm" ${attr}="open" title="Show it in the file manager">${ICON.folder}Open folder</button>` : ""}
    ${local.run ? `<button class="btn btn-sm btn-primary" ${attr}="run" title="Start ${esc(b.path.split(/[\\/]/).pop())}">${ICON.play}Run</button>` : ""}`;
}

/** Open the build's folder or start it: `url` is the project's or the issue's build endpoint; the server acts on
 *  the path it stored, never on anything this page sends. */
async function launchBuild(url, action) {
  try {
    const res = await api("POST", `${url}/${action}`, {}, { "X-Pair-Desk-Token": DESK_TOKEN });
    toast(action === "run" ? `Started ${res.started}` : `Opened ${res.folder.split(/[\\/]/).filter(Boolean).pop() || res.folder}`);
  } catch (e) { fail(e); }
}

function toggleBuildPop(open = !S.buildPop) {
  S.buildPop = open;
  const pop = $("#build-pop");
  if (pop) pop.hidden = !open;
  $(".build-current[data-build-pop]")?.setAttribute("aria-expanded", String(open));
}

/** The build an issue was handed over in, with its path to copy. */
function buildHtml(i) {
  const b = i.build;
  if (!b) return "";
  const current = S.project?.build;
  const older = current && current.number !== b.number;
  return `<section class="section">
    <div class="section-head"><h2>Build</h2></div>
    <div class="card build-card">
      <div class="build-head">${ICON.build}<strong class="build-name">${esc(buildName(b))}</strong>
        ${b.commit && b.commit !== b.label ? `<code class="build-commit" title="Commit">${esc(b.commit)}</code>` : ""}
        <span class="muted" title="${esc(fullTime(b.built_at))}">built ${ago(b.built_at)}</span>
        ${older ? `<span class="build-older" title="The current build is ${esc(buildName(current))}">not the current build</span>` : ""}
        ${b.path === b.label ? `<span class="build-actions">${buildActionsHtml(b, "data-issue-build")}</span>` : ""}</div>
      ${b.path !== b.label ? `<div class="build-path-row"><div class="cmd-box"><code>${esc(b.path)}</code></div>
        <span class="build-actions">${buildActionsHtml(b, "data-issue-build")}</span></div>` : ""}
    </div>
  </section>`;
}

async function copyText(text, label = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(label);
}

function fileToAttachment(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result);
      resolve({ filename: file.name, mime: file.type || "", data_base64: s.slice(s.indexOf(",") + 1) });
    };
    r.onerror = () => reject(new Error(`could not read ${file.name}`));
    r.readAsDataURL(file);
  });
}

function nameFile(file) {
  // Pasted screenshots all arrive as "image.png"; give them a findable name.
  if (!file.name || /^image\.(png|jpe?g|gif|webp)$/i.test(file.name)) {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    const ext = (file.type.split("/")[1] || "png").replace("jpeg", "jpg");
    const name = `screenshot-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.${ext}`;
    return new File([file], name, { type: file.type });
  }
  return file;
}

function filesFrom(dataTransfer) {
  if (!dataTransfer) return [];
  const files = Array.from(dataTransfer.files || []);
  if (!files.length && dataTransfer.items) {
    for (const it of dataTransfer.items) if (it.kind === "file") { const f = it.getAsFile(); if (f) files.push(f); }
  }
  return files.map(nameFile);
}

const isTyping = (el) => el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
const narrow = () => window.matchMedia("(max-width: 860px)").matches;

// ------------------------------------------------------------------------------ theme
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  $("#theme-toggle").title = `Theme: ${t} (click to change)`;
}
function cycleTheme() {
  let cur = "auto";
  try { cur = localStorage.getItem("pairdesk.theme") || "auto"; } catch (e) { /* ignore */ }
  const next = THEMES[(THEMES.indexOf(cur) + 1) % THEMES.length];
  try { localStorage.setItem("pairdesk.theme", next); } catch (e) { /* ignore */ }
  applyTheme(next);
  toast(`Theme: ${next === "auto" ? "follow system" : next}`);
}

// ------------------------------------------------------------------------------ routing
function parseHash() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  return { slug: parts[0] || null, id: parts[1] || null };
}
function go(slug, id) {
  const h = `#/${encodeURIComponent(slug)}${id ? `/${encodeURIComponent(id)}` : ""}`;
  if (location.hash !== h) location.hash = h;
  else route();
}

let routeSeq = 0;
async function route() {
  const seq = ++routeSeq;
  const { slug, id } = parseHash();
  if (!slug) {
    const last = store("project");
    const pick = S.projects.find((p) => p.slug === last) || S.projects[0];
    if (pick) { location.replace(`#/${pick.slug}`); return; }
    renderWelcome();
    return;
  }
  if (slug !== S.slug) await loadProject(slug, seq);
  if (seq !== routeSeq || !S.project) return;
  if (id === HANDOFF) await openHandoff();
  else if (id) await openIssue(id);
  else closeDetail(false);
}

// ------------------------------------------------------------------------------ projects
async function loadProjects() {
  const res = await api("GET", "/api/projects");
  S.projects = res.projects;
  renderProjectMenu();
}

async function loadProject(slug, seq = routeSeq) {
  let project;
  try {
    project = await api("GET", `/api/projects/${encodeURIComponent(slug)}`);
  } catch (e) {
    if (seq !== routeSeq) return;
    fail(e);
    S.project = null;
    if (S.projects.length) location.replace(`#/${S.projects[0].slug}`);
    else renderWelcome();
    return;
  }
  if (seq !== routeSeq) return;
  S.project = project;
  S.lastChange = project.last_change;
  S.slug = slug;
  store("project", slug);
  S.filters = { ...DEFAULT_FILTERS(), ...(store(`filters.${slug}`) || {}), q: "" };
  S.view = ["backlog", "board"].includes(store(`view.${slug}`)) ? store(`view.${slug}`) : "list";
  S.collapsed = new Set(store(`collapsed.${slug}`) || []);
  S.selected.clear();
  S.rowStamp = new Map();
  S.listReadKey = null;
  $("#search").value = "";
  S.limit = PAGE;
  S.cursor = -1;
  S.openId = null;
  S.issue = null;
  $("#project-name").textContent = S.project.name;
  document.title = `${S.project.name} - Pair Desk`;
  $("#quick-add").hidden = false;
  renderProjectMenu();
  await loadList();
  if (seq === routeSeq) {
    connectLive(slug);
    if (!S.openId && !S.handoff) renderEmptyDetail();
  }
}

function renderProjectMenu() {
  const menu = $("#project-menu");
  menu.innerHTML = S.projects.map((p) => `
    <button role="menuitem" data-slug="${esc(p.slug)}" class="${p.slug === S.slug ? "active" : ""}">
      <span class="prefix">${esc(p.prefix)}</span><span>${esc(p.name)}</span>
      <span class="menu-meta" title="waiting for you">${p.counts.to_check + p.counts.reported || ""}</span>
    </button>`).join("") + `${S.projects.length ? "<hr>" : ""}${S.project ? '<button role="menuitem" data-settings="1">Project settings…</button>' : ""}<button role="menuitem" data-new="1">New project…</button>`;
}

function openProjectSettings() {
  if (!S.project) return;
  const dlg = $("#project-dialog");
  const p = S.project;
  dlg.innerHTML = `
    <form method="dialog" id="settings-form">
      <div class="dialog-head"><h2>${esc(p.name)} settings</h2><button class="icon-btn" value="cancel" formnovalidate aria-label="Close">${ICON.close}</button></div>
      <div class="dialog-body">
        <label class="field"><span>Name</span><input name="name" required maxlength="80" value="${esc(p.name)}"></label>
        <label class="field"><span>Default world seed</span><input name="seed" class="mono" inputmode="numeric" pattern="[+-]?[0-9]{1,11}" value="${esc(p.default_seed ?? "")}" placeholder="none"></label>
        <p class="empty-hint">Checks the agents file without a seed are in this world. A <code>/goto</code> only lands in the right place in the same world, so the copied and sent command carries it (<code>seed ${esc(p.default_seed ?? "N")}</code>). Leave empty for none.</p>
        <fieldset class="field notify-set"><legend>Tell the agent sessions about</legend>
          ${Object.entries(NOTIFY_EVENTS).map(([k, label]) => `<label class="check"><input type="checkbox" name="notify-${k}"${p.notify?.[k] !== false ? " checked" : ""}> ${esc(label)}</label>`).join("")}
          <p class="empty-hint">On the agent's next prompt, and pushed live into sessions started with the Pair Desk channel.</p>
        </fieldset>
        <div class="form-error" id="settings-error"></div>
      </div>
      <div class="dialog-foot"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn btn-primary" value="ok">Save</button></div>
    </form>`;
  const f = $("#settings-form");
  f.addEventListener("submit", async (ev) => {
    if (ev.submitter?.value !== "ok") return;
    ev.preventDefault();
    try {
      const notify = Object.fromEntries(Object.keys(NOTIFY_EVENTS).map((k) => [k, f[`notify-${k}`].checked]));
      await api("PATCH", `/api/projects/${encodeURIComponent(p.slug)}`, { name: f.name.value, default_seed: f.seed.value.trim() || null, notify });
      dlg.close();
      await Promise.all([loadProjects(), refreshProject()]);
      $("#project-name").textContent = S.project.name;
      renderProjectMenu();
      if (S.issue) renderDetail(false);
      toast("Project settings saved");
    } catch (e) { $("#settings-error").textContent = e.message; }
  });
  dlg.showModal();
}

function toggleProjectMenu(force) {
  const menu = $("#project-menu");
  const open = force ?? menu.hidden;
  menu.hidden = !open;
  $("#project-button").setAttribute("aria-expanded", String(open));
  if (open) (menu.querySelector("button.active") || menu.querySelector("button"))?.focus();
}

function openProjectDialog() {
  const dlg = $("#project-dialog");
  dlg.innerHTML = `
    <form method="dialog" id="project-form">
      <div class="dialog-head"><h2>New project</h2><button class="icon-btn" value="cancel" formnovalidate aria-label="Close">${ICON.close}</button></div>
      <div class="dialog-body">
        <p class="empty-hint">A project is one game. Issues get ids like <b>MG-12</b> from the prefix.</p>
        <label class="field"><span>Name</span><input name="name" required maxlength="80" placeholder="MyGame" autofocus></label>
        <div class="grid-2">
          <label class="field"><span>Slug</span><input name="slug" required pattern="[a-z0-9][a-z0-9-]{0,39}" placeholder="mygame"></label>
          <label class="field"><span>Id prefix</span><input name="prefix" required pattern="[A-Z][A-Z0-9]{0,7}" placeholder="MG" class="mono"></label>
        </div>
        <div class="form-error" id="project-error"></div>
      </div>
      <div class="dialog-foot"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn btn-primary" value="ok">Create project</button></div>
    </form>`;
  const f = $("#project-form");
  let slugTouched = false, prefixTouched = false;
  f.slug.addEventListener("input", () => { slugTouched = true; });
  f.prefix.addEventListener("input", () => { prefixTouched = true; f.prefix.value = f.prefix.value.toUpperCase(); });
  f.name.addEventListener("input", () => {
    if (!slugTouched) f.slug.value = f.name.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);
    if (!prefixTouched) {
      const words = f.name.value.split(/[^A-Za-z0-9]+/).filter(Boolean);
      const caps = f.name.value.replace(/[^A-Z]/g, "");
      f.prefix.value = (caps.length >= 2 ? caps : words.length > 1 ? words.map((w) => w[0]).join("") : f.name.value.replace(/[^A-Za-z0-9]/g, "")).slice(0, 3).toUpperCase();
    }
  });
  f.addEventListener("submit", async (ev) => {
    if (ev.submitter?.value !== "ok") return;
    ev.preventDefault();
    try {
      const p = await api("POST", "/api/projects", { name: f.name.value, slug: f.slug.value, prefix: f.prefix.value });
      dlg.close();
      await loadProjects();
      toast(`Project ${p.name} created`);
      go(p.slug);
    } catch (e) { $("#project-error").textContent = e.message; }
  });
  dlg.showModal();
}

function renderWelcome() {
  $("#project-name").textContent = "No project";
  $("#quick-add").hidden = true;
  $("#filters").innerHTML = "";
  $("#list-footer").innerHTML = "";
  $("#list").innerHTML = `
    <div class="welcome">
      <svg viewBox="0 0 32 32" width="44" height="44" aria-hidden="true"><rect x="3" y="6" width="26" height="20" rx="5" fill="var(--accent)"/><path d="M9 16.5l4 4 9-9.5" fill="none" stroke="var(--accent-ink)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <h1>Welcome to Pair Desk</h1>
      <p>Your local desk for playtest reports, checks the agents ask you to verify, and ideas. Everything stays on this machine.</p>
      <p>Start with a project, one per game.</p>
      <button class="btn btn-primary" id="welcome-create">Create a project</button>
    </div>`;
  $("#welcome-create").addEventListener("click", openProjectDialog);
  $("#detail").innerHTML = "";
}

// ------------------------------------------------------------------------------ list
function listQuery() {
  const f = S.filters;
  const p = new URLSearchParams();
  const backlog = S.view === "backlog";
  if (backlog) p.set("status", BACKLOG_STATUSES.join(","));
  else if (f.merged) p.set("merged", "1");
  else if (f.status.length) p.set("status", f.status.join(","));
  if (f.kind.length) p.set("kind", f.kind.join(","));
  if (f.priority.length) p.set("priority", f.priority.join(","));
  if (f.area) p.set("area", f.area);
  if (f.seed) p.set("seed", f.seed);
  if (f.q) p.set("q", f.q);
  p.set("sort", backlog ? "backlog" : f.sort);
  p.set("limit", String(backlog ? 5000 : S.limit));
  // Rows need metadata and plan progress; descriptions and full plans load on opening detail.
  p.set("summary", "1");
  return p.toString();
}

let listRequest = null;
async function loadList() {
  if (!S.slug) return;
  const seq = ++S.listSeq;
  listRequest?.abort();
  const request = listRequest = new AbortController();
  const readKey = `/api/projects/${encodeURIComponent(S.slug)}/issues?${listQuery()}`;
  const presentationKey = `${S.view}:${readKey}`;
  let res;
  try {
    res = await api("GET", readKey, undefined, {}, request.signal);
  } catch (e) { if (e.name !== "AbortError") fail(e); return; }
  finally { if (listRequest === request) listRequest = null; }
  if (seq !== S.listSeq) return; // a newer request superseded this one
  if (presentationKey === S.listReadKey && res[RESPONSE_ETAG] && res[RESPONSE_ETAG] === S.list[RESPONSE_ETAG]) return;
  S.listReadKey = presentationKey;
  // Rows whose updated_at moved since the last list just changed: highlight them once.
  const fresh = new Set();
  if (S.rowStamp.size) {
    for (const i of res.issues) if (S.rowStamp.get(i.id) !== i.updated_at) fresh.add(i.id);
  }
  S.rowStamp = new Map(res.issues.map((i) => [i.id, i.updated_at]));
  const known = new Set(res.issues.map((i) => i.id));
  for (const id of [...S.selected]) if (!known.has(id)) S.selected.delete(id);
  const cursorId = S.list.issues?.[S.cursor]?.id;
  const anchorId = S.list.issues?.[S.anchor]?.id;
  S.list = res;
  if (cursorId) S.cursor = res.issues.findIndex(i => i.id === cursorId);
  if (anchorId) S.anchor = res.issues.findIndex(i => i.id === anchorId);
  renderFilters();
  renderList(fresh);
}

function saveFilters() {
  const { q, ...rest } = S.filters;
  store(`filters.${S.slug}`, rest);
}

function setFilter(mutator) {
  mutator(S.filters);
  S.limit = PAGE;
  S.cursor = -1;
  $("#list").scrollTop = 0;
  saveFilters();
  loadList();
}

function chip({ group, value, label, count, on, color, icon }) {
  const style = color ? ` style="--c: var(--st-${value}); --cbg: var(--st-${value}-bg)"` : "";
  return `<button type="button" class="chip${on ? " on" : ""}${count === 0 && !on ? " zero" : ""}" data-group="${group}" data-value="${esc(value)}"${style} aria-pressed="${on}">
    ${color ? '<span class="dot"></span>' : ""}${icon || ""}<span>${esc(label)}</span>${count !== undefined ? `<span class="count">${count}</span>` : ""}</button>`;
}

function renderFilters() {
  const c = S.list.counts || {};
  const f = S.filters;
  const sc = c.status || {}, kc = c.kind || {}, pc = c.priority || {}, ac = c.area || {}, seedc = c.seed || {};
  const seeds = [...new Set([...Object.keys(seedc), ...(f.seed ? [String(f.seed)] : [])])].sort((a, b) => Number(a) - Number(b));
  const allCount = Object.values(sc).reduce((a, b) => a + b, 0);
  const areas = [...new Set([...(S.project?.areas || []), ...Object.keys(ac).filter(Boolean)])];
  const backlog = S.view === "backlog";
  $("#filters").innerHTML = `
    <div class="chip-row view-row">
      <div class="segmented view-switch" role="tablist" aria-label="View">
        <button type="button" role="tab" data-view="list" aria-selected="${S.view === "list"}" class="${S.view === "list" ? "on" : ""}">Triage</button>
        <button type="button" role="tab" data-view="backlog" aria-selected="${backlog}" class="${backlog ? "on" : ""}" title="Backlog (b)">Backlog</button>
        <button type="button" role="tab" data-view="board" aria-selected="${S.view === "board"}" class="${S.view === "board" ? "on" : ""}">Board</button>
      </div>
      <span class="row-spacer"></span>
      ${currentBuildHtml()}
      <a class="btn btn-sm btn-quiet" href="#/${esc(S.slug)}/${HANDOFF}" title="Project handoff (h)">${ICON.doc}Handoff</a>
    </div>
    ${backlog ? `<div class="backlog-hint">Open and in-progress work by priority, then size, then area. ${sc.parked ? `<button type="button" class="linkish" data-parked="1">${sc.parked} parked</button>` : ""}</div>` : `<div class="chip-row" role="group" aria-label="Status">
      ${STATUSES.map((s) => chip({ group: "status", value: s, label: STATUS_LABEL[s], count: sc[s] || 0, on: f.status.includes(s), color: true })).join("")}
      ${chip({ group: "status", value: "*", label: "All", count: allCount, on: !f.status.length && !f.merged })}
      <span class="sep"></span>
      ${chip({ group: "merged", value: "1", label: "Merged", count: c.merged || 0, on: !!f.merged, icon: ICON.merge })}
    </div>`}
    <div class="chip-row">
      ${KINDS.map((k) => chip({ group: "kind", value: k, label: KIND_LABEL[k], count: kc[k] || 0, on: f.kind.includes(k), icon: `<span class="kind-icon kind-${k}">${ICON[k]}</span>` })).join("")}
      <span class="sep"></span>
      ${PRIORITIES.map((p) => chip({ group: "priority", value: p, label: p.toUpperCase(), count: pc[p] || 0, on: f.priority.includes(p) })).join("")}
      <span class="sep"></span>
      <select class="filter-select${f.area ? " on" : ""}" id="area-filter" aria-label="Area">
        <option value="">All areas</option>
        ${areas.map((a) => `<option value="${esc(a)}"${a.toLowerCase() === f.area.toLowerCase() ? " selected" : ""}>${esc(a)}${ac[a] ? ` (${ac[a]})` : ""}</option>`).join("")}
      </select>
      ${seeds.length ? `<select class="filter-select${f.seed ? " on" : ""}" id="seed-filter" aria-label="World seed">
        <option value="">All worlds</option>
        ${seeds.map((s) => `<option value="${esc(s)}"${String(f.seed) === s ? " selected" : ""}>Seed ${esc(s)}${seedc[s] ? ` (${seedc[s]})` : ""}</option>`).join("")}
      </select>` : ""}
      <span class="row-spacer"></span>
      ${backlog ? "" : `<select class="filter-select" id="sort-select" aria-label="Sort">
        ${Object.entries(SORTS).map(([k, v]) => `<option value="${k}"${k === f.sort ? " selected" : ""}>${v}</option>`).join("")}
      </select>`}
    </div>`;
}

function progressBadge(p, icon, title, cls = "") {
  if (!p || !p.total) return "";
  const done = p.done >= p.total;
  return `<span class="progress-badge${done ? " all-done" : ""}${cls}" title="${esc(title)}">${icon}${p.done}/${p.total}</span>`;
}

function rowHtml(i, idx, fresh) {
  const cls = ["issue-row", i.priority];
  if (i.id === S.openId) cls.push("selected");
  if (idx === S.cursor) cls.push("cursor");
  if (S.selected.has(i.id)) cls.push("checked");
  if (fresh?.has(i.id)) cls.push("fresh");
  const tags = (i.tags || []).slice(0, 3).map((t) => `<span class="tag">${esc(t)}</span>`).join("");
  const children = i.child_count ? { done: i.child_done, total: i.child_count } : null;
  return `<div id="issue-option-${esc(i.id)}" class="${cls.join(" ")}" data-id="${esc(i.id)}" data-idx="${idx}" role="option" aria-posinset="${idx + 1}" aria-setsize="${S.list.issues.length}" aria-selected="${i.id === S.openId}">
    <label class="row-check" title="Select (x), shift-click for a range"><input type="checkbox" data-select="${esc(i.id)}"${S.selected.has(i.id) ? " checked" : ""} aria-label="Select ${esc(i.id)}"></label>
    <span class="pill s-${i.status}">${STATUS_LABEL[i.status]}</span>
    <span class="row-title" title="${esc(i.title)}">${esc(i.title)}</span>
    <span class="row-right">${progressBadge(i.plan_progress, ICON.plan, "Plan steps done", " plan-badge")}${i.size ? `<span class="size-badge" title="Size">${esc(i.size)}</span>` : ""}${i.priority === "p2" ? "" : `<span class="prio prio-${i.priority}">${i.priority.toUpperCase()}</span>`}<span class="row-id">${esc(i.id)}</span></span>
    <div class="row-meta">
      <span class="kind-icon kind-${i.kind}" title="${KIND_LABEL[i.kind]}">${ICON[i.kind]}</span>
      ${i.merged_into ? `<span class="merged-link" title="Merged">${ICON.merge}into ${esc(i.merged_into)}</span>` : ""}
      ${i.parent ? `<span class="part-of" title="Part of">${ICON.tree}part of ${esc(i.parent)}</span>` : ""}
      ${progressBadge(children, ICON.tree, "Children done")}
      ${i.milestone ? `<span class="milestone" title="Milestone">${esc(i.milestone)}</span>` : ""}
      ${i.area && S.view !== "backlog" ? `<span>${esc(i.area)}</span>` : ""}
      ${tags}
      ${i.location && i.location.command ? `<span title="Has a location command">${ICON.pin}</span>` : ""}
      ${i.comment_count ? `<span title="Comments">${ICON.comment}${i.comment_count}</span>` : ""}
      ${i.attachment_count ? `<span title="Attachments">${ICON.clip}${i.attachment_count}</span>` : ""}
      ${i.last_verdict ? `<span class="verdict verdict-${i.last_verdict}">${i.last_verdict === "passed" ? "passed" : "still broken"}</span>` : ""}
      ${i.source !== "owner" ? `<span>by ${esc(i.source)}</span>` : ""}
      <span title="${esc(fullTime(i.updated_at))}">${ago(i.updated_at)}</span>
    </div>
  </div>`;
}

function backlogItems(issues, fresh) {
  const groups = new Map();
  issues.forEach((i, idx) => {
    const area = i.area || "";
    if (!groups.has(area)) groups.set(area, []);
    groups.get(area).push([i, idx]);
  });
  const order = [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));
  return order.flatMap((area) => {
    const rows = groups.get(area);
    const folded = S.collapsed.has(area);
    const sizes = SIZES.map((sz) => [sz, rows.filter(([i]) => i.size === sz).length]).filter(([, n]) => n);
    const html = `<button type="button" class="group-head${folded ? " folded" : ""}" data-area="${esc(area)}" aria-expanded="${!folded}">
        <span class="chev">${ICON.chev}</span><span class="group-name">${esc(area || "No area")}</span>
        <span class="count">${rows.length}</span>
        <span class="group-sizes">${sizes.map(([sz, n]) => `${n} ${sz}`).join(" · ")}</span>
      </button>`;
    return [{ key: `group:${area}`, height: GROUP_HEIGHT, header: true, content: () => ({ html, stamp: html }) },
      ...(folded ? [] : rows.map(([i, idx]) => windowRow(i, idx, fresh)))];
  });
}

function windowRow(i, idx, fresh) {
  return { key: i.id, height: S.view === "board" ? BOARD_ROW_HEIGHT : ROW_HEIGHT, content: () => ({
    html: rowHtml(i, idx, fresh),
    stamp: JSON.stringify([i, idx, S.list.issues.length, S.view, S.cursor === idx, S.openId === i.id, S.selected.has(i.id), !!fresh?.has(i.id)]),
  }) };
}

let listWindows = [], windowKey = null, windowFrame = 0;
function paintWindows() {
  windowFrame = 0;
  const list = $("#list"), top = list.scrollTop, height = list.clientHeight;
  const left = list.scrollLeft, width = list.clientWidth;
  listWindows.forEach((window, index) => {
    const columnLeft = 12 + index * 312;
    const visible = S.view !== "board" || (columnLeft + 300 > left && columnLeft < left + width);
    window.render(top, visible ? height : 0);
  });
  const cursor = $(".issue-row.cursor", list);
  if (cursor) list.setAttribute("aria-activedescendant", cursor.id);
  else list.removeAttribute("aria-activedescendant");
}
function scheduleWindows() {
  if (!windowFrame) windowFrame = requestAnimationFrame(paintWindows);
}
function revealRow(id) {
  const list = $("#list"), top = list.scrollTop, height = list.clientHeight;
  for (let index = 0; index < listWindows.length; index++) {
    if (!listWindows[index].reveal(id, top, height)) continue;
    if (S.view === "board") {
      const left = 12 + index * 312;
      if (left < list.scrollLeft) list.scrollLeft = left;
      else if (left + 300 > list.scrollLeft + list.clientWidth) list.scrollLeft = left + 300 - list.clientWidth;
    }
    break;
  }
  paintWindows();
}

function renderWindows(issues, fresh) {
  const list = $("#list");
  const statuses = STATUSES.filter(s => !S.filters.status.length || S.filters.status.includes(s));
  const key = `${S.slug}:${S.view}:${S.view === "board" ? statuses.join() : ""}`;
  // Read the old anchor before any DOM writes. Keep the same issue in view when
  // live triage sorting moves another row, without resetting a scrolled list.
  const top = list.scrollTop;
  const oldWindow = listWindows[0];
  const anchor = top > 0 && oldWindow?.items[oldWindow.indexAt(top - oldWindow.offset)];
  const delta = anchor ? top - anchor.top - oldWindow.offset : 0;
  const sameWindow = key === windowKey;
  if (!sameWindow) {
    windowKey = key;
    list.innerHTML = S.view === "board" ? `<div class="status-board">${statuses.map(s => `<section class="board-column list-window" data-status="${s}"></section>`).join("")}</div>` : '<div class="list-window"></div>';
    listWindows = $$(".list-window", list).map(host => new ListWindow(host, list, S.view === "board" ? 12 : 0));
    list.scrollTop = 0;
  }
  if (S.view === "board") {
    for (const window of listWindows) {
      const s = window.host.dataset.status;
      const rows = issues.map((i, idx) => ({ i, idx })).filter(row => row.i.status === s);
      const html = `<h3 class="pill s-${s}">${STATUS_LABEL[s]} (${rows.length})</h3>`;
      window.setItems([{ key: `status:${s}`, height: 48, content: () => ({ html, stamp: html }) }, ...rows.map(({ i, idx }) => windowRow(i, idx, fresh))]);
    }
  } else listWindows[0].setItems(S.view === "backlog" ? backlogItems(issues, fresh) : issues.map((i, idx) => windowRow(i, idx, fresh)));
  if (anchor && sameWindow && S.view !== "board") {
    const moved = listWindows[0].positions.get(anchor.key);
    if (moved) list.scrollTop = moved.top + delta;
  }
  paintWindows();
}

function renderSelectionBar() {
  const bar = $("#selection-bar");
  const n = S.selected.size;
  bar.hidden = n === 0;
  if (!n) { bar.innerHTML = ""; return; }
  bar.innerHTML = `<strong>${n} selected</strong>
    <button class="btn btn-sm" data-sel="merge"${n < 2 ? " disabled" : ""} title="Merge into one (m)">${ICON.merge}Merge into…</button>
    <button class="btn btn-sm" data-sel="group"${n < 2 ? " disabled" : ""} title="Make them part of one parent">${ICON.tree}Group under…</button>
    <span class="row-spacer"></span>
    <button class="btn btn-sm btn-quiet" data-sel="clear" title="Clear the selection">Clear</button>`;
}

function renderList(fresh) {
  const list = $("#list");
  const issues = S.list.issues;
  renderSelectionBar();
  if (!issues.length) {
    listWindows = []; windowKey = null;
    const filtered = S.filters.merged || S.filters.status.length || S.filters.kind.length || S.filters.priority.length || S.filters.area || S.filters.seed || S.filters.q;
    list.innerHTML = `<div class="list-empty"><strong>${filtered ? "Nothing matches these filters" : "No issues yet"}</strong>
      ${filtered ? 'Nothing waiting here. <button class="btn btn-sm" id="clear-filters">Show everything</button>' : "Add one with the bar above or press <kbd>n</kbd>."}</div>`;
    $("#clear-filters")?.addEventListener("click", () => setFilter((f) => { Object.assign(f, DEFAULT_FILTERS(), { status: [], sort: f.sort }); $("#search").value = ""; }));
  } else renderWindows(issues, fresh);
  const shown = issues.length;
  $("#list-footer").innerHTML = `<span>${shown < S.list.total ? `Showing ${shown} of ${S.list.total}` : `${S.list.total} issue${S.list.total === 1 ? "" : "s"}`}</span>
    ${shown < S.list.total ? '<button class="btn btn-sm" id="more">Show more</button>' : ""}
    <span class="row-spacer"></span><span class="live-state" title="${S.liveOk ? "Live: changes appear as they happen" : "Checking for changes every few seconds"}">${S.liveOk ? "● live" : "○ polling"}</span><span><kbd>j</kbd> <kbd>k</kbd> move · <kbd>x</kbd> select · <kbd>?</kbd> keys</span>`;
  $("#more")?.addEventListener("click", () => { S.limit += PAGE; loadList(); });
}

function setCursor(idx, { open = false, scroll = true } = {}) {
  const issues = S.list.issues;
  if (!issues.length) return;
  idx = Math.max(0, Math.min(issues.length - 1, idx));
  $$(".issue-row.cursor").forEach((r) => r.classList.remove("cursor"));
  S.cursor = idx;
  // Keyboard movement follows logical rows, including rows outside the DOM and
  // folded groups. Unfold the destination group before revealing its window.
  if (S.view === "backlog" && S.collapsed.delete(issues[idx].area || "")) {
    store(`collapsed.${S.slug}`, [...S.collapsed]); renderList();
  }
  if (scroll) revealRow(issues[idx].id); else paintWindows();
  const row = $(`.issue-row[data-idx="${idx}"]`);
  if (row) {
    row.classList.add("cursor");
  }
  if (open) go(S.slug, issues[idx].id);
}

// ------------------------------------------------------------------------------ detail
function renderEmptyDetail() {
  if (!S.project) return;
  const c = S.project.counts;
  $("#detail").innerHTML = `
    <div class="detail-scroll"><div class="detail-empty">
      <h2>${esc(S.project.name)}</h2>
      <div>${S.project.issue_count} issues so far · ids like <b class="mono">${esc(S.project.prefix)}-1</b></div>
      <div class="stats">
        <button class="stat s-auto_check" data-status="auto_check"><b>${c.auto_check || 0}</b><span>auto check</span></button>
        <button class="stat s-to_check" data-status="to_check"><b>${c.to_check}</b><span>to check</span></button>
        <button class="stat s-reported" data-status="reported"><b>${c.reported}</b><span>reported</span></button>
        <button class="stat s-failed" data-status="failed"><b>${c.failed}</b><span>still broken</span></button>
      </div>
      <div class="keys">
        <kbd>j</kbd><span>next issue, <kbd>k</kbd> previous, <kbd>Enter</kbd> open</span>
        <kbd>p</kbd><span>passed, <kbd>f</kbd> still broken, <kbd>c</kbd> comment</span>
        <kbd>s</kbd><span>send the first location command to the game</span>
        <kbd>n</kbd><span>new issue, <kbd>/</kbd> search, <kbd>?</kbd> all keys</span>
      </div>
    </div></div>`;
  $$(".stat", $("#detail")).forEach((b) => b.addEventListener("click", () => setFilter((f) => { f.status = [b.dataset.status]; })));
}

let issueRequest = null, issueSeq = 0;
async function openIssue(id) {
  const seq = ++issueSeq, slug = S.slug, hash = location.hash;
  issueRequest?.abort();
  const request = issueRequest = new AbortController();
  let issue;
  try {
    issue = await api("GET", `/api/issues/${encodeURIComponent(id)}`, undefined, {}, request.signal);
  } catch (e) {
    if (e.name === "AbortError" || seq !== issueSeq || hash !== location.hash) return;
    fail(e);
    closeDetail();
    return;
  }
  finally { if (issueRequest === request) issueRequest = null; }
  if (seq !== issueSeq || slug !== S.slug || hash !== location.hash) return;
  if (issue.project !== S.slug) { go(issue.project, issue.id); return; }
  if (issue.redirected_from) {
    toast(`${issue.redirected_from} was merged into ${issue.id}`);
    history.replaceState(null, "", `#/${S.slug}/${issue.id}`);
  }
  const changed = S.openId !== issue.id;
  S.openId = issue.id;
  S.issue = issue;
  S.handoff = null;
  S.editing = null;
  S.stepNote = null;
  if (changed) S.seen = timelineKeys(issue);
  document.body.classList.add("detail-open");
  const idx = S.list.issues.findIndex((i) => i.id === issue.id);
  if (idx >= 0) S.cursor = idx;
  if (changed && idx >= 0) setCursor(idx); else paintWindows();
  $$(".issue-row").forEach((r) => {
    const sel = r.dataset.id === issue.id;
    r.classList.toggle("selected", sel);
    r.classList.toggle("cursor", sel);
    r.setAttribute("aria-selected", String(sel));
  });
  renderDetail(changed);
  resumeSendPolling();
}

function closeDetail(updateHash = true) {
  ++issueSeq;
  issueRequest?.abort();
  S.openId = null;
  S.issue = null;
  S.handoff = null;
  S.editing = null;
  paintWindows();
  document.body.classList.remove("detail-open");
  $$(".issue-row.selected").forEach((r) => r.classList.remove("selected"));
  renderEmptyDetail();
  if (updateHash && S.slug) history.replaceState(null, "", `#/${S.slug}`);
}

function draft(id) {
  if (!S.drafts.has(id)) S.drafts.set(id, { text: "", files: [] });
  return S.drafts.get(id);
}

function renderDetail(fresh) {
  const root = $("#detail");
  if (fresh || !$(".detail-scroll", root) || root.dataset.issue !== S.issue.id) {
    root.dataset.issue = S.issue.id;
    root.innerHTML = `<div class="detail-scroll"><div class="detail-inner" id="detail-inner"></div></div><div class="composer" id="composer"></div>`;
    renderComposer();
  }
  const scroller = $(".detail-scroll", root);
  const keep = fresh ? 0 : scroller.scrollTop;
  const openBuildRuns = new Set($$(".tl-build-run[open]", root).map((el) => el.dataset.buildRun));
  $("#detail-inner").innerHTML = detailHtml(S.issue);
  $$(".tl-build-run", root).forEach((el) => { el.open = openBuildRuns.has(el.dataset.buildRun); });
  S.seen = timelineKeys(S.issue);
  scroller.scrollTop = keep;
  if (fresh) scroller.scrollTop = 0;
}

function locationFacts(loc) {
  const facts = [];
  if (loc.action) facts.push(["To do", loc.action]);
  if (loc.seed !== undefined && loc.seed !== null) facts.push(["World seed", loc.seed]);
  if (loc.place) facts.push(["Place", loc.place]);
  if (["x", "y", "z"].some((k) => loc[k] !== undefined)) facts.push(["Pos", ["x", "y", "z"].map((k) => loc[k] ?? "?").join(", ")]);
  if (loc.yaw !== undefined) facts.push(["Yaw", loc.yaw]);
  if (loc.pitch !== undefined) facts.push(["Pitch", loc.pitch]);
  if (loc.time) facts.push(["Time", loc.time]);
  if (loc.weather) facts.push(["Weather", loc.weather]);
  for (const [k, v] of Object.entries(loc.extra || {})) facts.push([k, typeof v === "object" ? JSON.stringify(v) : v]);
  return facts;
}

function sendStateHtml(cmd) {
  if (!cmd) return "";
  if (cmd.state === "delivered") return `<span class="send-state delivered">${ICON.pass}Picked up by ${esc(cmd.client || "the game")} at ${esc(clockTime(cmd.delivered_at))}</span>`;
  if (cmd.state === "expired") return `<span class="send-state expired">Not picked up; expired ${esc(ago(cmd.expires_at))}. Is the game running with its dev console on?</span>`;
  return `<span class="send-state"><span class="spinner"></span>Waiting for the game to pick it up…</span>`;
}

function attachmentTile(a, removable = true) {
  if (a.is_image) {
    return `<div class="thumb" role="button" tabindex="0" data-lightbox="${a.url}" title="${esc(a.filename)}">
      <img src="${a.url}" alt="${esc(a.filename)}" loading="lazy">
      ${removable ? `<button class="thumb-x" data-delete-att="${a.id}" title="Remove attachment" aria-label="Remove">✕</button>` : ""}</div>`;
  }
  return `<a class="file-chip" href="${a.url}" target="_blank" rel="noopener">${ICON.file}<span>${esc(a.filename)}</span><small>${fmtSize(a.size)}</small></a>`;
}

const timelineKeys = (issue) => new Set([...(issue.comments || []).map((c) => `c${c.id}`), ...(issue.activity || []).map((a) => `a${a.id}`)]);
const mergedTag = (x) => (x.merged_from ? `<span class="merged-from" title="Moved here by a merge">merged from ${esc(x.merged_from)}</span>` : "");

function stepLabel(d) {
  const bits = [];
  if (d.to) bits.push(`${d.from ? `${esc(d.from)} → ` : ""}<b>${esc(d.to)}</b>`);
  if (d.commit) bits.push(`commit <code>${esc(d.commit)}</code>`);
  if (d.note) bits.push(`note: “${esc(d.note)}”`);
  if (d.old_text) bits.push("reworded");
  return bits.join(", ");
}

function timelineHtml(issue) {
  const items = [
    ...issue.comments.map((c) => ({ t: c.created_at, o: 0, c })),
    ...issue.activity.map((a) => ({ t: a.created_at, o: 1, a })),
  ].sort((x, y) => (x.t < y.t ? -1 : x.t > y.t ? 1 : x.o - y.o));
  const renderItem = (it) => {
    if (it.c) {
      const c = it.c;
      const who = c.author.toLowerCase();
      const av = who === "owner" ? "owner" : who === "game" ? "game" : "";
      const fresh = S.seen.has(`c${c.id}`) ? "" : " fresh";
      return `<div class="tl-comment${fresh}" data-key="c${c.id}">
        <div class="avatar ${av}" title="${esc(c.author)}">${esc(c.author.slice(0, 1))}</div>
        <div class="bubble${c.verdict ? ` v-${c.verdict}` : ""}">
          <div class="bubble-head"><strong>${esc(c.author)}</strong>
            ${c.verdict ? `<span class="verdict verdict-${c.verdict}">${c.verdict === "passed" ? "✓ Passed" : "✕ Still broken"}</span>` : ""}
            ${mergedTag(c)}<span title="${esc(fullTime(c.created_at))}">${ago(c.created_at)}</span></div>
          ${c.text || c.attachments.length ? `<div class="bubble-body">${c.text ? `<div class="md">${md(c.text)}</div>` : ""}
            ${c.attachments.length ? `<div class="gallery">${c.attachments.map((a) => attachmentTile(a, false)).join("")}</div>` : ""}</div>` : ""}
        </div></div>`;
    }
    const a = it.a;
    const d = a.detail || {};
    let text;
    switch (a.action) {
      case "created": text = `<strong>${esc(a.actor)}</strong> filed this as <span class="pill s-${esc(d.status)}">${STATUS_LABEL[d.status] || esc(d.status)}</span>${inBuild(d.build)}`; break;
      case "status": text = `<strong>${esc(a.actor)}</strong> moved it from <span class="pill s-${esc(d.from)}">${STATUS_LABEL[d.from] || esc(d.from)}</span> to <span class="pill s-${esc(d.to)}">${STATUS_LABEL[d.to] || esc(d.to)}</span>${inBuild(d.build)}`; break;
      case "build": text = `<strong>${esc(a.actor)}</strong> published build <span class="build-ref">${ICON.build}${esc(d.label)}</span>${d.previous ? ` <span class="muted">(was ${esc(d.previous)})</span>` : ""}`; break;
      case "edited":
        if (d.owner_original) return `<div class="merged-card${S.seen.has(`a${a.id}`) ? "" : " fresh"}" data-key="a${a.id}">
          <strong>owner's original</strong><div class="merged-title">${esc(d.owner_original.title || "")}</div>
          <div class="md">${md(d.owner_original.body || "")}</div>
          <div class="muted">${esc(a.actor)} edited ${esc((d.fields || []).join(", "))} · ${ago(a.created_at)} ${mergedTag(a)}</div></div>`;
        text = `<strong>${esc(a.actor)}</strong> edited ${esc((d.fields || []).join(", "))}`; break;
      case "attached": text = `<strong>${esc(a.actor)}</strong> attached ${esc(d.filename)}`; break;
      case "detached": text = `<strong>${esc(a.actor)}</strong> removed ${esc(d.filename)}`; break;
      case "command_sent": text = `<strong>${esc(a.actor)}</strong> sent <code>${esc(d.command)}</code> to the game`; break;
      case "command_delivered": text = `<strong>${esc(d.client || a.actor)}</strong> picked up the command`; break;
      case "plan":
        text = d.op === "set"
          ? `<strong>${esc(a.actor)}</strong> ${d.replaced ? "revised" : "wrote"} the plan (${(d.steps || []).length} steps)${d.verification ? `, verification: ${esc(d.verification)}` : ""}`
          : `<strong>${esc(a.actor)}</strong> step ${esc(d.index)} <span class="tl-step">${esc(d.text || "")}</span>: ${stepLabel(d)}`;
        break;
      case "parent": text = d.parent ? `<strong>${esc(a.actor)}</strong> made it part of ${issueLink(d.parent)}` : `<strong>${esc(a.actor)}</strong> removed it from ${issueLink(d.previous)}`; break;
      case "child": text = `<strong>${esc(a.actor)}</strong> ${d.op === "added" ? "added" : "removed"} child ${issueLink(d.child)}`; break;
      case "merged_into": text = `<strong>${esc(a.actor)}</strong> merged it into ${issueLink(d.into)}`; break;
      case "unmerged": text = d.source ? `<strong>${esc(a.actor)}</strong> unmerged ${issueLink(d.source)}` : `<strong>${esc(a.actor)}</strong> unmerged it from ${issueLink(d.from)}`; break;
      case "merged": return mergedCardHtml(a, S.seen.has(`a${a.id}`) ? "" : " fresh");
      default: text = `<strong>${esc(a.actor)}</strong> ${esc(a.action)}`;
    }
    const fresh = S.seen.has(`a${a.id}`) ? "" : " fresh";
    return `<div class="tl-event${fresh}" data-key="a${a.id}"><span class="tl-dot"></span>${text}${mergedTag(a)}<span title="${esc(fullTime(a.created_at))}">· ${ago(a.created_at)}</span></div>`;
  };
  const rows = [];
  for (let n = 0; n < items.length;) {
    if (items[n].a?.action !== "build") {
      rows.push(renderItem(items[n++]));
      continue;
    }
    const start = n;
    while (n < items.length && items[n].a?.action === "build") n++;
    const run = items.slice(start, n);
    if (run.length < 3) {
      rows.push(...run.map(renderItem));
      continue;
    }
    const newest = run.at(-1).a;
    const fresh = run.some((it) => !S.seen.has(`a${it.a.id}`)) ? " fresh" : "";
    rows.push(`<details class="tl-build-run" data-build-run="a${run[0].a.id}">
      <summary class="tl-event${fresh}">published ${run.length} builds · newest <span class="build-ref">${ICON.build}${esc(newest.detail?.label || "")}</span><span title="${esc(fullTime(newest.created_at))}">· ${ago(newest.created_at)}</span></summary>
      <div class="tl-build-entries">${run.map(renderItem).join("")}</div></details>`);
  }
  return rows.join("");
}

const inBuild = (label) => (label ? ` in build <span class="build-ref">${ICON.build}${esc(label)}</span>` : "");
const issueLink = (key) => (key ? `<a class="issue-ref" href="#/${esc(S.slug)}/${esc(key)}">${esc(key)}</a>` : "?");

function mergedCardHtml(a, fresh) {
  const d = a.detail || {};
  const loc = d.location || {};
  const stillMerged = (S.issue?.merged_sources || []).some((m) => m.id === d.from);
  return `<div class="merged-card${fresh}" data-key="a${a.id}">
    <div class="merged-head">${ICON.merge}<span><strong>${esc(a.actor)}</strong> merged ${issueLink(d.from)} into this</span>
      <span class="pill s-${esc(d.status)}">${STATUS_LABEL[d.status] || esc(d.status)}</span>
      <span class="muted" title="${esc(fullTime(a.created_at))}">${ago(a.created_at)}</span>
      ${stillMerged ? `<button class="btn btn-sm btn-quiet" data-unmerge="${esc(d.from)}">Unmerge</button>` : ""}</div>
    <div class="merged-title">${esc(d.title || "")}</div>
    ${d.body?.trim() ? `<div class="md">${md(d.body)}</div>` : ""}
    ${gameCommands(loc).map((c) => `<div class="cmd-box small">${c.label ? `<span class="cmd-label">${esc(c.label)}</span>` : ""}<code>${esc(c.command)}</code></div>`).join("")}
    ${locationFacts(loc).length ? `<div class="loc-facts">${locationFacts(loc).map(([k, v]) => `<span class="fact"><b>${esc(k)}</b>${esc(v)}</span>`).join("")}</div>` : ""}
  </div>`;
}

function planHtml(i) {
  const plan = i.plan || { steps: [], verification: "" };
  const prog = i.plan_progress || { done: 0, total: 0 };
  if (!plan.steps.length && !plan.verification) {
    return `<section class="section plan-section" id="plan-section">
      <div class="section-head"><h2>Plan</h2></div>
      <div class="empty-hint">No plan yet. The agent writes one before coding (<code>set_plan</code>) and ticks steps as commits land.</div>
    </section>`;
  }
  const pct = prog.total ? Math.round((100 * prog.done) / prog.total) : 0;
  return `<section class="section plan-section" id="plan-section">
    <div class="section-head"><h2>Plan · <span id="plan-progress">${prog.done}/${prog.total}</span></h2>
      <div class="plan-bar" aria-hidden="true"><span style="width:${pct}%"></span></div></div>
    <ol class="plan">
      ${plan.steps.map((st, n) => `<li class="step st-${esc(st.state)}" data-step="${n + 1}">
        <button type="button" class="step-box" data-tick="${n + 1}" aria-label="Step ${n + 1}: ${esc(st.state)}. Toggle done" title="${st.state === "done" ? "Mark not done" : "Mark done"}">${st.state === "done" ? ICON.pass : st.state === "dropped" ? "–" : ""}</button>
        <div class="step-main">
          <span class="step-text">${esc(st.text)}</span>
          ${st.state === "doing" ? '<span class="step-state doing">doing</span>' : st.state === "dropped" ? '<span class="step-state dropped">dropped</span>' : ""}
          ${st.commit ? `<code class="step-commit" title="Commit">${esc(st.commit)}</code>` : ""}
          ${st.note && S.stepNote !== n + 1 ? `<div class="step-note">${esc(st.note)}</div>` : ""}
          ${S.stepNote === n + 1 ? `<div class="step-note-edit"><input class="input" id="step-note-input" value="${esc(st.note || "")}" placeholder="Note on this step (Enter saves, Esc cancels)"></div>` : ""}
        </div>
        <button type="button" class="btn btn-quiet btn-sm step-note-btn" data-note="${n + 1}" title="Comment on this step">Note</button>
      </li>`).join("")}
    </ol>
    ${plan.verification ? `<div class="plan-verify"><b>Verification</b> ${esc(plan.verification)}</div>` : ""}
  </section>`;
}

function groupHtml(i) {
  const parts = [];
  if (i.parent_info) {
    const pi = i.parent_info;
    parts.push(`<div class="part-of-line">${ICON.tree}Part of ${issueLink(pi.id)} <span class="pill s-${pi.status}">${STATUS_LABEL[pi.status]}</span> <span class="muted ellipsis">${esc(pi.title)}</span></div>`);
  }
  if ((i.merged_sources || []).length) {
    parts.push(`<div class="merged-sources">${ICON.merge}Merged into this: ${i.merged_sources.map((m) => `<span class="merged-src">${issueLink(m.id)} <button class="btn btn-sm btn-quiet" data-unmerge="${esc(m.id)}" title="Restore ${esc(m.id)}">Unmerge</button></span>`).join("")}</div>`);
  }
  if ((i.children || []).length) {
    const done = i.children.filter((c) => DONE.includes(c.status)).length;
    const allDone = done === i.children.length;
    parts.push(`<section class="section children-section" id="children-section">
      <div class="section-head"><h2>Children · ${done}/${i.children.length} done</h2></div>
      <div class="children">${i.children.map((c) => `<a class="child-row" href="#/${esc(S.slug)}/${esc(c.id)}">
        <span class="pill s-${c.status}">${STATUS_LABEL[c.status]}</span><span class="row-id">${esc(c.id)}</span>
        <span class="ellipsis">${esc(c.title)}</span>${progressBadge(c.plan_progress, ICON.plan, "Plan steps done", " plan-badge")}</a>`).join("")}</div>
      ${allDone && !DONE.includes(i.status) ? `<div class="close-offer">Every child is done. <button class="btn btn-sm btn-primary" data-act="close-parent">Close ${esc(i.id)}</button></div>` : ""}
    </section>`);
  }
  return parts.join("");
}

function detailHtml(i) {
  const loc = i.location || {};
  const hasLoc = Object.keys(loc).length > 0;
  const areas = S.project?.areas || [];
  const issueAtts = i.attachments.filter((a) => a.comment_id === null);
  const editing = S.editing;

  const title = editing === "title"
    ? `<input class="title-input" id="title-input" value="${esc(i.title)}" maxlength="300" aria-label="Title">`
    : `<h1 class="detail-title" id="detail-title" title="Click to edit">${esc(i.title)}</h1>`;

  const body = editing === "body"
    ? `<textarea class="textarea" id="body-input" rows="10" placeholder="Markdown: what happened, steps, expected vs actual">${esc(i.body)}</textarea>
       <div class="composer-row"><span class="hint">Markdown · <kbd>Ctrl</kbd>+<kbd>Enter</kbd> saves · <kbd>Esc</kbd> cancels</span>
       <button class="btn btn-sm" data-act="cancel-edit">Cancel</button><button class="btn btn-sm btn-primary" data-act="save-body">Save</button></div>`
    : `<div class="md" id="body-view">${i.body.trim() ? md(i.body) : '<p class="body-empty">No description.</p>'}</div>`;

  let location;
  if (editing === "location") {
    location = `<div class="card location-card">
      <div class="field"><span>Game commands <small class="muted">one place each, in order</small></span>
        <div class="cmd-editor" id="loc-commands">${commandEditorHtml()}</div></div>
      <div class="grid-3">
        <label class="field"><span>Action <small class="muted">a check that is something to do, not a place</small></span><input id="loc-action" value="${esc(loc.action || "")}"></label>
        <label class="field"><span>Place</span><input id="loc-place" value="${esc(loc.place || "")}"></label>
        <label class="field"><span>Time</span><input id="loc-time" value="${esc(loc.time || "")}"></label>
        <label class="field"><span>Weather</span><input id="loc-weather" value="${esc(loc.weather || "")}"></label>
        <label class="field"><span>World seed</span><input id="loc-seed" inputmode="numeric" class="mono" value="${esc(loc.seed ?? "")}" placeholder="${esc(S.project?.default_seed ?? "")}"></label>
        <label class="field"><span>X</span><input id="loc-x" inputmode="decimal" value="${esc(loc.x ?? "")}"></label>
        <label class="field"><span>Y</span><input id="loc-y" inputmode="decimal" value="${esc(loc.y ?? "")}"></label>
        <label class="field"><span>Z</span><input id="loc-z" inputmode="decimal" value="${esc(loc.z ?? "")}"></label>
        <label class="field"><span>Yaw</span><input id="loc-yaw" inputmode="decimal" value="${esc(loc.yaw ?? "")}"></label>
        <label class="field"><span>Pitch</span><input id="loc-pitch" inputmode="decimal" value="${esc(loc.pitch ?? "")}"></label>
      </div>
      <div class="composer-row"><span class="hint"></span><button class="btn btn-sm" data-act="cancel-edit">Cancel</button><button class="btn btn-sm btn-primary" data-act="save-location">Save location</button></div>
    </div>`;
  } else if (hasLoc) {
    const facts = locationFacts(loc);
    const cmds = gameCommands(loc);
    location = `<div class="card location-card">
      ${cmds.length ? cmds.map((c, n) => `<div class="loc-cmd" data-n="${n}">
        ${cmds.length > 1 || c.label ? `<div class="cmd-head">${cmds.length > 1 ? `<span class="cmd-num">${n + 1}</span>` : ""}${c.label ? `<span class="cmd-label">${esc(c.label)}</span>` : ""}</div>` : ""}
        <div class="cmd-box"><code${n === 0 ? ' id="loc-cmd"' : ""}>${esc(c.command)}</code></div>
        <div class="cmd-actions">
          <button class="btn btn-sm" data-act="copy-cmd" data-n="${n}" title="Copy${n === 0 ? " (y)" : ""}">${ICON.copy}Copy</button>
          <button class="btn btn-sm btn-primary" data-act="send-cmd" data-n="${n}" title="Send to game${n === 0 ? " (s)" : ""}">${ICON.send}Send to game</button>
          <span class="send-slot" data-n="${n}">${sendStateHtml(sentFor(i, c.command))}</span>
        </div>
      </div>`).join("") : '<div class="empty-hint">No game command yet.</div>'}
      ${facts.length ? `<div class="loc-facts">${facts.map(([k, v]) => `<span class="fact"><b>${esc(k)}</b>${esc(v)}</span>`).join("")}</div>` : ""}
    </div>`;
  } else {
    location = '<div class="empty-hint">No location. Add the game command that takes you there, so a click sends you back.</div>';
  }

  return `
    <div class="detail-head">
      <button class="icon-btn back-btn" data-act="close" aria-label="Back to list">${ICON.back}</button>
      <select class="status-select s-${i.status}" id="status-select" aria-label="Status">
        ${STATUSES.map((s) => `<option value="${s}"${s === i.status ? " selected" : ""}>${STATUS_LABEL[s]}</option>`).join("")}
      </select>
      <button class="btn btn-quiet btn-sm row-id" data-act="copy-id" title="Copy id">${esc(i.id)}</button>
      <span class="head-spacer"></span>
      <button class="icon-btn" data-act="copy-link" title="Copy link">${ICON.link}</button>
      <button class="icon-btn" data-act="delete" title="Delete issue">${ICON.trash}</button>
      <button class="icon-btn" data-act="close" title="Close (Esc)" aria-label="Close">${ICON.close}</button>
    </div>
    ${title}
    ${i.merged_into ? `<div class="banner">${ICON.merge}Merged into ${issueLink(i.merged_into)}. <button class="btn btn-sm" data-unmerge="${esc(i.id)}">Unmerge</button></div>` : ""}
    <div class="fields">
      <label class="field"><span>Kind</span><select data-field="kind">${KINDS.map((k) => `<option value="${k}"${k === i.kind ? " selected" : ""}>${KIND_LABEL[k]}</option>`).join("")}</select></label>
      <label class="field"><span>Priority</span><select data-field="priority">${PRIORITIES.map((p) => `<option value="${p}"${p === i.priority ? " selected" : ""}>${PRIORITY_LABEL[p]}</option>`).join("")}</select></label>
      <label class="field"><span>Area</span><input data-field="area" value="${esc(i.area)}" list="area-list" placeholder="e.g. terrain"></label>
      <label class="field"><span>Tags</span><input data-field="tags" value="${esc(i.tags.join(", "))}" placeholder="comma separated"></label>
      <label class="field"><span>Source</span><select data-field="source">${SOURCES.map((s) => `<option value="${s}"${s === i.source ? " selected" : ""}>${s}</option>`).join("")}</select></label>
      <label class="field"><span>Reference</span><input data-field="external_ref" value="${esc(i.external_ref)}" placeholder="TODO item, commit" class="mono"></label>
      <label class="field"><span>Size</span><select data-field="size"><option value=""${i.size ? "" : " selected"}>–</option>${SIZES.map((z) => `<option value="${z}"${z === i.size ? " selected" : ""}>${z}</option>`).join("")}</select></label>
      <label class="field"><span>Milestone</span><input data-field="milestone" value="${esc(i.milestone || "")}" list="milestone-list" placeholder="e.g. World 1"></label>
      <label class="field"><span>Part of</span><input id="parent-input" value="${esc(i.parent || "")}" placeholder="e.g. ${esc(S.project?.prefix || "MG")}-4" class="mono"></label>
    </div>
    <datalist id="milestone-list">${Object.keys(S.list.counts?.milestone || {}).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
    <datalist id="area-list">${areas.map((a) => `<option value="${esc(a)}">`).join("")}</datalist>
    <div class="meta-line">
      <span title="${esc(fullTime(i.created_at))}">Filed ${ago(i.created_at)} by ${esc(i.source)}</span>
      <span title="${esc(fullTime(i.updated_at))}">Updated ${ago(i.updated_at)}</span>
      ${i.closed_at ? `<span title="${esc(fullTime(i.closed_at))}">Done ${ago(i.closed_at)}</span>` : ""}
    </div>

    <section class="section">
      <div class="section-head"><h2>Description</h2>${editing === "body" ? "" : '<button class="btn btn-quiet btn-sm" data-act="edit-body" title="Edit (e)">Edit</button>'}</div>
      ${body}
    </section>

    ${planHtml(i)}

    ${groupHtml(i)}

    ${buildHtml(i)}

    <section class="section">
      <div class="section-head"><h2>Location</h2>${editing === "location" ? "" : `<button class="btn btn-quiet btn-sm" data-act="edit-location">${hasLoc ? "Edit" : "Add"}</button>`}</div>
      ${location}
    </section>

    <section class="section">
      <div class="section-head"><h2>Attachments${issueAtts.length ? ` · ${issueAtts.length}` : ""}</h2></div>
      ${issueAtts.length ? `<div class="gallery">${issueAtts.map((a) => attachmentTile(a)).join("")}</div>` : ""}
      <label class="dropzone" id="issue-dropzone">Drop screenshots here, paste with <kbd>Ctrl</kbd>+<kbd>V</kbd>, or <u>browse</u>
        <input type="file" id="issue-file" multiple hidden></label>
    </section>

    <section class="section">
      <div class="section-head"><h2>Activity</h2></div>
      <div class="timeline">${timelineHtml(i)}</div>
    </section>`;
}

function renderComposer() {
  const d = draft(S.issue.id);
  $("#composer").innerHTML = `
    <textarea class="textarea" id="comment-input" rows="2" placeholder="Comment, or say what is still broken… (paste or drop screenshots)">${esc(d.text)}</textarea>
    <div class="pending" id="pending"></div>
    <div class="composer-row">
      <span class="hint"><kbd>Ctrl</kbd>+<kbd>Enter</kbd> comment</span>
      <button class="btn btn-pass" data-verdict="passed" title="Passed (p)">${ICON.pass}Passed</button>
      <button class="btn btn-fail" data-verdict="failed" title="Still broken (f)">${ICON.fail}Still broken</button>
      <button class="btn btn-primary" data-verdict="" title="Comment (Ctrl+Enter)">Comment</button>
    </div>`;
  renderPending();
}

function renderPending() {
  const box = $("#pending");
  if (!box || !S.issue) return;
  const d = draft(S.issue.id);
  box.innerHTML = d.files.map((f, n) => `<div class="pending-item" title="${esc(f.file.name)}">
    ${f.url ? `<img src="${f.url}" alt="">` : esc(f.file.name.slice(-12))}
    <button data-unpend="${n}" aria-label="Remove">✕</button></div>`).join("");
}

function addPending(files) {
  if (!S.issue || !files.length) return;
  const d = draft(S.issue.id);
  for (const file of files) d.files.push({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : null });
  renderPending();
  toast(`${files.length} file${files.length > 1 ? "s" : ""} ready to post with your comment`);
}

async function refreshIssue() {
  if (!S.openId) return;
  const id = S.openId, slug = S.slug;
  try {
    const issue = await api("GET", `/api/issues/${encodeURIComponent(id)}`);
    if (S.openId !== id || S.slug !== slug) return;
    if (issue.id !== S.openId) { go(S.slug, issue.id); return; }
    S.issue = issue;
    renderDetail(false);
    S.seen = timelineKeys(issue);
  } catch (e) { if (S.openId === id && S.slug === slug) fail(e); }
}

async function refreshAll() {
  await Promise.all([refreshIssue(), loadList(), refreshProject()]);
}

async function refreshProject() {
  if (!S.slug) return;
  const slug = S.slug;
  try {
    const before = JSON.stringify(S.project?.build ?? null) + S.project?.builds_enabled;
    const project = await api("GET", `/api/projects/${encodeURIComponent(slug)}`);
    if (slug !== S.slug) return;
    S.project = project;
    S.lastChange = S.project.last_change;
    // A new build: the filter bar shows it (the stamped issues refresh through their own activity).
    if (JSON.stringify(S.project.build ?? null) + S.project.builds_enabled !== before && S.list) renderFilters();
    if (!S.openId) renderEmptyDetail();
  } catch (e) { /* the poller reports connection problems */ }
}

async function patchIssue(changes, label) {
  if (!S.issue) return;
  try {
    S.issue = await api("PATCH", `/api/issues/${encodeURIComponent(S.issue.id)}`, { ...changes, actor: AUTHOR });
    S.editing = null;
    renderDetail(false);
    if (label) toast(label);
    loadList();
    refreshProject();
  } catch (e) { fail(e); }
}

async function postComment(verdict) {
  if (!S.issue) return;
  const id = S.issue.id;
  const d = draft(id);
  const input = $("#comment-input");
  const text = input ? input.value : d.text;
  if (!text.trim() && !verdict && !d.files.length) { input?.focus(); return; }
  const buttons = $$("#composer button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const attachments = await Promise.all(d.files.map((f) => fileToAttachment(f.file)));
    await api("POST", `/api/issues/${encodeURIComponent(id)}/comments`, { author: AUTHOR, text, verdict: verdict || undefined, attachments });
    d.files.forEach((f) => f.url && URL.revokeObjectURL(f.url));
    S.drafts.delete(id);
    toast(verdict === "passed" ? `${id} passed` : verdict === "failed" ? `${id} marked still broken` : "Comment posted");
    const idx = S.list.issues.findIndex((i) => i.id === id);
    await loadList();
    await refreshProject();
    const stillListed = S.list.issues.some((i) => i.id === id);
    // Verdicts walk the owner through the queue: move to the next item when this one left the view.
    if (verdict === "passed" && !stillListed && S.list.issues.length && idx >= 0) {
      const next = S.list.issues[Math.min(idx, S.list.issues.length - 1)];
      go(S.slug, next.id);
      return;
    }
    if (S.openId === id) {
      S.issue = await api("GET", `/api/issues/${encodeURIComponent(id)}`);
      renderDetail(false);
      renderComposer();
      if (verdict === "failed" && !text.trim()) {
        $("#comment-input").placeholder = "What is still broken? (optional, helps the agent)";
        $("#comment-input").focus();
      }
    }
  } catch (e) {
    fail(e);
    buttons.forEach((b) => { b.disabled = false; });
  }
}

async function uploadToIssue(files) {
  if (!S.issue || !files.length) return;
  try {
    const attachments = await Promise.all(files.map(fileToAttachment));
    await api("POST", `/api/issues/${encodeURIComponent(S.issue.id)}/attachments`, { attachments, author: AUTHOR });
    toast(`Attached ${files.length} file${files.length > 1 ? "s" : ""}`);
    await refreshIssue();
    loadList();
  } catch (e) { fail(e); }
}

// -- send to game --------------------------------------------------------------------------
async function sendToGame(n = 0) {
  const cmd = gameCommand(S.issue?.location, n);
  if (!cmd) { toast("This issue has no game command", "error"); return; }
  try {
    const c = await api("POST", `/api/projects/${encodeURIComponent(S.slug)}/commands`, { command: cmd, issue: S.issue.id, author: AUTHOR });
    S.issue.commands = [c, ...(S.issue.commands || [])];
    renderSendStates();
    toast("Sent. Waiting for the game to pick it up…");
    pollCommand(c.id, S.issue.id);
  } catch (e) { fail(e); }
}

/** Each location command's line under its buttons: the newest send of that command and whether the game took it. */
function renderSendStates() {
  const cmds = gameCommands(S.issue?.location);
  $$(".send-slot").forEach((el) => {
    const c = cmds[+el.dataset.n];
    el.innerHTML = c ? sendStateHtml(sentFor(S.issue, c.command)) : "";
  });
}

function pollCommand(cid, issueId) {
  if (S.polls.has(cid)) return;
  const started = Date.now();
  const tick = async () => {
    let c;
    try { c = await api("GET", `/api/commands/${cid}`); } catch (e) { c = null; }
    if (c && S.issue && S.issue.id === issueId) {
      const at = (S.issue.commands || []).findIndex((x) => x.id === cid);
      if (at >= 0) { S.issue.commands[at] = c; renderSendStates(); }
    }
    if (c && c.state === "delivered") {
      toast(`${issueId}: picked up by ${c.client || "the game"}`);
      S.polls.delete(cid);
      if (S.issue && S.issue.id === issueId && !S.editing) refreshIssue();
      return;
    }
    if ((c && c.state === "expired") || Date.now() - started > 11 * 60 * 1000) {
      S.polls.delete(cid);
      return;
    }
    S.polls.set(cid, setTimeout(tick, 1500));
  };
  S.polls.set(cid, setTimeout(tick, 1200));
}

function resumeSendPolling() {
  for (const c of S.issue?.commands || []) if (c.state === "pending") pollCommand(c.id, S.issue.id);
}

// ------------------------------------------------------------------------------ handoff
async function openHandoff(version) {
  try {
    const q = version ? `?version=${encodeURIComponent(version)}` : "";
    const [h, hist] = await Promise.all([
      api("GET", `/api/projects/${encodeURIComponent(S.slug)}/handoff${q}`),
      api("GET", `/api/projects/${encodeURIComponent(S.slug)}/handoff/history`),
    ]);
    S.openId = null;
    S.issue = null;
    S.handoff = { ...h, history: hist.versions, viewing: version || null, diff: null };
    if (!version) S.editing = null;
    document.body.classList.add("detail-open");
    $$(".issue-row.selected").forEach((r) => r.classList.remove("selected"));
    renderHandoff();
  } catch (e) { fail(e); }
}

function renderHandoff() {
  const h = S.handoff;
  const root = $("#detail");
  root.dataset.issue = HANDOFF;
  const editing = S.editing === "handoff";
  const order = h.section_order || [];
  const extra = Object.keys(h.sections).filter((k) => !order.includes(k));
  const view = h.version ? [...order, ...extra].map((name) => `<section class="section handoff-section">
      <div class="section-head"><h2>${esc(name)}</h2></div>
      ${(h.sections[name] || "").trim() ? `<div class="md">${md(h.sections[name])}</div>` : '<div class="empty-hint">Empty.</div>'}
    </section>`).join("") : `<div class="empty-hint">No handoff yet. Agents write it with <code>set_handoff</code> before a session ends; you can start one here.</div>`;
  root.innerHTML = `<div class="detail-scroll"><div class="detail-inner handoff">
    <div class="detail-head">
      <button class="icon-btn back-btn" data-act="close" aria-label="Back to list">${ICON.back}</button>
      <h1 class="detail-title">${ICON.doc} Handoff</h1>
      <span class="head-spacer"></span>
      ${h.history?.length ? `<select class="filter-select" id="handoff-version" aria-label="Version">
        ${h.history.map((v) => `<option value="${v.version}"${v.version === h.version ? " selected" : ""}>v${v.version} · ${esc(v.author)} · ${esc(ago(v.created_at))}</option>`).join("")}
      </select>` : ""}
      ${editing ? "" : `<button class="btn btn-sm" data-act="edit-handoff">Edit</button>`}
      <button class="icon-btn" data-act="close" title="Close (Esc)" aria-label="Close">${ICON.close}</button>
    </div>
    ${h.version ? `<div class="meta-line"><span>Version ${h.version} of ${h.versions}</span><span title="${esc(fullTime(h.created_at))}">saved ${ago(h.created_at)} by ${esc(h.author)}</span>${h.note ? `<span>${esc(h.note)}</span>` : ""}
      ${h.version > 1 ? `<button class="btn btn-sm btn-quiet" data-act="handoff-diff">${h.diff ? "Hide changes" : `Changes since v${h.version - 1}`}</button>` : ""}</div>` : ""}
    ${h.diff ? `<pre class="diff">${h.diff.split("\n").map((l) => `<span class="${l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : ""}">${esc(l)}</span>`).join("\n")}</pre>` : ""}
    ${editing ? `<textarea class="textarea mono" id="handoff-input" rows="24">${esc(h.markdown || order.map((n) => `## ${n}\n\n`).join("\n"))}</textarea>
      <div class="composer-row"><span class="hint">Markdown, one <code>## </code> heading per section · <kbd>Ctrl</kbd>+<kbd>Enter</kbd> saves</span>
      <button class="btn btn-sm" data-act="cancel-handoff">Cancel</button><button class="btn btn-sm btn-primary" data-act="save-handoff">Save version</button></div>` : view}
  </div></div>`;
}

async function saveHandoff() {
  const text = $("#handoff-input")?.value ?? "";
  try {
    await api("POST", `/api/projects/${encodeURIComponent(S.slug)}/handoff`, { markdown: text, author: AUTHOR });
    S.editing = null;
    toast("Handoff saved");
    await openHandoff();
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------------------ merge and group
function selectionItems() {
  const byId = new Map(S.list.issues.map((i) => [i.id, i]));
  return [...S.selected].map((id) => byId.get(id)).filter(Boolean)
    .sort((a, b) => (a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.number - b.number));
}

function openMergeDialog(mode = "merge") {
  const items = selectionItems();
  if (items.length < 2) { toast("Select at least two items (x, or the checkboxes)", "error"); return; }
  const dlg = $("#merge-dialog");
  const merge = mode === "merge";
  dlg.innerHTML = `<form method="dialog" id="merge-form">
    <div class="dialog-head"><h2>${merge ? "Merge into…" : "Group under…"}</h2><button class="icon-btn" value="cancel" formnovalidate aria-label="Close">${ICON.close}</button></div>
    <div class="dialog-body">
      <p class="empty-hint">${merge
        ? "Pick the item to keep. The others' descriptions, comments, screenshots, locations and activity move into its timeline, marked where they came from; they close and their ids point to it. Unmerge restores one."
        : "Pick the parent. The others become part of it; it shows their statuses and offers to close itself when they are all done."}</p>
      <div class="target-list">${items.map((i, n) => `<label class="target"><input type="radio" name="target" value="${esc(i.id)}"${n === 0 ? " checked" : ""}>
        <span class="row-id">${esc(i.id)}</span><span class="pill s-${i.status}">${STATUS_LABEL[i.status]}</span><span class="ellipsis">${esc(i.title)}</span>
        <span class="muted">${n === 0 ? "oldest · " : ""}${esc(ago(i.created_at))}</span></label>`).join("")}</div>
      <div class="form-error" id="merge-error"></div>
    </div>
    <div class="dialog-foot"><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn btn-primary" value="ok">${merge ? `Merge ${items.length - 1} into it` : `Group ${items.length - 1} under it`}</button></div>
  </form>`;
  const f = $("#merge-form");
  f.addEventListener("submit", async (ev) => {
    if (ev.submitter?.value !== "ok") return;
    ev.preventDefault();
    const target = f.target.value;
    const others = items.map((i) => i.id).filter((id) => id !== target);
    try {
      if (merge) {
        await api("POST", `/api/issues/${encodeURIComponent(target)}/merge`, { sources: others, actor: AUTHOR });
      } else {
        for (const id of others) await api("POST", `/api/issues/${encodeURIComponent(id)}/parent`, { parent: target, actor: AUTHOR });
      }
      dlg.close();
      S.selected.clear();
      toast(merge ? `Merged ${others.join(", ")} into ${target}` : `${others.join(", ")} now part of ${target}`);
      await Promise.all([loadList(), refreshProject()]);
      go(S.slug, target);
    } catch (e) { $("#merge-error").textContent = e.message; }
  });
  dlg.showModal();
}

async function unmergeIssue(id) {
  try {
    await api("POST", `/api/issues/${encodeURIComponent(id)}/unmerge`, { actor: AUTHOR });
    toast(`${id} restored`);
    await Promise.all([refreshIssue(), loadList(), refreshProject()]);
  } catch (e) { fail(e); }
}

async function setStep(index, changes) {
  if (!S.issue) return;
  try {
    S.issue = await api("PATCH", `/api/issues/${encodeURIComponent(S.issue.id)}/plan/steps/${index}`, { ...changes, actor: AUTHOR });
    S.stepNote = null;
    renderDetail(false);
    loadList();
  } catch (e) { fail(e); }
}

async function setParent(value) {
  const parent = value.trim().toUpperCase() || null;
  if ((S.issue.parent || null) === parent) return;
  try {
    S.issue = await api("POST", `/api/issues/${encodeURIComponent(S.issue.id)}/parent`, { parent, actor: AUTHOR });
    renderDetail(false);
    toast(parent ? `Part of ${parent}` : "Removed from its parent");
    loadList();
  } catch (e) { fail(e); renderDetail(false); }
}

function toggleSelect(id, on) {
  if (on ?? !S.selected.has(id)) S.selected.add(id); else S.selected.delete(id);
  const row = $(`.issue-row[data-id="${CSS.escape(id)}"]`);
  if (row) {
    row.classList.toggle("checked", S.selected.has(id));
    const box = $("input[data-select]", row);
    if (box) box.checked = S.selected.has(id);
  }
  renderSelectionBar();
}

function selectRange(toIdx) {
  const from = S.anchor >= 0 ? S.anchor : toIdx;
  const [a, b] = from < toIdx ? [from, toIdx] : [toIdx, from];
  for (let n = a; n <= b; n++) if (S.list.issues[n]) toggleSelect(S.list.issues[n].id, true);
}

// ------------------------------------------------------------------------------ live updates
// The desk streams its change feed (Server-Sent Events); the list, the open issue and the handoff
// refresh as soon as the game, an agent or another tab writes. Polling stays as the fallback.
let liveTimer = null;
const liveQueue = { issues: new Set(), handoff: false, all: false };

function showLiveState(ok) {
  S.liveOk = ok;
  document.body.dataset.live = ok ? "sse" : "poll";
  const el = $(".live-state");
  if (el) {
    el.textContent = ok ? "● live" : "○ polling";
    el.title = ok ? "Live: changes appear as they happen" : "Checking for changes every few seconds";
  }
}

function connectLive(slug) {
  if (S.live) { S.live.close(); S.live = null; }
  showLiveState(false);
  if (typeof SharedWorker === "undefined") return; // short polling needs no persistent connection
  try {
    const worker = new SharedWorker("./live-worker.js", { name: "pairdesk-live" });
    const port = worker.port;
    const subscribe = () => port.postMessage({ type: "subscribe", slug });
    const heartbeat = setInterval(subscribe, 30000);
    const live = S.live = {
      subscribe,
      close() { clearInterval(heartbeat); port.postMessage({ type: "close" }); port.close(); },
    };
    worker.onerror = () => { if (S.live === live) { live.close(); S.live = null; showLiveState(false); } };
    port.onmessage = ({ data: message }) => {
      if (S.live !== live || S.slug !== slug) return;
      const { type, data } = message;
      if (type === "hello") {
        const wasDown = !S.liveOk;
        showLiveState(true);
        if (wasDown) scheduleLive({ all: true });
      } else if (type === "change") {
        const issues = new Set(), handoff = data.changes.some(c => c.type === "handoff");
        data.changes.forEach(c => c.issue && issues.add(c.issue));
        scheduleLive({ issues, handoff });
      } else if (type === "refresh") scheduleLive({ all: true });
      else if (type === "offline") showLiveState(false);
    };
    port.start();
    subscribe();
  } catch { showLiveState(false); }
}

function scheduleLive({ issues, handoff, all }) {
  issues?.forEach((i) => liveQueue.issues.add(i));
  if (handoff) liveQueue.handoff = true;
  if (all) liveQueue.all = true;
  clearTimeout(liveTimer);
  liveTimer = setTimeout(applyLive, 120);
}

async function applyLive() {
  const { issues, handoff, all } = liveQueue;
  const touched = new Set(issues);
  liveQueue.issues = new Set(); liveQueue.handoff = false; liveQueue.all = false;
  await loadList();
  refreshProject();
  loadProjects().catch(() => {});
  const active = document.activeElement;
  const busy = S.editing || S.stepNote || (active && $("#detail-inner")?.contains(active) && isTyping(active));
  if (S.openId && !busy && (all || touched.has(S.openId) || (S.issue?.children || []).some((c) => touched.has(c.id)) || (S.issue?.merged_sources || []).some((m) => touched.has(m.id)))) {
    await refreshIssue();
  }
  if (S.handoff && !S.editing && (all || handoff)) await openHandoff(S.handoff.viewing);
}

// ------------------------------------------------------------------------------ splitter
function applySplit(ratio) {
  const layout = $("#layout");
  if (ratio == null) {
    layout.style.removeProperty("--split-l");
    layout.style.removeProperty("--split-r");
  } else {
    layout.style.setProperty("--split-l", `${ratio}fr`);
    layout.style.setProperty("--split-r", `${1 - ratio}fr`);
  }
  const sp = $("#splitter");
  sp.setAttribute("aria-valuenow", String(Math.round(100 * (ratio ?? SPLIT_DEFAULT))));
}

function clampSplit(ratio) {
  const w = $("#layout").clientWidth || window.innerWidth;
  const min = Math.min(0.9, SPLIT_MIN_LIST / w), max = Math.max(0.1, 1 - SPLIT_MIN_DETAIL / w);
  return Math.min(Math.max(ratio, min), Math.max(min, max));
}

function setSplit(ratio, save = true) {
  const r = clampSplit(ratio);
  applySplit(r);
  if (save) { try { localStorage.setItem("pairdesk.split", String(r)); } catch (e) { /* storage blocked */ } }
  return r;
}

function currentSplit() {
  const list = $("#list-pane").getBoundingClientRect().width, all = $("#layout").getBoundingClientRect().width;
  return all ? list / all : SPLIT_DEFAULT;
}

function bindSplitter() {
  const sp = $("#splitter");
  let saved = null;
  try { saved = parseFloat(localStorage.getItem("pairdesk.split")); } catch (e) { saved = null; }
  if (Number.isFinite(saved)) setSplit(saved, false); else applySplit(null);
  sp.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    sp.setPointerCapture(e.pointerId);
    document.body.classList.add("resizing");
    const rect = $("#layout").getBoundingClientRect();
    const move = (ev) => setSplit((ev.clientX - rect.left) / rect.width);
    const up = () => {
      document.body.classList.remove("resizing");
      sp.removeEventListener("pointermove", move);
      sp.removeEventListener("pointerup", up);
      sp.removeEventListener("pointercancel", up);
    };
    sp.addEventListener("pointermove", move);
    sp.addEventListener("pointerup", up);
    sp.addEventListener("pointercancel", up);
  });
  sp.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 0.1 : 0.02;
    if (e.key === "ArrowLeft") { e.preventDefault(); setSplit(currentSplit() - step); }
    else if (e.key === "ArrowRight") { e.preventDefault(); setSplit(currentSplit() + step); }
    else if (e.key === "Home" || e.key === "Enter") { e.preventDefault(); resetSplit(); }
  });
  sp.addEventListener("dblclick", resetSplit);
}

function resetSplit() {
  try { localStorage.removeItem("pairdesk.split"); } catch (e) { /* storage blocked */ }
  applySplit(null);
}

// ------------------------------------------------------------------------------ new issue dialog
function parseQuick(text) {
  const out = { title: [], tags: [] };
  for (const tok of text.split(/\s+/)) {
    let m;
    if ((m = /^!(p[0-3])$/i.exec(tok))) out.priority = m[1].toLowerCase();
    else if ((m = /^\+(bug|check|idea|task)$/i.exec(tok))) out.kind = m[1].toLowerCase();
    else if ((m = /^@(\S+)$/.exec(tok))) out.area = m[1].replace(/_/g, " ");
    else if ((m = /^#(\S+)$/.exec(tok)) && !/^#\d+$/.test(tok)) out.tags.push(m[1]);
    else if (tok) out.title.push(tok);
  }
  out.title = out.title.join(" ");
  return out;
}

function segmented(name, values, labels, checked, icons) {
  return `<div class="segmented">${values.map((v) => `<label><input type="radio" name="${name}" value="${v}"${v === checked ? " checked" : ""}><span>${icons ? `<span class="kind-icon kind-${v}">${ICON[v]}</span>` : ""}${labels[v]}</span></label>`).join("")}</div>`;
}

function openIssueDialog(prefill = {}) {
  if (!S.project) { openProjectDialog(); return; }
  const dlg = $("#issue-dialog");
  S.dialogFiles = [];
  const areas = S.project.areas || [];
  dlg.innerHTML = `
    <form method="dialog" id="issue-form" autocomplete="off">
      <div class="dialog-head"><h2>New issue in ${esc(S.project.name)}</h2><button class="icon-btn" value="cancel" formnovalidate aria-label="Close">${ICON.close}</button></div>
      <div class="dialog-body">
        <label class="field"><span>Title</span><input name="title" required maxlength="300" value="${esc(prefill.title || "")}" placeholder="What did you see?" autofocus></label>
        <div class="field"><span>Kind</span>${segmented("kind", KINDS, KIND_LABEL, prefill.kind || "bug", true)}</div>
        <div class="grid-2">
          <div class="field"><span>Priority</span>${segmented("priority", PRIORITIES, { p0: "P0", p1: "P1", p2: "P2", p3: "P3" }, prefill.priority || "p2")}</div>
          <label class="field"><span>Status</span><select name="status">${STATUSES.map((s) => `<option value="${s}"${s === "reported" ? " selected" : ""}>${STATUS_LABEL[s]}</option>`).join("")}</select></label>
        </div>
        <div class="grid-2">
          <label class="field"><span>Area</span><input name="area" list="dlg-areas" value="${esc(prefill.area || "")}" placeholder="e.g. terrain"></label>
          <label class="field"><span>Tags</span><input name="tags" value="${esc((prefill.tags || []).join(", "))}" placeholder="comma separated"></label>
        </div>
        <datalist id="dlg-areas">${areas.map((a) => `<option value="${esc(a)}">`).join("")}</datalist>
        <label class="field"><span>Description</span><textarea class="textarea" name="body" rows="6" placeholder="Markdown: what happened, steps, expected vs actual"></textarea></label>
        <div class="grid-2">
          <label class="field"><span>Game command (location)</span><input name="command" class="mono" placeholder="/goto 1240 -380 yaw 90; /time 17:30"></label>
          <label class="field"><span>World seed</span><input name="seed" class="mono" inputmode="numeric" pattern="[+-]?[0-9]{1,11}" placeholder="${esc(S.project.default_seed ?? "e.g. 1234")}"></label>
        </div>
        <div class="dropzone" id="dlg-drop">Paste or drop screenshots, or <label><u>browse</u><input type="file" id="dlg-file" multiple hidden></label></div>
        <div class="pending" id="dlg-pending"></div>
        <div class="form-error" id="issue-error"></div>
      </div>
      <div class="dialog-foot"><span class="hint"><kbd>Ctrl</kbd>+<kbd>Enter</kbd> create</span><button class="btn" value="cancel" formnovalidate>Cancel</button><button class="btn btn-primary" value="ok">Create issue</button></div>
    </form>`;
  const f = $("#issue-form");
  const renderDlgPending = () => {
    $("#dlg-pending").innerHTML = S.dialogFiles.map((x, n) => `<div class="pending-item" title="${esc(x.file.name)}">${x.url ? `<img src="${x.url}" alt="">` : esc(x.file.name.slice(-12))}<button type="button" data-undlg="${n}" aria-label="Remove">✕</button></div>`).join("");
  };
  S.addDialogFiles = (files) => {
    for (const file of files) S.dialogFiles.push({ file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : null });
    renderDlgPending();
  };
  $("#dlg-file").addEventListener("change", (e) => { S.addDialogFiles(Array.from(e.target.files)); e.target.value = ""; });
  $("#dlg-pending").addEventListener("click", (e) => {
    const n = e.target.dataset.undlg;
    if (n !== undefined) { const [x] = S.dialogFiles.splice(+n, 1); if (x?.url) URL.revokeObjectURL(x.url); renderDlgPending(); }
  });
  const drop = $("#dlg-drop");
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("drag-over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("drag-over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("drag-over"); S.addDialogFiles(filesFrom(e.dataTransfer)); });
  f.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); f.requestSubmit(f.querySelector('button[value="ok"]')); } });
  f.addEventListener("submit", async (ev) => {
    if (ev.submitter?.value !== "ok") { S.dialogFiles.forEach((x) => x.url && URL.revokeObjectURL(x.url)); return; }
    ev.preventDefault();
    const btn = ev.submitter;
    btn.disabled = true;
    try {
      const attachments = await Promise.all(S.dialogFiles.map((x) => fileToAttachment(x.file)));
      const issue = await api("POST", `/api/projects/${encodeURIComponent(S.slug)}/issues`, {
        title: f.title.value, kind: f.kind.value, priority: f.priority.value, status: f.status.value,
        area: f.area.value, tags: f.tags.value, body: f.body.value, command: f.command.value,
        ...(f.seed.value.trim() ? { location: { seed: f.seed.value.trim() } } : {}),
        source: "owner", author: AUTHOR, attachments,
      });
      S.dialogFiles.forEach((x) => x.url && URL.revokeObjectURL(x.url));
      S.dialogFiles = [];
      dlg.close();
      toast(`${issue.id} created`);
      await Promise.all([loadList(), refreshProject()]);
      go(S.slug, issue.id);
    } catch (e) {
      $("#issue-error").textContent = e.message;
      btn.disabled = false;
    }
  });
  dlg.showModal();
}

// Screenshots pasted or dropped onto the quick-add bar ride along with the new report.
let quickFiles = [];
function setQuickFiles(files) {
  quickFiles = files;
  const chip = $("#quick-files");
  chip.hidden = !files.length;
  chip.textContent = files.length === 1 ? "1 screenshot ×" : `${files.length} screenshots ×`;
}
function addQuickFiles(files) {
  setQuickFiles(quickFiles.concat(files));
  $("#quick-title").focus();
}

async function quickAdd(ev) {
  ev.preventDefault();
  const input = $("#quick-title");
  const q = parseQuick(input.value.trim());
  if (!q.title) { input.focus(); return; }
  try {
    const attachments = await Promise.all(quickFiles.map(fileToAttachment));
    const issue = await api("POST", `/api/projects/${encodeURIComponent(S.slug)}/issues`, {
      title: q.title, kind: q.kind || "bug", priority: q.priority || "p2", area: q.area || "", tags: q.tags,
      status: "reported", source: "owner", author: AUTHOR, attachments,
    });
    input.value = "";
    setQuickFiles([]);
    toast(`${issue.id} added: ${issue.title}` + (attachments.length ? ` (${attachments.length} screenshot${attachments.length > 1 ? "s" : ""})` : ""));
    await Promise.all([loadList(), refreshProject()]);
  } catch (e) { fail(e); }
}

// ------------------------------------------------------------------------------ help + lightbox
function openHelp() {
  const rows = [
    ["j / ↓", "Next issue"], ["k / ↑", "Previous issue"], ["Enter / o", "Open issue"], ["Esc", "Close / cancel"],
    ["p", "Passed (posts the comment box text too)"], ["f", "Still broken"], ["c", "Write a comment"],
    ["s", "Send the first location command to the game"], ["y", "Copy the first location command"], ["e", "Edit the description"],
    ["x", "Select the issue under the cursor"], ["Shift+click", "Select a range"], ["m", "Merge the selected issues"],
    ["b", "Switch triage / backlog view"], ["h", "Project handoff"],
    ["n", "New issue"], ["q", "Quick add"], ["/", "Search"], ["r", "Refresh"], ["1 - 9", "Show one status (1 = To check)"],
    ["0", "Show all statuses"], ["t", "Switch theme"], ["?", "This help"],
  ];
  const dlg = $("#help-dialog");
  dlg.innerHTML = `<form method="dialog">
    <div class="dialog-head"><h2>Keyboard</h2><button class="icon-btn" value="close" aria-label="Close">${ICON.close}</button></div>
    <div class="dialog-body"><div class="keys">${rows.map(([k, v]) => `<kbd>${esc(k)}</kbd><span>${esc(v)}</span>`).join("")}</div>
    <p class="empty-hint">In the comment box, <kbd>Ctrl</kbd>+<kbd>Enter</kbd> posts. Paste screenshots anywhere in an open issue.</p></div></form>`;
  dlg.showModal();
}

const LB = { list: [], idx: 0 };
function openLightbox(src) {
  const all = [];
  if (S.issue) {
    for (const a of S.issue.attachments) if (a.is_image) all.push({ url: a.url, name: a.filename });
  }
  if (!all.some((x) => x.url === src)) all.unshift({ url: src, name: src.split("/").pop() });
  LB.list = all;
  LB.idx = all.findIndex((x) => x.url === src);
  renderLightbox();
  $("#lightbox").hidden = false;
}
function renderLightbox() {
  const cur = LB.list[LB.idx];
  if (!cur) return;
  $("#lightbox").innerHTML = `
    <div class="lightbox-bar">
      <span>${esc(cur.name)}</span><span style="opacity:.6">${LB.idx + 1} / ${LB.list.length}</span>
      <a href="${cur.url}" target="_blank" rel="noopener">Open original</a>
      <span class="row-spacer"></span>
      <button data-lb="prev" aria-label="Previous">‹</button><button data-lb="next" aria-label="Next">›</button>
      <button data-lb="close" aria-label="Close">✕</button>
    </div>
    <div class="lightbox-stage" data-lb="close"><img src="${cur.url}" alt="${esc(cur.name)}"></div>`;
}
function lightboxStep(d) {
  if (!LB.list.length) return;
  LB.idx = (LB.idx + d + LB.list.length) % LB.list.length;
  renderLightbox();
}
const closeLightbox = () => { $("#lightbox").hidden = true; $("#lightbox").innerHTML = ""; };

// ------------------------------------------------------------------------------ events
function bindEvents() {
  $("#theme-toggle").addEventListener("click", cycleTheme);
  $("#help-button").addEventListener("click", openHelp);
  $("#new-issue").addEventListener("click", () => openIssueDialog());
  $("#quick-add").addEventListener("submit", quickAdd);
  $("#quick-files").addEventListener("click", () => { setQuickFiles([]); $("#quick-title").focus(); });
  const quick = $("#quick-add");
  quick.addEventListener("dragover", (e) => {
    if (!Array.from(e.dataTransfer?.types || []).includes("Files")) return;
    e.preventDefault(); quick.classList.add("drag-over");
  });
  quick.addEventListener("dragleave", () => quick.classList.remove("drag-over"));
  quick.addEventListener("drop", (e) => {
    e.preventDefault(); quick.classList.remove("drag-over");
    const files = filesFrom(e.dataTransfer);
    if (files.length) addQuickFiles(files);
  });
  $("#project-button").addEventListener("click", (e) => { e.stopPropagation(); toggleProjectMenu(); });
  $("#project-menu").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    toggleProjectMenu(false);
    if (b.dataset.new) openProjectDialog();
    else if (b.dataset.settings) openProjectSettings();
    else go(b.dataset.slug);
  });
  $("#project-menu").addEventListener("keydown", (e) => {
    const items = $$("#project-menu button");
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
    if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
    if (e.key === "Escape") { toggleProjectMenu(false); $("#project-button").focus(); }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".project-switch")) toggleProjectMenu(false);
    if (S.buildPop && !e.target.closest(".build-wrap")) toggleBuildPop(false);
  });

  let searchTimer;
  $("#search").addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => setFilter((f) => { f.q = e.target.value.trim(); }), 160);
  });
  $("#search").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.target.value = ""; setFilter((f) => { f.q = ""; }); e.target.blur(); }
    if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); e.target.blur(); setCursor(0); }
  });

  $("#filters").addEventListener("click", (e) => {
    const v = e.target.closest("[data-view]");
    if (v) { setView(v.dataset.view); return; }
    if (e.target.closest("[data-parked]")) { setView("list"); setFilter((f) => { f.status = ["parked"]; }); return; }
    if (e.target.closest("[data-build-pop]")) { toggleBuildPop(); return; }
    const ba = e.target.closest("[data-build-act]")?.dataset.buildAct;
    if (ba) {
      const b = S.project?.build;
      if (!b) return;
      if (ba === "copy") copyText(b.path, b.path === b.label ? "Build copied" : "Build path copied");
      else launchBuild(`/api/projects/${encodeURIComponent(S.slug)}/build`, ba);
      return;
    }
    const c = e.target.closest(".chip");
    if (!c) return;
    const { group, value } = c.dataset;
    setFilter((f) => {
      if (group === "merged") { f.merged = !f.merged; return; }
      if (group === "status") f.merged = false;
      if (value === "*") { f[group] = []; return; }
      const arr = f[group];
      if (e.shiftKey || e.ctrlKey || e.metaKey) { f[group] = arr.length === 1 && arr[0] === value ? [] : [value]; return; }
      const at = arr.indexOf(value);
      if (at >= 0) arr.splice(at, 1); else arr.push(value);
    });
  });
  $("#filters").addEventListener("change", (e) => {
    if (e.target.id === "area-filter") setFilter((f) => { f.area = e.target.value; });
    if (e.target.id === "seed-filter") setFilter((f) => { f.seed = e.target.value; });
    if (e.target.id === "sort-select") setFilter((f) => { f.sort = e.target.value; });
  });

  $("#list").addEventListener("click", (e) => {
    const head = e.target.closest(".group-head");
    if (head) {
      const area = head.dataset.area;
      if (S.collapsed.has(area)) S.collapsed.delete(area); else S.collapsed.add(area);
      store(`collapsed.${S.slug}`, [...S.collapsed]);
      renderList();
      return;
    }
    const row = e.target.closest(".issue-row");
    if (!row) return;
    const idx = +row.dataset.idx;
    if (e.target.closest(".row-check")) {
      if (e.target.tagName !== "INPUT") return; // the label forwards the click to its checkbox
      if (e.shiftKey) selectRange(idx); else toggleSelect(row.dataset.id, e.target.checked);
      S.anchor = idx;
      return;
    }
    if (e.shiftKey) { e.preventDefault(); selectRange(idx); S.anchor = idx; return; }
    go(S.slug, row.dataset.id);
  });
  $("#list").addEventListener("mousedown", (e) => { if (e.shiftKey) e.preventDefault(); }); // no text selection on shift-click
  $("#list").addEventListener("scroll", scheduleWindows, { passive: true });
  new ResizeObserver(scheduleWindows).observe($("#list"));
  $("#selection-bar").addEventListener("click", (e) => {
    const a = e.target.closest("[data-sel]")?.dataset.sel;
    if (a === "merge") openMergeDialog("merge");
    else if (a === "group") openMergeDialog("group");
    else if (a === "clear") { S.selected.clear(); renderList(); }
  });

  const detail = $("#detail");
  detail.addEventListener("click", (e) => {
    const lb = e.target.closest("[data-lightbox]");
    const del = e.target.closest("[data-delete-att]");
    if (del) {
      e.stopPropagation();
      if (confirm("Remove this attachment?")) {
        api("DELETE", `/api/attachments/${del.dataset.deleteAtt}`).then(() => { toast("Attachment removed"); refreshIssue(); loadList(); }).catch(fail);
      }
      return;
    }
    if (lb) { e.preventDefault(); openLightbox(lb.dataset.lightbox); return; }
    const unp = e.target.closest("[data-unpend]");
    if (unp) {
      const d = draft(S.issue.id);
      const [x] = d.files.splice(+unp.dataset.unpend, 1);
      if (x?.url) URL.revokeObjectURL(x.url);
      renderPending();
      return;
    }
    const v = e.target.closest("[data-verdict]");
    if (v) { postComment(v.dataset.verdict || null); return; }
    const tick = e.target.closest("[data-tick]");
    if (tick) {
      const n = +tick.dataset.tick;
      const st = S.issue.plan.steps[n - 1];
      setStep(n, { state: st.state === "done" ? "todo" : "done" });
      return;
    }
    const note = e.target.closest("[data-note]");
    if (note) {
      S.stepNote = +note.dataset.note;
      renderDetail(false);
      const inp = $("#step-note-input");
      if (inp) { inp.focus(); inp.select(); }
      return;
    }
    const um = e.target.closest("[data-unmerge]");
    if (um) {
      if (confirm(`Unmerge ${um.dataset.unmerge}? It gets back its own comments, attachments and status.`)) unmergeIssue(um.dataset.unmerge);
      return;
    }
    const ib = e.target.closest("[data-issue-build]")?.dataset.issueBuild;
    if (ib && S.issue?.build) {
      const b = S.issue.build;
      if (ib === "copy") copyText(b.path, b.path === b.label ? "Build copied" : "Build path copied");
      else launchBuild(`/api/issues/${encodeURIComponent(S.issue.id)}/build`, ib);
      return;
    }
    if (e.target.closest("#detail-title")) { S.editing = "title"; renderDetail(false); const t = $("#title-input"); t.focus(); t.select(); return; }
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (!act) return;
    const i = S.issue;
    switch (act) {
      case "close": closeDetail(); break;
      case "close-parent": patchIssue({ status: "closed" }, `${i.id} closed`); break;
      case "edit-handoff": S.editing = "handoff"; renderHandoff(); $("#handoff-input")?.focus(); break;
      case "cancel-handoff": S.editing = null; renderHandoff(); break;
      case "save-handoff": saveHandoff(); break;
      case "handoff-diff":
        if (S.handoff.diff) { S.handoff.diff = null; renderHandoff(); break; }
        api("GET", `/api/projects/${encodeURIComponent(S.slug)}/handoff/diff?from=${S.handoff.version - 1}&to=${S.handoff.version}`)
          .then((d) => { S.handoff.diff = d.diff || "(no differences)"; renderHandoff(); }).catch(fail);
        break;
      case "copy-id": copyText(i.id, `Copied ${i.id}`); break;
      case "copy-link": copyText(`${location.origin}/#/${S.slug}/${i.id}`, "Link copied"); break;
      case "copy-cmd": copyText(gameCommand(i.location, +(e.target.closest("[data-n]")?.dataset.n || 0)), "Command copied"); break;
      case "send-cmd": sendToGame(+(e.target.closest("[data-n]")?.dataset.n || 0)); break;
      case "cmd-add": readCommandEditor(); S.locDraft.push({ command: "", label: "" }); renderCommandEditor(S.locDraft.length - 1); break;
      case "cmd-remove": case "cmd-up": case "cmd-down": {
        readCommandEditor();
        const n = +e.target.closest("[data-n]").dataset.n;
        const d = S.locDraft;
        if (act === "cmd-remove") { d.splice(n, 1); if (!d.length) d.push({ command: "", label: "" }); renderCommandEditor(Math.min(n, d.length - 1)); }
        else {
          const to = act === "cmd-up" ? n - 1 : n + 1;
          if (to < 0 || to >= d.length) break;
          [d[n], d[to]] = [d[to], d[n]];
          renderCommandEditor(to);
        }
        break;
      }
      case "edit-body": startEdit("body"); break;
      case "edit-location": startEdit("location"); break;
      case "cancel-edit": S.editing = null; renderDetail(false); break;
      case "save-body": patchIssue({ body: $("#body-input").value }, "Description saved"); break;
      case "save-location": saveLocation(); break;
      case "delete":
        if (confirm(`Delete ${i.id} "${i.title}" with its comments and attachments? This cannot be undone.`)) {
          api("DELETE", `/api/issues/${encodeURIComponent(i.id)}`).then(async () => {
            toast(`${i.id} deleted`);
            closeDetail();
            await Promise.all([loadList(), refreshProject()]);
          }).catch(fail);
        }
        break;
      default: break;
    }
  });
  detail.addEventListener("keydown", (e) => {
    if (e.target.classList.contains("thumb") && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openLightbox(e.target.dataset.lightbox); }
    if (e.target.id === "title-input") {
      if (e.key === "Enter") { e.preventDefault(); commitTitle(e.target.value); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); S.editing = null; renderDetail(false); }
    }
    if (e.target.id === "body-input" || e.target.closest?.(".location-card")) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (S.editing === "body") patchIssue({ body: $("#body-input").value }, "Description saved"); else saveLocation(); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); S.editing = null; renderDetail(false); }
    }
    if (e.target.id === "comment-input") {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); postComment(null); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); e.target.blur(); }
    }
    if (e.target.id === "step-note-input") {
      if (e.key === "Enter") { e.preventDefault(); setStep(S.stepNote, { note: e.target.value.trim() }); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); S.stepNote = null; renderDetail(false); }
    }
    if (e.target.id === "parent-input" && e.key === "Enter") { e.preventDefault(); e.target.blur(); }
    if (e.target.id === "handoff-input") {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveHandoff(); }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); S.editing = null; renderHandoff(); }
    }
  });
  detail.addEventListener("focusout", (e) => {
    if (e.target.id === "title-input") commitTitle(e.target.value);
    if (e.target.id === "parent-input" && S.issue) setParent(e.target.value);
  });
  detail.addEventListener("input", (e) => {
    if (e.target.id === "comment-input" && S.issue) draft(S.issue.id).text = e.target.value;
    if (e.target.closest(".cmd-row") && S.locDraft) readCommandEditor();
  });
  detail.addEventListener("change", (e) => {
    if (e.target.id === "status-select") { patchIssue({ status: e.target.value }, `Status: ${STATUS_LABEL[e.target.value]}`); return; }
    if (e.target.id === "handoff-version") { openHandoff(+e.target.value === S.handoff.history[0]?.version ? null : e.target.value); return; }
    if (e.target.id === "parent-input") return; // saved on blur / Enter
    if (e.target.id === "issue-file") { uploadToIssue(Array.from(e.target.files)); e.target.value = ""; return; }
    const field = e.target.dataset.field;
    if (!field || !S.issue) return;
    const val = e.target.value;
    const cur = field === "tags" ? S.issue.tags.join(", ") : S.issue[field];
    if (val !== cur) patchIssue({ [field]: val });
  });
  // drag and drop: the attachments zone uploads straight to the issue; anywhere else in the
  // detail pane queues files for the next comment.
  detail.addEventListener("dragover", (e) => {
    if (!S.issue || !Array.from(e.dataTransfer?.types || []).includes("Files")) return;
    e.preventDefault();
    $$(".drag-over", detail).forEach((x) => x.classList.remove("drag-over"));
    (e.target.closest("#issue-dropzone") || $("#composer"))?.classList.add("drag-over");
  });
  detail.addEventListener("dragleave", (e) => { if (!detail.contains(e.relatedTarget)) $$(".drag-over", detail).forEach((x) => x.classList.remove("drag-over")); });
  detail.addEventListener("drop", (e) => {
    if (!S.issue) return;
    e.preventDefault();
    $$(".drag-over", detail).forEach((x) => x.classList.remove("drag-over"));
    const files = filesFrom(e.dataTransfer);
    if (!files.length) return;
    if (e.target.closest("#issue-dropzone")) uploadToIssue(files); else addPending(files);
  });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => e.preventDefault());

  document.addEventListener("paste", (e) => {
    const files = filesFrom(e.clipboardData);
    if (!files.length) return;
    if ($("#issue-dialog").open) { e.preventDefault(); S.addDialogFiles?.(files); return; }
    if (document.activeElement?.closest?.("#quick-add")) { e.preventDefault(); addQuickFiles(files); return; }
    if (S.issue) { e.preventDefault(); addPending(files); }
  });

  $("#lightbox").addEventListener("click", (e) => {
    const a = e.target.closest("[data-lb]")?.dataset.lb;
    if (e.target.tagName === "IMG") return;
    if (a === "prev") lightboxStep(-1);
    else if (a === "next") lightboxStep(1);
    else if (a === "close") closeLightbox();
  });

  document.addEventListener("keydown", onKey);
  window.addEventListener("hashchange", route);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { S.live?.subscribe(); poll(true); } });
  window.addEventListener("pagehide", () => { S.live?.close(); S.live = null; });
  window.addEventListener("pageshow", e => { if (e.persisted && S.slug) { connectLive(S.slug); poll(true); } });
  bindSplitter();
}

function setView(view) {
  if (S.view === view) return;
  S.view = view;
  store(`view.${S.slug}`, view);
  S.cursor = -1;
  loadList();
}

function commitTitle(value) {
  if (S.editing !== "title") return; // already committed (Enter, then the blur that follows)
  S.editing = null;
  const v = value.trim();
  if (v && v !== S.issue.title) patchIssue({ title: v }, "Title saved");
  else renderDetail(false);
}

function startEdit(what) {
  S.editing = what;
  if (what === "location") {
    const loc = S.issue.location || {};
    const list = Array.isArray(loc.commands) ? loc.commands : loc.command ? [{ command: loc.command }] : [];
    S.locDraft = list.length ? list.map((c) => ({ command: c.command || "", label: c.label || "" })) : [{ command: "", label: "" }];
  }
  renderDetail(false);
  const el = what === "body" ? $("#body-input") : $(".cmd-row textarea");
  if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
}

/** The location editor's command rows (S.locDraft): a label and a command each, with move and remove buttons. */
function commandEditorHtml() {
  const d = S.locDraft || [{ command: "", label: "" }];
  return `${d.map((c, n) => `<div class="cmd-row" data-n="${n}">
      <span class="cmd-num">${n + 1}</span>
      <div class="cmd-fields">
        <input class="cmd-label-input" maxlength="80" value="${esc(c.label)}" placeholder="Label, e.g. the capital" aria-label="Label of command ${n + 1}">
        <textarea class="textarea mono" rows="2" placeholder="/goto 1240 -380 yaw 90; /time 17:30" aria-label="Command ${n + 1}">${esc(c.command)}</textarea>
      </div>
      <div class="cmd-row-tools">
        <button type="button" class="icon-btn icon-btn-sm" data-act="cmd-up" title="Move up"${n === 0 ? " disabled" : ""} aria-label="Move command ${n + 1} up">${ICON.up}</button>
        <button type="button" class="icon-btn icon-btn-sm" data-act="cmd-down" title="Move down"${n === d.length - 1 ? " disabled" : ""} aria-label="Move command ${n + 1} down">${ICON.down}</button>
        <button type="button" class="icon-btn icon-btn-sm" data-act="cmd-remove" title="Remove" aria-label="Remove command ${n + 1}">${ICON.trash}</button>
      </div>
    </div>`).join("")}
    <div class="cmd-editor-foot"><button type="button" class="btn btn-sm btn-quiet" data-act="cmd-add">${ICON.plus}Add a place</button>
      <span class="hint">One <code>/goto</code> (and at most one creature) per command; the owner runs each on its own.</span></div>`;
}

/** Reads the typed labels and commands back into S.locDraft. */
function readCommandEditor() {
  $$("#loc-commands .cmd-row").forEach((row) => {
    const c = S.locDraft[+row.dataset.n];
    if (!c) return;
    c.label = $(".cmd-label-input", row).value;
    c.command = $("textarea", row).value;
  });
}

function renderCommandEditor(focus) {
  const box = $("#loc-commands");
  if (!box) return;
  box.innerHTML = commandEditorHtml();
  const el = focus === undefined ? null : $(`.cmd-row[data-n="${focus}"] textarea`, box);
  if (el) el.focus();
}

function saveLocation() {
  const loc = { ...(S.issue.location || {}) };
  readCommandEditor();
  delete loc.command;
  loc.commands = S.locDraft
    .map((c) => ({ command: c.command.trim(), label: c.label.trim() }))
    .filter((c) => c.command)
    .map((c) => (c.label ? c : { command: c.command }));
  for (const k of ["action", "place", "time", "weather"]) {
    const v = $(`#loc-${k}`).value.trim();
    if (v) loc[k] = v; else delete loc[k];
  }
  for (const k of ["x", "y", "z", "yaw", "pitch"]) {
    const raw = $(`#loc-${k}`).value.trim();
    if (raw === "") { delete loc[k]; continue; }
    const n = Number(raw);
    if (!Number.isFinite(n)) { toast(`${k} must be a number`, "error"); return; }
    loc[k] = n;
  }
  const seed = $("#loc-seed").value.trim();
  if (seed === "") delete loc.seed;
  else if (!/^[+-]?\d{1,11}$/.test(seed)) { toast("The world seed must be a whole number", "error"); return; }
  else loc.seed = Number(seed);
  patchIssue({ location: loc }, "Location saved");
}

function onKey(e) {
  if (e.key === "Escape" && S.buildPop) { toggleBuildPop(false); e.preventDefault(); return; }
  if (!$("#lightbox").hidden) {
    if (e.key === "Escape") closeLightbox();
    else if (e.key === "ArrowLeft") lightboxStep(-1);
    else if (e.key === "ArrowRight") lightboxStep(1);
    e.preventDefault();
    return;
  }
  if ($$("dialog").some((d) => d.open)) return;
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  // Let the native build disclosure handle keyboard activation, without list shortcuts.
  if (e.target.closest?.(".tl-build-run > summary")) return;
  if (isTyping(e.target)) {
    if (e.key === "Escape") e.target.blur();
    return;
  }
  if (!S.project) return;
  const k = e.key;
  const issues = S.list.issues;
  const handled = () => e.preventDefault();
  switch (k) {
    case "j": case "ArrowDown":
      handled();
      setCursor(S.cursor < 0 ? 0 : S.cursor + 1, { open: !!S.openId && !narrow() });
      break;
    case "k": case "ArrowUp":
      handled();
      setCursor(S.cursor < 0 ? 0 : S.cursor - 1, { open: !!S.openId && !narrow() });
      break;
    case "Enter": case "o":
      if (S.cursor >= 0 && issues[S.cursor]) { handled(); go(S.slug, issues[S.cursor].id); }
      break;
    case "Escape":
      if (S.editing === "handoff") { S.editing = null; renderHandoff(); }
      else if (S.editing) { S.editing = null; renderDetail(false); }
      else if (S.openId || S.handoff) closeDetail();
      else if (S.selected.size) { S.selected.clear(); renderList(); }
      break;
    case "x":
      if (S.cursor >= 0 && issues[S.cursor]) { handled(); toggleSelect(issues[S.cursor].id); S.anchor = S.cursor; }
      break;
    case "m": if (S.selected.size) { handled(); openMergeDialog("merge"); } break;
    case "b": handled(); setView(S.view === "backlog" ? "list" : "backlog"); break;
    case "h": handled(); go(S.slug, HANDOFF); break;
    case "/": handled(); $("#search").focus(); $("#search").select(); break;
    case "n": handled(); openIssueDialog(); break;
    case "q": handled(); $("#quick-title").focus(); break;
    case "c": if (S.issue) { handled(); $("#comment-input")?.focus(); } break;
    case "p": if (S.issue) { handled(); postComment("passed"); } break;
    case "f": if (S.issue) { handled(); postComment("failed"); } break;
    case "s": if (S.issue) { handled(); sendToGame(); } break;
    case "y": if (gameCommand(S.issue?.location)) { handled(); copyText(gameCommand(S.issue.location), "Command copied"); } break;
    case "e": if (S.issue) { handled(); startEdit("body"); } break;
    case "r": handled(); refreshAll(); break;
    case "t": handled(); cycleTheme(); break;
    case "?": handled(); openHelp(); break;
    case "0": handled(); if (S.view !== "list") setView("list"); setFilter((f) => { f.status = []; }); break;
    default:
      if (/^[1-9]$/.test(k)) { handled(); if (S.view !== "list") setView("list"); setFilter((f) => { f.status = [STATUSES[+k - 1]]; }); }
  }
}

// ------------------------------------------------------------------------------ live refresh
let polling = false, lastPoll = 0;
async function poll(force = false) {
  if (polling || document.hidden || !S.slug) return;
  if (S.liveOk && !force && Date.now() - lastPoll < 60000) return; // the stream brings changes
  lastPoll = Date.now();
  polling = true;
  const slug = S.slug;
  try {
    const p = await api("GET", `/api/projects/${encodeURIComponent(slug)}`);
    if (slug !== S.slug) return;
    if (p.last_change !== S.lastChange) {
      const first = S.lastChange === undefined;
      S.project = p;
      S.lastChange = p.last_change;
      if (!first) {
        await loadList();
        const active = document.activeElement;
        const busy = S.editing || S.stepNote || (active && $("#detail-inner")?.contains(active) && isTyping(active));
        if (S.openId && !busy) await refreshIssue();
        if (S.handoff && !S.editing) await openHandoff(S.handoff.viewing);
        if (!S.openId) renderEmptyDetail();
        loadProjects().catch(() => {});
      }
    }
  } catch (e) { /* offline for a moment; the next tick retries */ }
  finally { polling = false; }
}

// ------------------------------------------------------------------------------ boot
/** The running desk's version in the top bar (from /api/health): the owner can see which build of the desk he uses. */
async function showVersion() {
  try {
    const health = await api("GET", "/api/health");
    const el = document.getElementById("brand-version");
    if (el && health && health.version) {
      el.textContent = "v" + health.version;
      el.title = "Pair Desk " + health.version;
    }
  } catch (e) { /* the version is a convenience: never block the desk */ }
}

async function boot() {
  let theme = "auto";
  try { theme = localStorage.getItem("pairdesk.theme") || "auto"; } catch (e) { /* ignore */ }
  applyTheme(theme);
  bindEvents();
  showVersion();
  try {
    await loadProjects();
  } catch (e) {
    fail(e);
    return;
  }
  S.lastChange = undefined;
  await route();
  setInterval(poll, 4000);
  poll();
}

boot();
