// Optional browser check of the web UI (development only; the desk itself needs no Node).
// Drives a headless Edge/Chrome over the DevTools protocol against a running desk, performs the
// owner's daily actions with real keyboard/DOM events, and fails on any page exception.
//
//   python desk.py --data <throwaway> serve --detach --port 8799
//   node scripts/ui-check.mjs http://127.0.0.1:8799 [screenshot-dir] [showcase-project]
//
// With a showcase project (fill the throwaway desk with `python scripts/demo-desk.py --data
// <throwaway>` first and pass `mygame`), it also takes the README screenshots of that project.
//
// Needs Node 22+ (built-in WebSocket) and Edge or Chrome. It creates a project "uicheck" and issues in
// that desk, so point it at a throwaway data folder.

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:8799";
const SHOTS = process.argv[3] || null;
const SHOWCASE = process.argv[4] || null;
if (SHOTS) mkdirSync(SHOTS, { recursive: true });
const BROWSERS = [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/microsoft-edge",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
];
const browser = BROWSERS.find((b) => existsSync(b));
if (!browser) { console.error("no Edge/Chrome found"); process.exit(2); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (method, path, body) => {
  const r = await fetch(BASE + path, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  return r.status === 204 ? null : r.json();
};

const profile = mkdtempSync(join(tmpdir(), "pairdesk-ui-"));
const port = 9300 + Math.floor(Math.random() * 500);
const proc = spawn(browser, ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--disable-extensions", "--disable-component-extensions-with-background-pages",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "--window-size=1500,950",
  // CI containers and Ubuntu 24.04 runners forbid Chrome's sandbox; a throwaway local page needs none
  ...(process.env.CI ? ["--no-sandbox"] : []), "about:blank"], { stdio: "ignore" });

let ws, seq = 0;
const pending = new Map();
const errors = [];
const results = [];
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq;
  pending.set(id, { resolve, reject });
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
};
const check = (name, ok, detail) => { results.push(ok); console.log(`  [${ok ? "ok" : "FAIL"}] ${name}${ok ? "" : "  " + JSON.stringify(detail)}`); };
const key = (k, opts = {}) => evaluate(`document.activeElement.dispatchEvent(new KeyboardEvent("keydown", { key: ${JSON.stringify(k)}, bubbles: true, ctrlKey: ${!!opts.ctrl} })); await new Promise(r => setTimeout(r, 350));`);
const waitFor = async (expr, ms = 4000) => {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await evaluate(`return !!(${expr})`)) return true; await sleep(100); }
  return false;
};
const shot = async (name) => {
  if (!SHOTS) return;
  const r = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(SHOTS, `${name}.png`), Buffer.from(r.data, "base64"));
};

