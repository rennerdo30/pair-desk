// Isolated dialog behavior: no browser, server or real desk data.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const elements = new Map();
const element = key => {
  if (!elements.has(key)) elements.set(key, { innerHTML: '', textContent: '', disabled: false, hidden: false });
  return elements.get(key);
};
const dlg = element('#transfer-dialog');
dlg.showModal = () => { dlg.open = true; };
dlg.close = () => { dlg.open = false; dlg.onclose(); };
const filters = { status: ['open'], kind: [], priority: [], area: '', seed: '', merged: false, q: '', sort: 'triage' };
const state = { slug: 'mygame', project: { name: 'Game' }, filters, view: 'list' };
const requests = [], downloads = [];
let visible = [{ id: 'MG-1', title: 'First', status: 'open' }];
let nextRead;
let nextDownload;
let preview = { topics: 1, attachments: 0, source: { name: '<unsafe>' }, titles: ['<topic>'], notice: 'New copies', already_imported: false };
const esc = x => String(x).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = vm.createContext({
  S: state, PAGE: 300, URLSearchParams, structuredClone, Set, ICON: { close: '' },
  BACKLOG_STATUSES: ['open', 'in_progress'], STATUS_LABEL: { open: 'Open' }, esc, DESK_TOKEN: 'local-token',
  $: element, $$: () => [], renderFilters: () => {},
  api: async (method, url) => { requests.push(url); return nextRead ? nextRead : { issues: visible, total: visible.length, counts: {} }; },
  fetch: async (url, opts) => {
    downloads.push({ url, opts });
    if (nextDownload) return nextDownload;
    return { ok: true, blob: async () => 'zip', json: async () => url.endsWith('/import') ? { created: ['MG-2'] } : preview };
  },
  URL: { createObjectURL: () => 'blob:archive', revokeObjectURL: () => {} },
  document: { createElement: () => ({ click: () => downloads.push('clicked') }) },
  setTimeout: () => {}, toast: () => {}, loadList: () => {},
});
vm.runInContext(source.slice(source.indexOf('function listQuery('), source.indexOf('let listRequest = null;')), context);
vm.runInContext(source.slice(source.indexOf('async function openTransferDialog('), source.indexOf('function progressBadge(')), context);
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
const event = (dataset = {}, id = '') => ({ target: { dataset, id, closest: selector => selector === '[data-pick]' && dataset.pick ? { dataset } : null } });
const submit = () => element('form').onsubmit({ submitter: { value: 'ok' }, preventDefault() {} });

await context.openTransferDialog('export'); await flush();
assert.ok(requests.at(-1).includes('status=open'));
await dlg.onchange({ target: { dataset: { topic: 'MG-1' }, checked: true } });
visible = [{ id: 'MG-2', title: 'Second', status: 'open' }];
dlg.oninput({ target: { id: 'transfer-search', value: 'second' } }); await flush();
await dlg.onchange({ target: { dataset: { topic: 'MG-2' }, checked: true } });
assert.ok(element('#transfer-count').textContent.startsWith('2 selected'));
assert.equal(filters.q, '', 'export search must not alter normal list filters');
await submit();
assert.deepEqual(JSON.parse(downloads[0].opts.body).ids, ['MG-1', 'MG-2'], 'hidden selection must export too');
assert.ok(downloads.includes('clicked'), 'export must trigger ZIP download');

await context.openTransferDialog('export'); await flush();
dlg.onclick(event({ pick: 'page' }));
assert.ok(element('#transfer-count').textContent.startsWith('1 selected'));
dlg.onclick(event({ pick: 'clear' }));
assert.equal(element('#transfer-submit').disabled, true);
let resolveLate;
nextRead = new Promise(resolve => { resolveLate = resolve; });
dlg.oninput({ target: { id: 'transfer-search', value: 'late' } });
dlg.close();
nextRead = null;
await context.openTransferDialog('export'); await flush();
const current = element('#transfer-rows').innerHTML;
resolveLate({ issues: [{ id: 'MG-99', title: 'Stale', status: 'open' }], total: 1, counts: {} }); await flush();
assert.equal(element('#transfer-rows').innerHTML, current, 'closed dialog request must not replace new dialog');
dlg.close();

await context.openTransferDialog('export'); await flush();
dlg.onclick(event({ pick: 'page' }));
let completeDownload;
nextDownload = new Promise(resolve => { completeDownload = resolve; });
const oldSubmission = submit();
dlg.close();
await context.openTransferDialog('export'); await flush();
nextDownload = null;
completeDownload({ ok: true, blob: async () => 'zip' });
await oldSubmission;
assert.equal(dlg.open, true, 'old submission must not close a newly opened dialog');
dlg.close();

await context.openTransferDialog('import');
const candidate = { size: 12 };
await dlg.onchange({ target: { id: 'transfer-file', dataset: {}, files: [candidate] } });
assert.equal(element('#transfer-submit').disabled, false);
assert.ok(element('#transfer-preview').innerHTML.includes('&lt;unsafe&gt;'));
assert.ok(element('#transfer-preview').innerHTML.includes('&lt;topic&gt;'));
assert.equal(downloads.at(-1).opts.headers['X-Pair-Desk-Token'], 'local-token');
await submit();
assert.ok(downloads.at(-1).url.endsWith('/import'));
assert.equal(downloads.at(-1).opts.body, candidate, 'confirmation sends the same previewed File');
await context.openTransferDialog('import');
preview = { ...preview, already_imported: true };
await dlg.onchange({ target: { id: 'transfer-file', dataset: {}, files: [candidate] } });
assert.equal(element('#transfer-submit').disabled, true, 'duplicate archive must refuse confirmation');
assert.match(element('#transfer-error').textContent, /already imported/);
console.log('Transfer dialog checks passed: persistent selection, independent search, download, clear, stale requests, escaped preview, exact-file confirmation and duplicate refusal.');
