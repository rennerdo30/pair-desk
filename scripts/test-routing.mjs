import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const S = { slug: 'mygame', list: { issues: [] } };
const location = { hash: '#/mygame/MG-1' };
let resolveFirst, response, draws = [], requests = [];
const context = vm.createContext({
  S, location, AbortController,
  api: (...args) => { requests.push(args); return response; },
  fail: e => { throw e; }, closeDetail: () => { draws.push('close'); },
  history: { replaceState() {} }, toast() {}, go() {},
  timelineKeys: () => new Set(), document: { body: { classList: { add() {} } } },
  $$: () => [], paintWindows() {}, setCursor() {}, resumeSendPolling() {},
  renderDetail: () => draws.push(S.openId),
});
vm.runInContext(source.slice(source.indexOf('let issueRequest = null'), source.indexOf('function closeDetail(')), context);
response = new Promise(resolve => { resolveFirst = resolve; });
const first = context.openIssue('MG-1');
location.hash = '#/mygame/MG-2';
response = Promise.resolve({ id: 'MG-2', project: 'mygame' });
await context.openIssue('MG-2');
assert(requests[0].at(-1).aborted, 'new navigation cancels the old read');
resolveFirst({ id: 'MG-1', project: 'mygame' });
await first;
assert.deepEqual(draws, ['MG-2'], 'late detail cannot overwrite the chosen issue');
response = new Promise(resolve => { resolveFirst = resolve; });
const handoffRace = context.openIssue('MG-2');
location.hash = '#/mygame/~handoff';
resolveFirst({ id: 'MG-2', project: 'mygame' });
await handoffRace;
assert.deepEqual(draws, ['MG-2'], 'pending detail cannot replace a handoff');
console.log('Routing checks passed (cancellation, stale detail, handoff race).');
