// Fixed CSS geometry and keyed nodes keep scrolling and live changes bounded by
// the viewport. No individual row is measured during a scroll or refresh.
export const ROW_HEIGHT = 68, BOARD_ROW_HEIGHT = 104, GROUP_HEIGHT = 36, OVERSCAN = 5;

export class ListWindow {
  constructor(host, viewport, offset = 0) {
    Object.assign(this, { host, viewport, offset, items: [], nodes: new Map() });
  }
  setItems(items) {
    this.items = items;
    this.positions = new Map();
    this.headers = [];
    let top = 0;
    for (const item of items) {
      item.top = top;
      this.positions.set(item.key, item);
      if (item.header) this.headers.push(item);
      top += item.height;
    }
    this.height = top;
    this.host.style.height = `${top}px`;
  }
  indexAt(y) {
    let lo = 0, hi = this.items.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1, item = this.items[mid];
      if (item.top + item.height <= y) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }
  render(scrollTop, viewportHeight) {
    const y = Math.max(0, scrollTop - this.offset);
    const start = Math.max(0, this.indexAt(y) - OVERSCAN);
    const end = Math.min(this.items.length, this.indexAt(y + viewportHeight) + OVERSCAN + 1);
    const visible = viewportHeight > 0 ? this.items.slice(start, end) : [];
    let pinned, next;
    for (const head of viewportHeight > 0 ? this.headers : []) {
      if (head.top <= y) pinned = head;
      else { next = head; break; }
    }
    if (pinned && !visible.includes(pinned)) visible.unshift(pinned);
    const keep = new Set(visible.map(item => item.key));
    for (const [key, entry] of this.nodes) {
      if (!keep.has(key)) { entry.node.remove(); this.nodes.delete(key); }
    }
    const fragment = document.createDocumentFragment();
    for (const item of visible) {
      const { html, stamp } = item.content();
      let entry = this.nodes.get(item.key);
      if (!entry) {
        const template = document.createElement('template');
        template.innerHTML = html;
        entry = { node: template.content.firstElementChild, stamp };
        this.nodes.set(item.key, entry);
        fragment.append(entry.node);
      } else if (entry.stamp !== stamp) {
        const focused = entry.node.contains(document.activeElement);
        const template = document.createElement('template');
        template.innerHTML = html;
        const replacement = template.content.firstElementChild;
        entry.node.className = replacement.className;
        for (const attr of replacement.attributes) entry.node.setAttribute(attr.name, attr.value);
        entry.node.innerHTML = replacement.innerHTML;
        entry.stamp = stamp;
        if (focused) entry.node.querySelector('input, button')?.focus({ preventScroll: true });
      }
      const top = item === pinned ? Math.max(item.top, Math.min(y, (next?.top ?? this.height) - item.height)) : item.top;
      entry.node.style.top = `${top}px`;
      entry.node.style.height = `${item.height}px`;
      entry.node.classList.add('window-item');
    }
    this.host.append(fragment);
    // Reordering existing keys must also retain a logical tab order.
    let before = this.host.firstElementChild;
    for (const item of visible) {
      const node = this.nodes.get(item.key).node;
      if (node !== before) this.host.insertBefore(node, before);
      before = node.nextElementSibling;
    }
  }
  reveal(key, scrollTop, viewportHeight) {
    const item = this.positions.get(key);
    if (!item) return false;
    const top = item.top + this.offset, heading = this.headers.length ? GROUP_HEIGHT : 0;
    if (top < scrollTop + heading) this.viewport.scrollTop = Math.max(0, top - heading);
    else if (top + item.height > scrollTop + viewportHeight) this.viewport.scrollTop = top + item.height - viewportHeight;
    return true;
  }
}
