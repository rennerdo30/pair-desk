// Real Edge/Chrome checks and same-data benchmarks. Point only at a copied desk:
// node scripts/test-tabs-list.mjs http://127.0.0.1:8808 after animasky
// "before" records the old behaviour without requiring the fixes to pass.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const BASE = process.argv[2] || 'http://127.0.0.1:8808';
const MODE = process.argv[3] || 'after', SLUG = process.argv[4] || 'animasky';
const POLLING = process.argv.includes('--poll'), CHECKS_ONLY = process.argv.includes('--checks');
const LABEL = MODE + (POLLING ? '-poll' : '') + (CHECKS_ONLY ? '-checks' : '');
assert(['before', 'after'].includes(MODE), 'mode must be before or after');
assert(new URL(BASE).port !== '8765', 'never run this writable check against the owner desk on 8765');
const browsers = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe', '/usr/bin/google-chrome', '/usr/bin/chromium'];
const browser = browsers.find(existsSync);
assert(browser, 'Edge or Chrome is required');
mkdirSync('.cache', { recursive: true });
const profile = mkdtempSync(resolve('.cache/edge-pd8-'));
const debugPort = 9600 + Math.floor(Math.random() * 1000);
const proc = spawn(browser, ['--headless=new', '--disable-gpu', '--no-first-run', '--disable-extensions', '--disable-background-timer-throttling', '--disable-renderer-backgrounding', `--log-net-log=${resolve('.cache/pd8-'+LABEL+'-netlog.json')}`, `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`, '--window-size=1500,950', ...(process.env.CI ? ['--no-sandbox'] : []), 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const report = { base: BASE, mode: MODE, project: SLUG, runs: [], tabs: [], errors: [] };
const sessions = [];
let browserSession;

async function attach(target) {
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r));
  let seq = 0;
  const pending = new Map(), requests = new Map(), responses = [];
  ws.addEventListener('message', event => {
    const m = JSON.parse(event.data);
    if (m.id) {
      const p = pending.get(m.id);
      if (!p) return;
      pending.delete(m.id); clearTimeout(p.timer);
      m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result);
    }
    if (m.method === 'Runtime.exceptionThrown') report.errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === 'Network.requestWillBeSent') requests.set(m.params.requestId, m.params.request.url);
    if (m.method === 'Network.loadingFinished' || m.method === 'Network.loadingFailed') requests.delete(m.params.requestId);
    if (m.method === 'Network.responseReceived') responses.push({ url: m.params.response.url, status: m.params.response.status });
  });
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++seq;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, { resolve, reject, timer }); ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  };
  const wait = async (expression, timeout = 7000) => {
    const until = Date.now() + timeout;
    while (Date.now() < until) { if (await evaluate(expression)) return true; await sleep(40); }
    return false;
  };
  await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
  if (POLLING) await send('Page.addScriptToEvaluateOnNewDocument', { source: "Object.defineProperty(window,'SharedWorker',{value:undefined,configurable:true})" });
  const s = { ws, send, evaluate, wait, requests, responses };
  sessions.push(s); return s;
}

