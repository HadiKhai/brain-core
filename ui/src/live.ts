// A visible, active dashboard streams changes. Idle tabs check once a minute;
// hidden tabs pause unless opted-in notifications need the minute check.
export type ReadingProgress = { version: number; refreshing?: boolean; failed?: boolean };

export function readingMarker(value: any, progress: ReadingProgress | null, connection: string, now = Date.now()): string | null {
  const partial = !!value?.brain?.filling_in;
  const cached = value?.source?.kind === 'cached-full';
  const version = value?.__ui_version;
  if (!partial && !cached && version == null) return null; // Older servers and saved snapshots have no delivery metadata.
  const confirmed = progress && String(progress.version) === version;
  const failure = progress?.failed ? 'Refresh failed' :
    ['Connection failed', 'Updates interrupted', 'Permission required'].includes(connection) ? connection : null;
  if (!partial && !cached && confirmed && !progress.refreshing && !failure) return null;
  const at = Date.parse(cached ? value?.source?.built_at : value?.brain?.now);
  const age = Number.isFinite(at) ? `reading ${Math.max(0, Math.floor((now - at) / 1000))}s old` : 'reading age unknown';
  const captured = cached && Number.isFinite(at) ? 'As of ' + new Intl.DateTimeFormat('en-GB', {
    hour: '2-digit', minute: '2-digit', timeZone: value?.brain?.tz || undefined,
  }).format(new Date(at)) : null;
  return [captured, partial ? 'Filling in…' : !failure ? 'Refreshing…' : null, failure, age].filter(Boolean).join(' · ');
}

export function watchLive(key: string, state: (value: any) => void, status: (value: string) => void, notify = false) {
  let source: EventSource | undefined;
  let controller: AbortController | undefined;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let active = true, disposed = false, generation = 0, filling = false;
  const delay = () => filling ? 1_000 : 60_000;
  const deliver = (value: any, version: string | null) => {
    filling = !!value?.brain?.filling_in || value?.source?.kind === 'cached-full';
    state({ ...value, __ui_version: version });
  };
  const close = () => {
    generation++;
    controller?.abort();
    controller = undefined;
    source?.close();
    source = undefined;
    clearTimeout(pollTimer);
  };
  const read = async () => {
    if (disposed || (document.hidden && !notify)) return;
    const current = ++generation;
    controller?.abort();
    controller = new AbortController();
    try {
      const response = await fetch('/api/state?k=' + encodeURIComponent(key), { signal: controller.signal });
      if (current !== generation || disposed || (document.hidden && !notify)) return;
      if (response.status === 403) { status('Permission required'); return; }
      if (!response.ok) throw Error('state unavailable');
      const value = await response.json();
      if (current !== generation || disposed || (document.hidden && !notify)) return;
      deliver(value, response.headers.get('X-Brain-UI-Version'));
      status(active ? 'Live' : 'Updates each minute');
      if (active) {
        source?.close();
        source = new EventSource('/api/events?k=' + encodeURIComponent(key));
        source.onmessage = event => {
          if (!active || current !== generation || disposed || (document.hidden && !notify)) return;
          try { deliver(JSON.parse(event.data), event.lastEventId || null); status('Live'); }
          catch { status('Updates interrupted'); }
        };
        source.onerror = () => { if (active && current === generation) status('Updates interrupted'); };
      }
    } catch (error) {
      if (current === generation && !disposed && (error as Error).name !== 'AbortError') status('Connection failed');
    } finally {
      if (current === generation) {
        controller = undefined;
        if (!disposed && !active && (!document.hidden || notify))
          pollTimer = setTimeout(read, delay());
      }
    }
  };
  const idle = () => {
    active = false;
    // Finish a pending snapshot even when its response takes longer than the
    // active window. Its finally block schedules the next idle poll.
    source?.close();
    source = undefined;
    clearTimeout(pollTimer);
    status('Updates each minute');
    if (!controller && (!document.hidden || notify)) pollTimer = setTimeout(read, delay());
  };
  const activity = () => {
    if (disposed || document.hidden) return;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(idle, 5_000);
    if (!active) { active = true; close(); void read(); }
  };
  const visibility = () => {
    clearTimeout(idleTimer);
    close();
    active = false;
    if (!document.hidden) activity();
    else if (notify) pollTimer = setTimeout(read, 60_000);
  };
  document.addEventListener('visibilitychange', visibility);
  document.addEventListener('pointerdown', activity, { passive: true });
  document.addEventListener('keydown', activity);
  document.addEventListener('wheel', activity, { passive: true });
  if (document.hidden) { active = false; if (notify) pollTimer = setTimeout(read, 60_000); }
  else { activity(); void read(); }
  return () => {
    disposed = true;
    clearTimeout(idleTimer);
    close();
    document.removeEventListener('visibilitychange', visibility);
    document.removeEventListener('pointerdown', activity);
    document.removeEventListener('keydown', activity);
    document.removeEventListener('wheel', activity);
  };
}