try {
  let target;
  for (let i = 0; i < 50 && !target; i++) {
    try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page"); } catch { await sleep(150); }
  }
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
    if (m.method === "Runtime.exceptionThrown") errors.push((m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text) + ` @${m.params.exceptionDetails.url || m.params.exceptionDetails.scriptId}`);
    if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errors.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
  });
  await send("Runtime.enable");
  await send("Page.enable");

  // fixture
  await api("POST", "/api/projects", { slug: "uicheck", name: "UI Check", prefix: "UC" });
  // Created last, so the triage order (newest first within a status) lists it first.
  await api("POST", "/api/projects/uicheck/issues", { title: "Second check", kind: "check", status: "to_check", source: "agent" });
  const a = await api("POST", "/api/projects/uicheck/issues", { title: "Check the rounded lava tips", kind: "check", status: "to_check", source: "agent", command: "/goto 1 2 3; /time 17:30", body: "Look at the **east** flow. See UC-1." });
  await api("POST", "/api/projects/uicheck/issues", { title: "Boat wobbles", status: "reported", source: "game" });

  await send("Page.navigate", { url: `${BASE}/#/uicheck` });
  check("list renders", await waitFor(`document.querySelectorAll(".issue-row").length === 3`), await evaluate(`return document.querySelectorAll(".issue-row").length`));
  check("default filter shows unfinished work", await evaluate(`return [...document.querySelectorAll('.chip.on')].map(c => c.dataset.value).join()`) === "to_check,auto_check,reported,failed,open,in_progress");

  await evaluate(`document.activeElement.blur()`);
  await key("j");
  await key("Enter");
  check("j + Enter opens the first issue", await waitFor(`location.hash === "#/uicheck/${a.id}" && document.querySelector(".detail-title")`), await evaluate(`return location.hash`));
  check("markdown and issue refs render", await evaluate(`return !!document.querySelector("#body-view strong") && !!document.querySelector("#body-view a.issue-ref")`));
  check("location command shown", await evaluate(`return document.querySelector("#loc-cmd")?.textContent`) === "/goto 1 2 3; /time 17:30");
  await shot("01-detail");

  // send to game, then play the game's part
  await evaluate(`document.querySelector('[data-act="send-cmd"]').click()`);
  await sleep(400);
  const next = await api("GET", "/api/projects/uicheck/commands/next?client=ui-check-game");
  check("game receives the command", next && next.command === "/goto 1 2 3; /time 17:30", next);
  check("UI shows the pickup", await waitFor(`document.querySelector('.send-slot[data-n="0"]')?.textContent.includes("Picked up by ui-check-game")`, 5000), await evaluate(`return document.querySelector('.send-slot[data-n="0"]')?.textContent`));

  // comment with c + Ctrl+Enter
  await key("c");
  await evaluate(`const t = document.querySelector("#comment-input"); t.value = "Checked in the evening light"; t.dispatchEvent(new Event("input", { bubbles: true }));`);
  await key("Enter", { ctrl: true });
  check("comment posted", await waitFor(`[...document.querySelectorAll(".bubble-body")].some(b => b.textContent.includes("evening light"))`));

  // p = passed, auto-advance to the next item in the queue
  await evaluate(`document.activeElement.blur()`);
  await key("p");
  const passed = await api("GET", `/api/issues/${a.id}`);
  check("p marks passed", passed.status === "passed", passed.status);
  check("advances to the next item", await waitFor(`location.hash !== "#/uicheck/${a.id}" && location.hash.startsWith("#/uicheck/UC-")`), await evaluate(`return location.hash`));

  // f = still broken on the current one
  const cur = await evaluate(`return location.hash.split("/").pop()`);
  await key("f");
  check("f marks failed", (await api("GET", `/api/issues/${cur}`)).status === "failed");

  // inline title edit
  await evaluate(`document.querySelector("#detail-title").click()`);
  await evaluate(`const t = document.querySelector("#title-input"); t.value = "Second check, renamed";`);
  await evaluate(`document.querySelector("#title-input").dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }))`);
  await sleep(500);
  check("title edit saves once", (await api("GET", `/api/issues/${cur}`)).activity.filter((x) => x.action === "edited").length === 1);

  // new issue dialog with a pasted screenshot
  await evaluate(`document.activeElement.blur()`);
  await key("n");
  check("n opens the new issue dialog", await waitFor(`document.querySelector("#issue-dialog").open`));
  await evaluate(`
    const f = document.querySelector("#issue-form");
    f.title.value = "Fox clips through the fence";
    f.area.value = "creatures";
    f.command.value = "/goto 9 9 9";
    const c = document.createElement("canvas"); c.width = 64; c.height = 40;
    const g = c.getContext("2d"); g.fillStyle = "#2f5d62"; g.fillRect(0, 0, 64, 40);
    const blob = await new Promise(r => c.toBlob(r, "image/png"));
    const dt = new DataTransfer(); dt.items.add(new File([blob], "image.png", { type: "image/png" }));
    document.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true }));
    await new Promise(r => setTimeout(r, 200));`);
  check("pasted screenshot is pending in the dialog", await evaluate(`return document.querySelectorAll("#dlg-pending .pending-item").length`) === 1);
  await shot("02-new-issue");
  await evaluate(`const f = document.querySelector("#issue-form"); f.requestSubmit(f.querySelector('button[value="ok"]'));`);
  check("issue created and opened", await waitFor(`document.querySelector(".detail-title")?.textContent === "Fox clips through the fence"`, 5000));
  const fox = await api("GET", `/api/issues/${await evaluate(`return location.hash.split("/").pop()`)}`);
  check("with its screenshot, renamed from image.png", fox.attachments.length === 1 && fox.attachments[0].filename.startsWith("screenshot-"), fox.attachments);

  // lightbox
  await evaluate(`document.querySelector(".thumb").click()`);
  check("lightbox opens", await waitFor(`!document.querySelector("#lightbox").hidden && document.querySelector("#lightbox img")`));
  await shot("03-lightbox");
  // Large screenshots must fit beneath the toolbar, including after a viewport resize.
  for (const [width, height] of [[1500, 950], [800, 600], [390, 844], [844, 390]]) {
    await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
    for (const [imageWidth, imageHeight] of [[900, 5000], [5000, 900], [4000, 3000], [64, 40]]) {
      const bounds = await evaluate(`
        const canvas = document.createElement("canvas"); canvas.width = ${imageWidth}; canvas.height = ${imageHeight};
        const g = canvas.getContext("2d"); g.fillStyle = "#2f5d62"; g.fillRect(0, 0, canvas.width, canvas.height);
        const image = document.querySelector("#lightbox img"); image.src = canvas.toDataURL(); await image.decode();
        await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
        const rect = image.getBoundingClientRect();
        const bar = document.querySelector(".lightbox-bar").getBoundingClientRect();
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
          width: rect.width, height: rect.height, barBottom: bar.bottom, viewportWidth: innerWidth, viewportHeight: innerHeight };
      `);
      check(`lightbox fits ${imageWidth}x${imageHeight} at ${width}x${height}`,
        bounds.left >= 11 && bounds.right <= bounds.viewportWidth - 11 &&
        bounds.top >= bounds.barBottom - 1 && bounds.bottom <= bounds.viewportHeight - 15 &&
        bounds.width > 0 && bounds.height > 0 &&
        Math.abs(bounds.width / bounds.height - imageWidth / imageHeight) < 0.01, bounds);
      if (imageHeight === 5000) await shot(`lightbox-tall-${width}x${height}`);
    }
  }
  await send("Emulation.clearDeviceMetricsOverride");
  await evaluate(`document.querySelector('[data-lb="next"]').click()`);
  check("Next restores the attachment image", await waitFor(`document.querySelector('#lightbox img')?.getAttribute('src') === ${JSON.stringify(fox.attachments[0].url)}`));
  await evaluate(`document.querySelector('[data-lb="prev"]').click()`);
  check("Previous keeps the attachment image", await evaluate(`return document.querySelector('#lightbox img')?.getAttribute('src') === ${JSON.stringify(fox.attachments[0].url)}`));
  await evaluate(`document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))`);
  check("Esc closes the lightbox", await evaluate(`return document.querySelector("#lightbox").hidden`));

  // quick add with tokens
  await evaluate(`const q = document.querySelector("#quick-title"); q.value = "Grass flickers !p1 @terrain +bug #4k"; document.querySelector("#quick-add").requestSubmit();`);
  await sleep(700);
  const listing = await api("GET", "/api/projects/uicheck/issues?q=Grass");
  const g = listing.issues[0] || {};
  check("quick add parses tokens", g.priority === "p1" && g.area === "terrain" && g.kind === "bug" && g.tags?.[0] === "4k", g);

  // status filter keys and search
  await evaluate(`document.activeElement.blur()`);
  await key("0");
  check("0 shows all statuses", await waitFor(`document.querySelectorAll(".issue-row").length === 5`), await evaluate(`return document.querySelectorAll(".issue-row").length`));
  await key("/");
  await evaluate(`const s = document.querySelector("#search"); s.value = "fox"; s.dispatchEvent(new Event("input", { bubbles: true }));`);
  check("search filters", await waitFor(`document.querySelectorAll(".issue-row").length === 1`));

  // help dialog, theme toggle
  await evaluate(`document.activeElement.blur(); document.querySelector("#search").value=""; document.querySelector("#search").dispatchEvent(new Event("input", { bubbles: true }));`);
  await key("?");
  check("? opens help", await evaluate(`return document.querySelector("#help-dialog").open`));
  await evaluate(`document.querySelector("#help-dialog").close()`);
  await evaluate(`document.querySelector("#theme-toggle").click(); document.querySelector("#theme-toggle").click();`);
  check("theme toggle sets dark", await evaluate(`return document.documentElement.dataset.theme`) === "dark");
  await shot("04-dark");

  // world seed: project settings, the seeded command, the location card, the seed filter, a webp screenshot
  await evaluate(`document.querySelector("#project-button").click()`);
  await evaluate(`document.querySelector('#project-menu [data-settings]').click()`);
  check("project settings open", await waitFor(`document.querySelector("#settings-form")`));
  await evaluate(`const f = document.querySelector("#settings-form"); f.seed.value = "1234"; f.requestSubmit(f.querySelector('button[value="ok"]'));`);
  await sleep(500);
  check("default seed saved", (await api("GET", "/api/projects/uicheck")).default_seed === 1234);
  const seeded = await api("POST", "/api/projects/uicheck/issues", { title: "Seeded check", kind: "check", status: "to_check", source: "agent", command: "/goto 5 6; /time 06:00" });
  check("agent check inherits the default seed", seeded.location.seed === 1234, seeded.location);
  await evaluate(`
    const c = document.createElement("canvas"); c.width = 48; c.height = 32;
    const g = c.getContext("2d"); g.fillStyle = "#e07a3a"; g.fillRect(0, 0, 48, 32);
    const data_base64 = c.toDataURL("image/webp").split(",")[1];
    await fetch("/api/issues/${seeded.id}/attachments", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: "shot.webp", mime: "image/webp", data_base64 }) });`);
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${seeded.id}` });
  // The list was loaded before the new issue: an (empty) search reloads it, as typing would.
  await evaluate(`const s = document.querySelector("#search"); s.value = ""; s.dispatchEvent(new Event("input", { bubbles: true }));`);
  check("seeded command shown", await waitFor(`document.querySelector("#loc-cmd")?.textContent === "/goto 5 6 seed 1234; /time 06:00"`, 5000), await evaluate(`return document.querySelector("#loc-cmd")?.textContent`));
  check("location card names the world seed", await evaluate(`return [...document.querySelectorAll(".loc-facts .fact")].some(f => f.textContent === "World seed1234")`));
  check("webp thumbnail renders", await waitFor(`[...document.querySelectorAll(".thumb img")].some(i => i.complete && i.naturalWidth === 48)`, 5000));
  check("list shows the attachment count", await waitFor(`document.querySelector('.issue-row[data-id="${seeded.id}"] [title="Attachments"]')?.textContent === "1"`, 5000));
  check("seed filter offered", await waitFor(`[...document.querySelectorAll("#seed-filter option")].some(o => o.value === "1234")`, 5000));
  await evaluate(`document.querySelector('[data-act="send-cmd"]').click()`);
  await sleep(400);
  const seededNext = await api("GET", "/api/projects/uicheck/commands/next?client=ui-check-game");
  check("game receives the seeded command", seededNext && seededNext.command === "/goto 5 6 seed 1234; /time 06:00", seededNext);
  await shot("05-seed");

  // ---------------------------------------------------------------- several places: one command each
  const multi = await api("POST", "/api/projects/uicheck/issues", { title: "Three places", kind: "check", status: "to_check", source: "agent",
    commands: [{ command: "/goto capital", label: "the capital" }, { command: "/goto dungeon keep", label: "the keep" }, { command: "/spawn kind canine 20 2" }] });
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${multi.id}` });
  check("every command is listed with its label", await waitFor(`document.querySelectorAll(".loc-cmd").length === 3`, 5000)
    && await evaluate(`return [...document.querySelectorAll(".loc-cmd .cmd-label")].map(l => l.textContent).join("|")`) === "the capital|the keep",
    await evaluate(`return document.querySelector(".location-card")?.innerText`));
  check("each command carries the seed", await evaluate(`return [...document.querySelectorAll(".loc-cmd code")].map(c => c.textContent).join("|")`)
    === "/goto capital seed 1234|/goto dungeon keep seed 1234|/spawn kind canine 20 2");
  check("each command has its own Copy and Send", await evaluate(`return document.querySelectorAll('.loc-cmd [data-act="copy-cmd"]').length === 3 && document.querySelectorAll('.loc-cmd [data-act="send-cmd"]').length === 3`));
  await evaluate(`document.querySelector('.loc-cmd[data-n="1"] [data-act="send-cmd"]').click()`);
  await sleep(400);
  const second = await api("GET", "/api/projects/uicheck/commands/next?client=ui-check-game");
  check("Send on the second sends the second", second && second.command === "/goto dungeon keep seed 1234", second);
  check("only the second shows the pickup", await waitFor(`document.querySelector('.send-slot[data-n="1"]')?.textContent.includes("Picked up")`, 5000)
    && await evaluate(`return document.querySelector('.send-slot[data-n="0"]').textContent === ""`));
  await shot("05b-commands");
  await evaluate(`document.querySelector('[data-act="edit-location"]').click()`);
  check("the editor lists the commands", await waitFor(`document.querySelectorAll("#loc-commands .cmd-row").length === 3`, 3000));
  await evaluate(`document.querySelector("#loc-commands").scrollIntoView({ block: "center" })`);
  await shot("05c-commands-edit");
  await evaluate(`
    document.querySelector('.cmd-row[data-n="2"] [data-act="cmd-remove"]').click();
    document.querySelector('[data-act="cmd-add"]').click();
    const row = document.querySelector('.cmd-row[data-n="2"]');
    row.querySelector("textarea").value = "/goto back"; row.querySelector(".cmd-label-input").value = "back at the camp";
    row.querySelector("textarea").dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector('.cmd-row[data-n="1"] [data-act="cmd-up"]').click();
    document.querySelector('[data-act="save-location"]').click();`);
  await sleep(500);
  const edited = await api("GET", `/api/issues/${multi.id}`);
  check("add, remove and reorder save the list", JSON.stringify(edited.location.commands) === JSON.stringify([
    { command: "/goto dungeon keep", label: "the keep" }, { command: "/goto capital", label: "the capital" },
    { command: "/goto back", label: "back at the camp" }]) && edited.location.command === "/goto dungeon keep", edited.location);

  // ---------------------------------------------------------------- the current build
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${multi.id}` });
  check("no build shown before one is published", await waitFor(`document.querySelector(".loc-cmd")`, 5000)
    && await evaluate(`return !document.querySelector(".build-current, .build-card, .build-chip")`));
  await sleep(300);
  const published = await api("POST", "/api/projects/uicheck/build", { path: "D:/builds/uicheck/UICheck.exe", commit: "a2d78b49c0", label: "nightly 12", author: "claude" });
  check("publishing stamps the waiting checks", published.stamped.includes(multi.id) && published.stamped.includes(seeded.id), published);
  check("the filter bar shows the new build live", await waitFor(`document.querySelector(".build-current .build-name")?.textContent === "nightly 12"`, 6000),
    await evaluate(`return document.querySelector(".view-row")?.innerText`));
  check("list rows stay free of the build name (the filter bar names it)", await evaluate(`return !document.querySelector(".issue-row .build-chip")`));
  check("the issue shows its build and path", await waitFor(`document.querySelector(".build-card .build-path-row code")?.textContent === "D:/builds/uicheck/UICheck.exe"`, 5000)
    && await evaluate(`return document.querySelector(".build-card .build-commit")?.textContent === "a2d78b49c0"`));
  check("the timeline has one build entry", await waitFor(`[...document.querySelectorAll(".tl-event")].filter(e => e.textContent.includes("published build")).length === 1`, 3000));
  await evaluate(`document.querySelector('[data-issue-build="copy"]').click()`);
  check("copying the path confirms", await waitFor(`[...document.querySelectorAll(".toast")].some(t => t.textContent.includes("Build path copied"))`, 3000));
  await evaluate(`document.querySelector(".build-card").scrollIntoView({ block: "center" })`);
  await shot("05d-build");
  await api("DELETE", "/api/projects/uicheck/build");
  check("a cleared build warns in the filter bar", await waitFor(`document.querySelector(".build-current.none")`, 6000));
  await api("POST", "/api/projects/uicheck/build", { path: "0.9.4", author: "claude" });
  check("a version-string build shows without a path row", await waitFor(`document.querySelector(".build-card .build-name")?.textContent === "0.9.4" && !document.querySelector(".build-path-row")`, 6000));
  check("a version string offers neither Open folder nor Run", await evaluate(`return !document.querySelector('[data-issue-build="open"], [data-issue-build="run"]')`));
  check("two consecutive builds stay expanded", await evaluate(`return !document.querySelector('.tl-build-run') && [...document.querySelectorAll('.tl-event')].filter(e => e.textContent.includes('published build')).length === 2`));
  await api("POST", "/api/projects/uicheck/build", { path: "0.9.5", author: "claude" });
  check("three builds collapse with the newest label and time", await waitFor(`document.querySelector('.tl-build-run:not([open]) > summary')?.textContent.includes('published 3 builds · newest') && document.querySelector('.tl-build-run > summary .build-ref')?.textContent === '0.9.5' && !!document.querySelector('.tl-build-run > summary [title]')`, 6000));
  check("collapsed entries are hidden", await evaluate(`return [...document.querySelectorAll('.tl-build-entries .tl-event')].every(e => e.getBoundingClientRect().height === 0)`));
  await evaluate(`document.querySelector('.tl-build-run > summary').focus()`);
  // CDP key events exercise the browser's native details keyboard behavior.
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  check("Enter expands all original build entries", await waitFor(`document.querySelector('.tl-build-run')?.open && [...document.querySelectorAll('.tl-build-entries .tl-event')].length === 3 && [...document.querySelectorAll('.tl-build-entries .tl-event')].every(e => e.getBoundingClientRect().height > 0)`, 2000));
  await api("POST", "/api/projects/uicheck/build", { path: "0.9.6", author: "claude" });
  check("live build updates preserve expansion and newest label", await waitFor(`document.querySelector('.tl-build-run')?.open && document.querySelector('.tl-build-run > summary')?.textContent.includes('published 4 builds') && document.querySelector('.tl-build-run > summary .build-ref')?.textContent === '0.9.6'`, 6000));
  await evaluate(`document.querySelector('.tl-build-run > summary').focus()`);
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: " ", code: "Space", windowsVirtualKeyCode: 32 });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: " ", code: "Space", windowsVirtualKeyCode: 32 });
  check("Space collapses the run again", await waitFor(`!document.querySelector('.tl-build-run')?.open`, 2000));
  await api("POST", `/api/issues/${multi.id}/comments`, { author: "owner", text: "Build run separator" });
  for (const path of ["0.9.7", "0.9.8", "0.9.9"]) await api("POST", "/api/projects/uicheck/build", { path, author: "claude" });
  check("comments separate independently collapsed build runs", await waitFor(`document.querySelectorAll('.tl-build-run').length === 2 && [...document.querySelectorAll('.tl-build-run > summary')].map(e => e.textContent).join('|').includes('published 3 builds') && [...document.querySelectorAll('.tl-comment')].some(e => e.textContent.includes('Build run separator') && e.previousElementSibling?.matches('.tl-build-run') && e.nextElementSibling?.matches('.tl-build-run'))`, 6000));
  // A build on this machine: Open folder and Run appear. They are never clicked here (nothing is started).
  const buildDir = mkdtempSync(join(tmpdir(), "pairdesk-build-"));
  const buildExe = join(buildDir, process.platform === "win32" ? "UICheck.exe" : "UICheck");
  writeFileSync(buildExe, "not a game", { mode: 0o755 });
  await api("POST", "/api/projects/uicheck/build", { path: buildExe, commit: "c0ffee1234", label: "local 13", author: "claude" });
  check("a build on this machine offers Open folder and Run", await waitFor(`document.querySelector('[data-issue-build="open"]') && document.querySelector('[data-issue-build="run"]')`, 6000));
  await evaluate(`document.querySelector("[data-build-pop]").click()`);
  check("the build chip opens its popover with the actions", await waitFor(`!document.querySelector("#build-pop").hidden && document.querySelectorAll("#build-pop [data-build-act]").length === 3`, 3000));
  await shot("05e-build-popover");
  await evaluate(`document.querySelector('#build-pop [data-build-act="copy"]').click()`);
  check("the popover copies the path", await waitFor(`[...document.querySelectorAll(".toast")].some(t => t.textContent.includes("Build path copied"))`, 3000));
  await evaluate(`document.body.click()`);
  check("a click elsewhere closes the popover", await waitFor(`document.querySelector("#build-pop")?.hidden`, 2000));
  check("the page's own run request without the token is refused", await evaluate(`return (await fetch("/api/projects/uicheck/build/run", { method: "POST" })).status`) === 403);
  await api("POST", "/api/projects/uicheck/build", { path: "0.9.5", author: "claude" });
  rmSync(buildDir, { recursive: true, force: true });

  // ---------------------------------------------------------------- live updates (Server-Sent Events)
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${a.id}` });
  check("live stream connects", await waitFor(`document.body.dataset.live === "sse"`, 6000), await evaluate(`return document.body.dataset.live`));
  await api("POST", `/api/issues/${a.id}/comments`, { author: "claude", text: "Live from the agent: tips rounded in 5ea28d6" });
  // well inside the 60 s polling safety net: only the stream can bring it this fast
  check("an agent comment appears without a reload", await waitFor(`[...document.querySelectorAll(".bubble-body")].some(b => b.textContent.includes("Live from the agent"))`, 3000));
  check("the new timeline entry is highlighted", await evaluate(`return !!document.querySelector(".tl-comment.fresh")`));

  // ---------------------------------------------------------------- plan panel
  const planned = await api("POST", "/api/projects/uicheck/issues", { title: "Plan the lava rework", kind: "task", status: "in_progress", source: "agent" });
  await api("POST", `/api/issues/${planned.id}/plan`, { steps: [{ text: "Round the tips", state: "done", commit: "5ea28d6" }, "Clamp flows to the ground", "Stage shot 12"], verification: "stage shot 12 shows rounded tips", actor: "claude" });
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${planned.id}` });
  check("plan panel lists the steps", await waitFor(`document.querySelectorAll("#plan-section .step").length === 3`, 5000));
  check("plan progress 1/3", await evaluate(`return document.querySelector("#plan-progress")?.textContent`) === "1/3");
  check("plan sits above the timeline", await evaluate(`const p = document.querySelector("#plan-section"), t = document.querySelector(".timeline"); return !!(p && t && (p.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING))`));
  check("list row shows plan progress", await waitFor(`document.querySelector('.issue-row[data-id="${planned.id}"] .plan-badge')?.textContent === "1/3"`, 5000), await evaluate(`return document.querySelector('.issue-row[data-id="${planned.id}"] .plan-badge')?.textContent`));
  await shot("08-plan");
  await evaluate(`document.querySelector('[data-tick="2"]').click()`);
  check("owner ticks a step", await waitFor(`document.querySelector("#plan-progress")?.textContent === "2/3"`, 4000));
  let pl = await api("GET", `/api/issues/${planned.id}`);
  check("the tick is stored and in the timeline", pl.plan.steps[1].state === "done" && pl.activity.some((x) => x.action === "plan" && x.actor === "owner" && x.detail.to === "done"), pl.plan);
  await evaluate(`document.querySelector('[data-note="3"]').click()`);
  await evaluate(`const i = document.querySelector("#step-note-input"); i.value = "Use the evening light"; i.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));`);
  await sleep(500);
  pl = await api("GET", `/api/issues/${planned.id}`);
  check("owner notes a step", pl.plan.steps[2].note === "Use the evening light", pl.plan.steps[2]);
  await api("PATCH", `/api/issues/${planned.id}/plan/steps/3`, { state: "done", commit: "abc1234", actor: "claude" });
  check("an agent tick streams in", await waitFor(`document.querySelector("#plan-progress")?.textContent === "3/3"`, 3000));

  // ---------------------------------------------------------------- multi-select merge
  const dupA = await api("POST", "/api/projects/uicheck/issues", { title: "Boat sinks at the harbor", status: "reported", source: "game", command: "/goto 40 2 40" });
  const dupB = await api("POST", "/api/projects/uicheck/issues", { title: "Boat sinks in the harbour again", status: "reported", source: "owner" });
  await api("POST", `/api/issues/${dupB.id}/comments`, { author: "owner", text: "Second time today" });
  await send("Page.navigate", { url: `${BASE}/#/uicheck` });
  check("both duplicates listed", await waitFor(`document.querySelector('.issue-row[data-id="${dupA.id}"]') && document.querySelector('.issue-row[data-id="${dupB.id}"]')`, 5000));
  await evaluate(`document.querySelector('.issue-row[data-id="${dupB.id}"] input[data-select]').click()`);
  await evaluate(`document.querySelector('.issue-row[data-id="${dupA.id}"] .row-title').dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true }))`);
  check("checkbox + shift-click select a range", await waitFor(`document.querySelector("#selection-bar strong")?.textContent === "2 selected"`, 2000), await evaluate(`return document.querySelector("#selection-bar")?.textContent`));
  check("shift-click did not open the issue", await evaluate(`return location.hash === "#/uicheck"`));
  await evaluate(`document.activeElement.blur()`);
  await key("m");
  check("m opens Merge into…", await waitFor(`document.querySelector("#merge-dialog").open`));
  check("the oldest is the default target", await evaluate(`return document.querySelector('#merge-form input[name="target"]:checked')?.value`) === dupA.id);
  await shot("09-merge-dialog");
  await evaluate(`const f = document.querySelector("#merge-form"); f.requestSubmit(f.querySelector('button[value="ok"]'));`);
  check("merge opens the target", await waitFor(`location.hash === "#/uicheck/${dupA.id}" && document.querySelector(".merged-card")`, 5000), await evaluate(`return location.hash`));
  const merged = await api("GET", `/api/issues/${dupB.id}`);
  check("source redirects to the target", merged.id === dupA.id && merged.redirected_from === dupB.id, merged.id);
  check("the source's comment moved, marked", await evaluate(`return [...document.querySelectorAll(".tl-comment")].some(c => c.textContent.includes("Second time today") && c.textContent.includes("merged from ${dupB.id}"))`));
  await shot("10-merged");
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${dupB.id}` });
  check("opening a merged id lands on the target", await waitFor(`location.hash === "#/uicheck/${dupA.id}"`, 5000), await evaluate(`return location.hash`));
  await evaluate(`window.confirm = () => true; document.querySelector('.merged-sources [data-unmerge="${dupB.id}"]').click()`);
  await sleep(700);
  check("unmerge restores the source", (await api("GET", `/api/issues/${dupB.id}`)).id === dupB.id);

  // ---------------------------------------------------------------- parent / children
  const epic = await api("POST", "/api/projects/uicheck/issues", { title: "Water epic", kind: "task", status: "open" });
  const kids = [];
  for (const t of ["Shore foam", "Chunk seams"]) {
    const k = await api("POST", "/api/projects/uicheck/issues", { title: t, kind: "task", status: "open" });
    await api("POST", `/api/issues/${k.id}/parent`, { parent: epic.id });
    kids.push(k);
  }
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${epic.id}` });
  check("parent lists its children", await waitFor(`document.querySelectorAll("#children-section .child-row").length === 2`, 5000));
  check("list shows children progress", await waitFor(`document.querySelector('.issue-row[data-id="${epic.id}"] .progress-badge')?.textContent === "0/2"`, 5000));
  check("no close offer while children are open", await evaluate(`return !document.querySelector(".close-offer")`));
  for (const k of kids) await api("PATCH", `/api/issues/${k.id}`, { status: "closed", actor: "owner" });
  check("close offer appears when every child is done", await waitFor(`document.querySelector('[data-act="close-parent"]')`, 4000));
  await shot("11-children");
  await evaluate(`document.querySelector('[data-act="close-parent"]').click()`);
  await sleep(600);
  check("the parent closes itself", (await api("GET", `/api/issues/${epic.id}`)).status === "closed");
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${kids[0].id}` });
  check("a child names its parent", await waitFor(`document.querySelector(".part-of-line")?.textContent.includes("${epic.id}")`, 5000));

  // ---------------------------------------------------------------- backlog view and handoff
  await api("PATCH", `/api/issues/${kids[0].id}`, { status: "open", size: "S", actor: "owner" });
  await evaluate(`document.activeElement.blur()`);
  await key("b");
  check("b shows the backlog grouped by area", await waitFor(`document.querySelector(".group-head") && document.querySelector('.view-switch [data-view="backlog"]').classList.contains("on")`, 4000));
  await shot("12-backlog");
  await key("b");
  await api("POST", "/api/projects/uicheck/handoff", { markdown: "## State\n\nUI checks running\n\n## Next step\n\nShip PD-1\n", author: "claude" });
  await key("h");
  check("h opens the handoff", await waitFor(`location.hash.endsWith("/~handoff") && [...document.querySelectorAll(".handoff-section .md")].some(m => m.textContent.includes("Ship PD-1"))`, 5000));
  await api("POST", "/api/projects/uicheck/handoff", { section: "Traps", text: "Streamed trap", author: "codex" });
  check("the handoff updates live", await waitFor(`[...document.querySelectorAll(".handoff-section .md")].some(m => m.textContent.includes("Streamed trap"))`, 3000));
  await shot("13-handoff");
  await evaluate(`document.querySelector('.detail-head [data-act="close"]').click()`);

  // ---------------------------------------------------------------- splitter
  const paneW = () => evaluate(`return Math.round(document.querySelector("#list-pane").getBoundingClientRect().width)`);
  const w0 = await paneW();
  const sp = await evaluate(`const r = document.querySelector("#splitter").getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }`);
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: sp.x, y: sp.y });
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: sp.x, y: sp.y, button: "left", clickCount: 1 });
  for (let n = 1; n <= 5; n++) await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: sp.x - 40 * n, y: sp.y, button: "left", buttons: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: sp.x - 200, y: sp.y, button: "left", clickCount: 1 });
  const w1 = await paneW();
  check("dragging the splitter resizes the list", Math.abs(w0 - 200 - w1) <= 12, { w0, w1 });
  check("the split is remembered", Number(await evaluate(`return localStorage.getItem("pairdesk.split")`)) > 0);
  await evaluate(`document.querySelector("#splitter").focus()`);
  await evaluate(`document.querySelector("#splitter").dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }))`);
  check("Right arrow widens the list", (await paneW()) > w1, await paneW());
  const w2 = await paneW();
  await send("Page.reload");
  await sleep(300);
  check("the split survives a reload", await waitFor(`document.querySelector("#list-pane") && Math.abs(document.querySelector("#list-pane").getBoundingClientRect().width - ${w2}) <= 4`, 5000));
  await evaluate(`document.querySelector("#splitter").dispatchEvent(new MouseEvent("dblclick", { bubbles: true }))`);
  await sleep(200);
  check("double-click restores the default split", Math.abs((await paneW()) - w0) <= 4 && (await evaluate(`return localStorage.getItem("pairdesk.split")`)) === null, { now: await paneW(), w0 });
  const narrowList = await evaluate(`document.querySelector("#layout").style.setProperty("--split-l", "0.01fr"); document.querySelector("#layout").style.setProperty("--split-r", "0.99fr"); return Math.round(document.querySelector("#list-pane").getBoundingClientRect().width)`);
  check("the list keeps its minimum width", narrowList >= 338, narrowList);
  await evaluate(`document.querySelector("#layout").style.removeProperty("--split-l"); document.querySelector("#layout").style.removeProperty("--split-r");`);
  await shot("14-split");

  // phone width, with an issue that has a plan open
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${planned.id}` });
  await waitFor(`document.querySelector("#plan-section")`, 5000);
  await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
  await sleep(500);
  check("no horizontal scroll at 390px", await evaluate(`return document.documentElement.scrollWidth <= 390`), await evaluate(`return document.documentElement.scrollWidth`));
  await shot("06-phone-detail");
  await evaluate(`document.querySelector('.detail-head [data-act="close"]').click()`);
  await sleep(300);
  check("phone: back returns to the list", await evaluate(`return !document.body.classList.contains("detail-open")`));
  check("phone: no splitter", await evaluate(`return getComputedStyle(document.querySelector("#splitter")).display === "none"`));
  await shot("07-phone-list");

  // ---------------------------------------------------------------- README screenshots (optional)
  // With a showcase project (e.g. the sample data of scripts/demo-desk.py), take the README shots
  // of it: the triage list with an open check, the backlog, the handoff, and a failed check in dark.
  const automatic = await api("POST", "/api/projects/uicheck/issues", { title: "short owner report", body: "owner reproduction", source: "owner", status: "auto_check" });
  await api("PATCH", `/api/issues/${automatic.id}`, { title: "Precise reproduction", body: "Agent steps", milestone: "Beta", actor: "agent" });
  await send("Page.navigate", { url: `${BASE}/#/uicheck/${automatic.id}` });
  check("owner wording survives agent clarification", await waitFor(`document.querySelector('.merged-card')?.textContent.includes("owner's original") && document.querySelector('.merged-card')?.textContent.includes('short owner report') && document.querySelector('.merged-card')?.textContent.includes('owner reproduction')`, 5000));
  check("automatic status badge renders", await evaluate(`return document.querySelector('#status-select.s-auto_check')?.value === 'auto_check'`));
  await evaluate(`document.activeElement.blur()`);
  await key("0");
  await evaluate(`document.querySelector('[data-view="board"]').click()`);
  check("board has separate automatic and manual columns", await waitFor(`document.querySelector('.board-column .s-auto_check') && document.querySelector('.board-column .s-to_check')`, 5000));
  await shot("14-auto-check-board");
  await evaluate(`document.querySelector('[data-view="list"]').click()`);
  await waitFor(`document.querySelector('[data-view="list"][aria-selected="true"]')`, 5000);
  await evaluate(`document.querySelector('[data-group="status"][data-value="auto_check"]').dispatchEvent(new MouseEvent('click', {bubbles: true, shiftKey: true}));`);
  check("automatic filter lists agent work", await waitFor(`document.querySelector('.issue-row[data-id="${automatic.id}"]') && [...document.querySelectorAll('.issue-row')].every(r => r.querySelector('.s-auto_check'))`, 5000));

  if (SHOTS && SHOWCASE) {
    await send("Emulation.setDeviceMetricsOverride", { width: 1360, height: 820, deviceScaleFactor: 1, mobile: false });
    await evaluate(`localStorage.removeItem("pairdesk.split"); localStorage.setItem("pairdesk.theme", "light")`);
    const all = (await api("GET", `/api/projects/${SHOWCASE}/issues?status=${["reported", "open", "in_progress", "to_check", "passed", "failed", "parked", "closed"].join(",")}`)).issues;
    const pick = (status) => all.find((i) => i.status === status && i.plan_progress?.total) || all.find((i) => i.status === status);
    const view = async (hash, ready, name, theme = "light") => {
      await evaluate(`localStorage.setItem("pairdesk.theme", "${theme}")`);
      await send("Page.navigate", { url: `${BASE}/${hash}` });
      await sleep(300);
      await evaluate(`document.documentElement.dataset.theme = "${theme}"`);
      check(`showcase ${name} renders`, await waitFor(ready, 6000), hash);
      await evaluate(`document.activeElement?.blur(); window.scrollTo(0, 0)`);
      await sleep(500);
      await shot(name);
    };
    await view(`#/${SHOWCASE}/${pick("to_check").id}`, `document.querySelector("#plan-section") && document.querySelectorAll(".issue-row").length > 3`, "readme-triage");
    await view(`#/${SHOWCASE}`, `document.querySelectorAll(".issue-row").length > 3`, "readme-list");
    await evaluate(`document.activeElement?.blur()`);
    await key("b");
    check("showcase backlog renders", await waitFor(`document.querySelector(".group-head")`, 4000));
    await sleep(400);
    await shot("readme-backlog");
    await key("b");
    await view(`#/${SHOWCASE}/~handoff`, `document.querySelectorAll(".handoff-section").length >= 3`, "readme-handoff");
    await view(`#/${SHOWCASE}/${pick("failed").id}`, `document.querySelector(".detail-title")`, "readme-dark", "dark");
    await evaluate(`localStorage.removeItem("pairdesk.theme")`);
  }

  check("no page exceptions or console errors", errors.length === 0, errors);
} catch (e) {
  console.error(e);
  results.push(false);
} finally {
  try { ws?.close(); } catch { /* closing */ }
  proc.kill();
  await sleep(500);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* browser may still hold files */ }
}
const ok = results.filter(Boolean).length;
console.log(`${ok}/${results.length} UI checks passed`);
process.exit(results.length && results.every(Boolean) ? 0 : 1);