async function newTab(url) {
  const t = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?about:blank`, { method: 'PUT' })).json();
  const s = await attach(t);
  await s.send('Page.navigate', { url }); return s;
}

try {
  let targets;
  for (let n = 0; n < 100; n++) {
    try { targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json(); break; } catch { await sleep(100); }
  }
  assert(targets, 'browser debugging endpoint did not start');
  const a = browserSession = await attach(targets.find(t => t.type === 'page'));
  await a.send('Page.addScriptToEvaluateOnNewDocument', { source: `window.pdFirstList=null;new MutationObserver(()=>{if(window.pdFirstList===null&&document.querySelector('.issue-row'))window.pdFirstList=performance.now()}).observe(document,{childList:true,subtree:true});` });
  await a.send('Page.navigate', { url: `${BASE}/#/${SLUG}` });
  assert(await a.wait(`!!document.querySelector('.issue-row')`), 'first tab list');
  const ids = await a.evaluate(`Array.from(document.querySelectorAll('.issue-row')).slice(0,2).map(r=>r.dataset.id)`);
  const second = await newTab(`${BASE}/#/${SLUG}/${ids[0]}`);
  const deepLink = await second.wait(`!!document.querySelector('.detail-title')`);
  await second.evaluate(`document.querySelector('.issue-row[data-id=${JSON.stringify(ids[1])}]')?.click()`);
  const click = await second.wait(`document.querySelector('[data-act="copy-id"]')?.textContent===${JSON.stringify(ids[1])}`);
  report.twoFreshTabs = { deepLink, click };
  if (MODE === 'after') assert(deepLink && click, 'two fresh tabs must open deep links and clicks');
  await second.send('Page.close');

  // Warm reads, same 2K-issue snapshot, same viewport, three runs, complete list.
  for (let run = 0; run < (CHECKS_ONLY ? 0 : 3); run++) {
    await a.evaluate(`localStorage.removeItem('pairdesk.filters.${SLUG}');localStorage.removeItem('pairdesk.view.${SLUG}')`);
    await a.send('Page.navigate', { url: 'about:blank' });
    await a.send('Page.navigate', { url: `${BASE}/#/${SLUG}` });
    assert(await a.wait(`window.pdFirstList!==null`), 'first list');
    const firstListMs = await a.evaluate('window.pdFirstList');
    await a.evaluate(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'0',bubbles:true}))`);
    assert(await a.wait(`document.querySelector('[data-group="status"][data-value="*"]')?.classList.contains('on')`), 'all filter');
    await a.evaluate(`const s=document.querySelector('#sort-select');s.value='number';s.dispatchEvent(new Event('change',{bubbles:true}))`);
    await sleep(400);
    for (let n = 0; n < 30; n++) {
      if (!await a.evaluate(`!!document.querySelector('#more')`)) break;
      const footer = await a.evaluate(`document.querySelector('#list-footer').textContent`);
      await a.evaluate(`document.querySelector('#more').click()`);
      assert(await a.wait(`document.querySelector('#list-footer').textContent!==${JSON.stringify(footer)}`), 'show more');
    }
    const count = await a.evaluate(`document.querySelector('#list-footer').textContent.match(/[0-9]+/)?.[0]`);
    const live = await a.evaluate(`(async()=>{
      const list=document.querySelector('#list'), row=list.querySelector('.issue-row'), id=row.dataset.id, title=row.querySelector('.row-title').textContent;
      const changed=title+' [PD8 benchmark '+${run}+']';let added=0,removed=0;const tasks=[];
      const obs=new MutationObserver(ms=>ms.forEach(m=>{added+=m.addedNodes.length;removed+=m.removedNodes.length}));obs.observe(list,{childList:true,subtree:true});
      const po=new PerformanceObserver(ms=>tasks.push(...ms.getEntries().map(e=>e.duration)));po.observe({entryTypes:['longtask']});
      const start=performance.now();await fetch('/api/issues/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:changed})});
      const deadline=performance.now()+7000;while(list.querySelector('.issue-row[data-id="'+id+'"] .row-title')?.textContent!==changed&&performance.now()<deadline)await new Promise(r=>setTimeout(r,20));
      const updateMs=performance.now()-start;obs.disconnect();po.disconnect();
      await fetch('/api/issues/'+id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});
      return {updateMs,added,removed,longTaskMs:tasks.reduce((a,b)=>a+b,0),maxLongTaskMs:Math.max(0,...tasks),renderedRows:list.querySelectorAll('.issue-row').length,updated:updateMs<7000};
    })()`);
    assert(live.updated, 'live update must arrive');
    await sleep(700);
    const scroll = await a.evaluate(`new Promise(resolve=>{const list=document.querySelector('#list'),intervals=[];list.scrollTop=0;let previous,start;function frame(t){if(start===undefined){start=previous=t} else intervals.push(t-previous);previous=t;list.scrollTop=(t-start)*6;if(t-start<2000)requestAnimationFrame(frame);else {const sorted=[...intervals].sort((a,b)=>a-b);resolve({fps:1000/(intervals.reduce((a,b)=>a+b,0)/intervals.length),p95FrameMs:sorted[Math.floor(sorted.length*.95)],maxFrameMs:Math.max(...intervals)})}}requestAnimationFrame(frame)})`);
    await a.evaluate(`document.querySelector('#list').scrollTop=0;document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))`);
    await sleep(100);
    const clickMs = await a.evaluate(`new Promise(resolve=>{const start=performance.now(),until=start+7000;document.querySelector('.issue-row').click();function frame(){if(document.querySelector('.detail-title'))resolve(performance.now()-start);else if(performance.now()>until)resolve(null);else requestAnimationFrame(frame)}requestAnimationFrame(frame)})`);
    assert(clickMs !== null, 'click-to-detail');
    report.runs.push({ firstListMs, clickMs, issueCount: Number(count), live, scroll });
    console.log('run', run + 1, JSON.stringify(report.runs.at(-1)));
  }

  if (MODE === 'after') {
    await a.send('Page.bringToFront');
    await a.evaluate(`document.querySelector('.issue-row').click()`);
    assert(await a.wait(`!!document.querySelector('.detail-title')`), 'first row opens before keyboard checks');
    await a.evaluate(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));document.activeElement.blur();for(let n=0;n<80;n++)document.dispatchEvent(new KeyboardEvent('keydown',{key:'j',bubbles:true}));`);
    assert.equal(await a.evaluate(`Number(document.querySelector('.issue-row.cursor')?.dataset.idx)`), 80, 'keyboard materializes an offscreen row');
    const geometry = await a.evaluate(`Array.from(document.querySelectorAll('.issue-row')).sort((a,b)=>Number(a.dataset.idx)-Number(b.dataset.idx)).map(r=>({idx:Number(r.dataset.idx),top:r.getBoundingClientRect().top,height:r.getBoundingClientRect().height}))`);
    assert(geometry.every(r => Math.abs(r.height - 68) < 1), 'row geometry matches the window model');
    assert(geometry.every((r,n) => !n || Math.abs(r.top - geometry[n-1].top - 68) < 1), 'no gaps or overlapping rows');
    await a.evaluate(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'x',bubbles:true}));document.querySelector('#list').scrollTop=0`);
    await sleep(100);
    const selected = await a.evaluate(`document.querySelector('#selection-bar strong')?.textContent`);
    assert.equal(selected, '1 selected', 'selection survives recycling');
    await a.evaluate(`document.querySelector('.issue-row').dispatchEvent(new MouseEvent('click',{shiftKey:true,bubbles:true}))`);
    assert.equal(await a.evaluate(`document.querySelector('#selection-bar strong')?.textContent`), '81 selected', 'shift selection includes offscreen logical rows');
    await a.evaluate(`document.querySelector('[data-sel="clear"]').click();document.querySelector('[data-view="backlog"]').click()`);
    assert(await a.wait(`!!document.querySelector('.group-head')`), 'backlog group headings');
    const backlogShot = await a.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(`.cache/pd8-${LABEL}-backlog.png`, Buffer.from(backlogShot.data, 'base64'));
    assert(await a.evaluate(`document.querySelectorAll('.issue-row').length<100`), 'backlog is windowed');
    await a.evaluate(`document.querySelector('.group-head').click()`);
    assert(await a.evaluate(`document.querySelector('.group-head')?.getAttribute('aria-expanded')==='false'`), 'group folds');
    await a.evaluate(`document.querySelector('.group-head').click();document.querySelector('[data-view="board"]').click()`);
    assert(await a.wait(`!!document.querySelector('.status-board')`), 'board renders');
    const boardShot = await a.send('Page.captureScreenshot', { format: 'png' });
    writeFileSync(`.cache/pd8-${LABEL}-board.png`, Buffer.from(boardShot.data, 'base64'));
    assert(await a.evaluate(`document.querySelectorAll('.issue-row').length<100`), 'board windows vertical and horizontal rows');
    await a.evaluate(`document.querySelector('[data-view="list"]').click()`);
    assert(await a.wait(`!document.querySelector('.status-board') && !!document.querySelector('.issue-row')`), 'triage restored');
    report.keyboardGrouping = true;
  }

  // Six persistent HTTP/1.1 streams exhaust the origin's socket pool. Open real
  // tabs sequentially so the sixth can paint a list before its detail is queued.
  for (let n = 0; n < 7; n++) {
    const tab = await newTab(`${BASE}/#/${SLUG}/${ids[n % ids.length]}`);
    const opened = await tab.wait(`!!document.querySelector('.detail-title')`, 2500);
    const state = await tab.evaluate(`({hash:location.hash,rows:document.querySelectorAll('.issue-row').length,detail:!!document.querySelector('.detail-title'),responsive:(document.body.dataset.probe='yes')==='yes'})`);
    report.tabs.push({ tab: n + 2, opened, ...state, pending: [...tab.requests.values()] });
    console.log('tab', JSON.stringify(report.tabs.at(-1)));
    if (MODE === 'after') assert(opened, `tab ${n + 2} must open`);
    if (!opened && MODE === 'before') break;
  }
  if (MODE === 'after') {
    assert.equal(report.errors.length, 0, 'no page exceptions');
    const keys = await a.evaluate(`document.querySelectorAll('.issue-row').length`);
    assert(keys < 100, 'DOM must remain bounded with the complete list');
    const targets = await a.send('Target.getTargets');
    report.sharedWorkers = targets.targetInfos.filter(t=>t.type==='shared_worker' && t.url===BASE+'/live-worker.js').length;
    assert.equal(report.sharedWorkers, POLLING ? 0 : 1, POLLING ? 'polling fallback opens no shared worker' : 'all tabs use one shared worker');
    if (POLLING) {
      await a.send('Page.bringToFront'); // hidden tabs intentionally defer polling until visible
      const row = await a.evaluate(`({id:document.querySelector('.issue-row').dataset.id,title:document.querySelector('.row-title').textContent})`);
      const changed = row.title + ' [polling check]';
      await a.evaluate(`fetch('/api/issues/'+${JSON.stringify(row.id)},{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:${JSON.stringify(changed)}})})`);
      try {
        assert(await a.wait(`document.querySelector('.issue-row[data-id="${row.id}"] .row-title')?.textContent===${JSON.stringify(changed)}`, 7000), 'polling delivers live edits');
      } finally {
        await a.evaluate(`fetch('/api/issues/'+${JSON.stringify(row.id)},{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:${JSON.stringify(row.title)}})})`);
      }
      report.pollingLiveUpdate = true;
    }
  }
  writeFileSync(`.cache/pd8-${LABEL}.json`, JSON.stringify(report, null, 2));
  await a.send('Page.bringToFront');
  const shot = await a.send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(`.cache/pd8-${LABEL}.png`, Buffer.from(shot.data, 'base64'));
  console.log('report', `.cache/pd8-${LABEL}.json`, JSON.stringify(report.twoFreshTabs));
} finally {
  if (browserSession) await browserSession.send('Browser.close').catch(() => {});
  sessions.forEach(s => s.ws.close());
  await Promise.race([new Promise(r => proc.once('exit', r)), sleep(2000)]);
  if (proc.exitCode === null) proc.kill();
}
