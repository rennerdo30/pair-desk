import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

let reap, now = 0;
const streams = [];
class Stream {
  constructor(url) { this.url = url; this.handlers = {}; streams.push(this); }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  emit(type, data) { this.handlers[type]({ data: JSON.stringify(data) }); }
  close() { this.closed = true; }
}
const self = {};
vm.runInNewContext(readFileSync(new URL('../web/live-worker.js', import.meta.url), 'utf8'), {
  self, EventSource: Stream, Date: { now: () => now }, setInterval: callback => { reap = callback; },
});
function port(slug) {
  const p = { messages: [], postMessage(m) { this.messages.push(m); }, start() {}, close() { this.closed = true; } };
  self.onconnect({ ports: [p] });
  p.onmessage({ data: { type: 'subscribe', slug } });
  return p;
}
const a = port('one'), b = port('one'), c = port('two');
assert.equal(streams.length, 1, 'all projects and tabs share one stream');
streams[0].emit('hello', {});
streams[0].emit('change', { changes: [{ project: 'one', issue: 'MG-1' }, { project: 'two', issue: 'OT-1' }] });
assert.equal(a.messages.at(-1).data.changes[0].issue, 'MG-1');
assert.equal(b.messages.at(-1).data.changes.length, 1);
assert.equal(c.messages.at(-1).data.changes[0].issue, 'OT-1');
streams[0].onerror();
assert.equal(a.messages.at(-1).type, 'offline');
streams[0].emit('hello', {});
assert.equal(a.messages.at(-1).type, 'hello', 'reconnect requests catch-up in the pages');
a.onmessage({ data: { type: 'close' } });
assert(!streams[0].closed, 'closing one tab cannot break its siblings');
now = 121000; reap();
assert(streams[0].closed, 'crashed tabs lose their leases');
b.onmessage({ data: { type: 'subscribe', slug: 'two' } });
assert.equal(streams.length, 2, 'resuming after lease expiry reopens the stream');
b.onmessage({ data: { type: 'close' } });
assert(streams[1].closed, 'the final tab closes the stream');
console.log('Shared live worker checks passed (one stream, project routing, disconnects, leases, cleanup).');
