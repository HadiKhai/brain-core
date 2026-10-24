import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
const js = ts.transpile(readFileSync(new URL('../src/live.ts', import.meta.url), 'utf8'), { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 });
const live = await import('data:text/javascript,' + encodeURIComponent(js));
function environment() {
  const names = ['document', 'EventSource', 'fetch', 'setTimeout', 'clearTimeout'];
  const saved = names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]);
  let clock = 0, serial = 0, partial = true, reads = 0;
  const timers = new Map(), listeners = new Map(), streams = [], values = [], statuses = [];
  const data = () => ({ brain: { now: '2026-01-01T00:00:00Z', filling_in: partial }, nodes: [{ title: 'Meadow' }] });
  Object.assign(globalThis, {
    document: { hidden: false, addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) },
    EventSource: class { closed = false; constructor() { streams.push(this); } close() { this.closed = true; } },
    fetch: async () => { reads++; return { ok: true, status: 200, headers: new Headers({ 'X-Brain-UI-Version': partial ? '1' : '2' }), json: async () => data() }; },
    setTimeout: (fn, delay) => { const id = ++serial; timers.set(id, { fn, at: clock + delay }); return id; },
    clearTimeout: id => timers.delete(id),
  });
  const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  return { values, statuses, streams, listeners, full: () => { partial = false; }, reads: () => reads, flush,
    start: () => live.watchLive('demo', value => values.push(value), value => statuses.push(value)),
    tick: async ms => {
      const target = clock + ms;
      while (true) {
        const next = [...timers].filter(([, timer]) => timer.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        clock = next[1].at; timers.delete(next[0]); next[1].fn(); await flush();
      }
      clock = target; await flush();
    },
    restore: () => saved.forEach(([name, descriptor]) => descriptor ? Object.defineProperty(globalThis, name, descriptor) : delete globalThis[name]),
  };
}
test('a displayed partial reading stays marked after idle closes SSE and fills on the next bounded poll', async () => {
  const env = environment(); let stop;
  try {
    stop = env.start(); await env.flush();
    assert.equal(env.values.at(-1).brain.filling_in, true);
    assert.equal(env.streams.length, 1);
    await env.tick(5_000);
    assert.equal(env.streams[0].closed, true);
    const serverReady = { version: 2, refreshing: false, failed: false };
    const now = Date.parse('2026-01-01T00:00:12Z');
    assert.match(live.readingMarker(env.values.at(-1), serverReady, 'Updates each minute', now), /Filling in.*12s old/);
    env.full(); await env.tick(1_001);
    assert.equal(env.values.at(-1).brain.filling_in, false);
    assert.equal(live.readingMarker(env.values.at(-1), serverReady, 'Updates each minute', now), null);
    const count = env.reads(); await env.tick(59_000);
    assert.equal(env.reads(), count, 'the complete reading must return to minute polling');
  } finally { stop?.(); env.restore(); }
});
test('a stale, refreshing, failed or unconfirmed reading shows its own age until its version arrives', () => {
  const value = { brain: { now: '2026-01-01T00:00:00Z' }, __ui_version: '1' };
  const now = Date.parse('2026-01-01T00:00:42Z');
  for (const progress of [null, { version: 2 }, { version: 1, refreshing: true }, { version: 1, failed: true }])
    assert.match(live.readingMarker(value, progress, 'Live', now), /42s old/);
  assert.match(live.readingMarker(value, { version: 1 }, 'Connection failed', now), /42s old/);
  assert.equal(live.readingMarker(value, { version: 1 }, 'Live', now), null);
});
test('stream delivery carries the displayed version and disposed partial polls stop', async () => {
  const env = environment(); let stop;
  try {
    stop = env.start(); await env.flush();
    assert.equal(env.values.at(-1).__ui_version, '1');
    env.streams[0].onmessage({ data: JSON.stringify({ brain: { filling_in: false } }), lastEventId: '9' });
    assert.equal(env.values.at(-1).__ui_version, '9');
    stop(); const count = env.reads(); await env.tick(120_000);
    assert.equal(env.reads(), count);
  } finally { stop?.(); env.restore(); }
});

test('a failed refresh keeps the partial reading marked and explains why details have not arrived', () => {
  const value = { brain: { now: '2026-01-01T00:00:00Z', filling_in: true }, __ui_version: '1' };
  const now = Date.parse('2026-01-01T00:00:12Z');
  assert.match(live.readingMarker(value, { version: 1, failed: true }, 'Live', now), /Filling in.*Refresh failed.*12s old/);
  for (const connection of ['Connection failed', 'Updates interrupted', 'Permission required'])
    assert.ok(live.readingMarker(value, { version: 1 }, connection, now).includes(connection));
  assert.match(live.readingMarker({ ...value, brain: { filling_in: true } }, null, 'Live', now), /Filling in.*age unknown/);
});
