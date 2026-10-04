// Real first rendered list timings against an isolated desk; no packages required (Node 22+).
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname, basename } from 'node:path';
const [base = 'http://127.0.0.1:8799', project = 'mygame', out = '.cache/ui.json'] = process.argv.slice(2);
const browser = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe', '/usr/bin/chromium', '/usr/bin/google-chrome',
  '/usr/bin/microsoft-edge', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
  .find(existsSync);
if (!browser) throw new Error('No Edge/Chrome installed');
const profile = mkdtempSync(join(tmpdir(), 'pairdesk-bench-'));
const port = 9400 + Math.floor(Math.random() * 400);
const proc = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-features=BackForwardCache', '--disable-background-timer-throttling',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  '--window-size=1500,950', ...(process.env.CI ? ['--no-sandbox'] : []), 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws, seq = 0;
const pending = new Map(), errors = [], requests = new Map();
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++seq; pending.set(id, { resolve, reject }); ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expression => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
  return r.result.value;
};
const stats = values => {
  const sorted = [...values].sort((a, b) => a - b);
  return { n: sorted.length, p50_ms: +((sorted[(sorted.length - 1) >> 1] + sorted[sorted.length >> 1]) / 2).toFixed(3),
    p95_ms: +sorted[Math.ceil(sorted.length * .95) - 1].toFixed(3) };
};
try {
  let target;
  for (let i = 0; i < 80 && !target; i++) {
    try { target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === 'page'); }
    catch { await sleep(100); }
  }
  if (!target) throw new Error('Browser did not start');
  ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r));
  ws.addEventListener('message', ev => {
    const m = JSON.parse(ev.data);
    if (pending.has(m.id)) {
      const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    }
    if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === 'Network.requestWillBeSent') requests.set(m.params.requestId, { url: m.params.request.url, state: 'pending' });
    if (m.method === 'Network.responseReceived' && requests.has(m.params.requestId)) requests.get(m.params.requestId).status = m.params.response.status;
    if (m.method === 'Network.loadingFinished' && requests.has(m.params.requestId)) requests.get(m.params.requestId).state = 'done';
    if (m.method === 'Network.loadingFailed' && requests.has(m.params.requestId)) requests.get(m.params.requestId).state = m.params.errorText;
  });
  await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
  // Measure in-page time to rows and the next painted frame, not CDP polling time.
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    const obs = new MutationObserver(() => {
      if (document.querySelector('.issue-row') && !window.__listMeasured) {
        window.__listMeasured = true;
        requestAnimationFrame(() => requestAnimationFrame(() => { window.__listMs = performance.now(); }));
      }
    });
    obs.observe(document, {childList: true, subtree: true});
  ` });
  const result = {};
  await send('Page.navigate', { url: `${base}/#/${project}` });
  await sleep(1000);
  for (const cold of [true, false]) {
    await send('Network.setCacheDisabled', { cacheDisabled: cold });
    const times = [];
    for (let i = 0; i < 21; i++) {
      const previousOrigin = await evaluate('performance.timeOrigin');
      await send('Page.reload', { ignoreCache: cold });
      let ms;
      for (let j = 0; j < 200; j++) {
        ms = await evaluate(`performance.timeOrigin !== ${previousOrigin} ? (window.__listMs || 0) : 0`);
        if (ms) break;
        await sleep(30);
      }
      if (!ms) throw new Error('List never rendered: ' + JSON.stringify(await evaluate(`({url:location.href,
        rows:document.querySelectorAll('.issue-row').length, measured:window.__listMeasured,
        body:document.body?.innerText.slice(0, 1500)})`)) + ' errors=' + JSON.stringify(errors)
        + ' requests=' + JSON.stringify([...requests.values()].slice(-18)));
      if (i) times.push(ms);
      if (i % 5 === 0) console.log('browser sample', cold ? 'cold' : 'warm', i, ms.toFixed(1));
      // Let the initial live-stream catch-up finish before destroying this document.
      // Otherwise a reload hammer can abort in-flight cache revalidations mid-bootstrap.
      await sleep(200);
    }
    result[cold ? 'cold_initial_list' : 'warm_initial_list'] = stats(times);
  }
  result.errors = errors;
  if (errors.length) throw new Error(JSON.stringify(errors));
  writeFileSync(out, JSON.stringify(result, null, 2) + '\n');
  console.log('browser', JSON.stringify(result));
} finally {
  ws?.close(); proc.kill(); await sleep(500);
  const ownedProfile = resolve(profile);
  if (dirname(ownedProfile) !== resolve(tmpdir()) || !basename(ownedProfile).startsWith('pairdesk-bench-')) {
    throw new Error('Refusing cleanup outside the owned browser profile');
  }
  try { rmSync(ownedProfile, { recursive: true, force: true }); } catch { /* owned profile may be held briefly */ }
}
