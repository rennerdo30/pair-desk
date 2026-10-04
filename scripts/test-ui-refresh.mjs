// Behavior checks for refresh cancellation, conditional redraws and navigation races.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const etag = Symbol('etag');
const state = { slug: 'mygame', view: 'list', listSeq: 0, list: {}, rowStamp: new Map(), selected: new Set() };
const draws = [], errors = [], requests = [];
let response;
const context = vm.createContext({
  S: state, RESPONSE_ETAG: etag, AbortController,
  listQuery: () => 'same=query',
  api: (...args) => { requests.push(args); return response; },
  renderFilters: () => {}, renderList: () => draws.push(state.view),
  fail: e => errors.push(e), renderDetail: () => draws.push('detail'),
  timelineKeys: () => new Set(), go: () => draws.push('navigate'),
});
vm.runInContext(source.slice(source.indexOf('let listRequest = null;'), source.indexOf('function saveFilters()')), context);
vm.runInContext(source.slice(source.indexOf('async function refreshIssue()'), source.indexOf('async function refreshAll()')), context);
const list = () => ({ issues: [{id: 'MG-1', updated_at: 'same'}], [etag]: 'unchanged' });
response = Promise.resolve(list());
await context.loadList();
response = Promise.resolve(list());
await context.loadList();
assert.deepEqual(draws, ['list'], 'unchanged refresh must preserve the DOM');
state.view = 'board';
response = Promise.resolve(list());
await context.loadList();
assert.deepEqual(draws, ['list', 'board'], 'same payload still needs a redraw after a view change');

let finish;
response = new Promise(resolve => { finish = resolve; });
const obsolete = context.loadList();
const oldSignal = requests.at(-1).at(-1);
response = Promise.resolve(list());
await context.loadList();
assert.equal(oldSignal.aborted, true, 'superseded requests must be cancelled');
finish({ issues: [{id: 'MG-99', updated_at: 'later'}], [etag]: 'obsolete' });
await obsolete;
assert.equal(state.list.issues[0].id, 'MG-1', 'late list responses cannot overwrite the newest list');

state.openId = 'MG-1';
response = new Promise(resolve => { finish = resolve; });
const lateDetail = context.refreshIssue();
state.openId = null;
state.handoff = { version: 1 };
finish({ id: 'MG-1' });
await lateDetail;
assert.deepEqual(draws, ['list', 'board'], 'late detail must not navigate away from the handoff');
state.openId = 'MG-1';
response = Promise.resolve({ id: 'MG-1' });
await context.refreshIssue();
assert.equal(draws.at(-1), 'detail');
assert.equal(errors.length, 0);
console.log('UI refresh checks passed (unchanged DOM, view changes, cancellation, stale lists and details).');
