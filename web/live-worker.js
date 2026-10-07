// One stream per origin shared by all tabs and projects. Unsupported browsers
// retain short polling, so they never consume one permanent socket per tab.
const clients = new Map();
let stream = null, live = false;

function broadcast(type, data) {
  for (const [port, client] of clients) {
    const changes = type === 'change' ? data.changes.filter(change => change.project === client.slug) : null;
    if (changes && !changes.length) continue;
    try { port.postMessage({ type, data: changes ? { ...data, changes } : data }); }
    catch { release(port); }
  }
}
function connect() {
  if (stream || !clients.size) return;
  stream = new EventSource('/api/events');
  stream.addEventListener('hello', event => { live = true; broadcast('hello', JSON.parse(event.data)); });
  for (const type of ['change', 'refresh']) stream.addEventListener(type, event => broadcast(type, JSON.parse(event.data)));
  stream.onerror = () => { live = false; broadcast('offline', {}); };
}
function release(port) {
  clients.delete(port);
  if (!clients.size && stream) { stream.close(); stream = null; live = false; }
}
self.onconnect = event => {
  const port = event.ports[0];
  port.onmessage = ({ data }) => {
    if (data.type === 'close') { release(port); port.close(); return; }
    if (data.type === 'subscribe') {
      clients.set(port, { slug: data.slug, seen: Date.now() });
      connect();
      if (live) port.postMessage({ type: 'hello', data: {} });
    } else if (clients.has(port)) clients.get(port).seen = Date.now();
  };
  port.start();
};
// Crash cleanup; pagehide releases promptly. A page renews when it becomes
// visible and always catches up through a fresh API read after reconnecting.
setInterval(() => {
  const cutoff = Date.now() - 120000;
  for (const [port, client] of clients) if (client.seen < cutoff) release(port);
}, 30000);
