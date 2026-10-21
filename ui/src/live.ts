// A visible, active dashboard streams changes. Idle tabs check once a minute;
// hidden tabs make no requests and reconnect when they become visible again.
export function watchLive(key: string, state: (value: any) => void, status: (value: string) => void) {
  let source: EventSource | undefined;
  let controller: AbortController | undefined;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let active = true, disposed = false, generation = 0;
  const close = () => {
    generation++;
    controller?.abort();
    controller = undefined;
    source?.close();
    source = undefined;
    clearTimeout(pollTimer);
  };
  const read = async () => {
    if (disposed || document.hidden) return;
    const current = ++generation;
    controller?.abort();
    controller = new AbortController();
    try {
      const response = await fetch('/api/state?k=' + encodeURIComponent(key), { signal: controller.signal });
      if (current !== generation || disposed || document.hidden) return;
      if (response.status === 403) { status('Permission required'); return; }
      if (!response.ok) throw Error('state unavailable');
      const value = await response.json();
      if (current !== generation || disposed || document.hidden) return;
      state(value);
      status('Live');
      if (active) {
        source?.close();
        source = new EventSource('/api/events?k=' + encodeURIComponent(key));
        source.onmessage = event => {
          if (current !== generation || disposed || document.hidden) return;
          try { state(JSON.parse(event.data)); status('Live'); }
          catch { status('Updates interrupted'); }
        };
        source.onerror = () => { if (current === generation) status('Updates interrupted'); };
      }
    } catch (error) {
      if (current === generation && !disposed && (error as Error).name !== 'AbortError') status('Connection failed');
    } finally {
      if (!disposed && !active && !document.hidden && current === generation)
        pollTimer = setTimeout(read, 60_000);
    }
  };
  const idle = () => {
    active = false;
    close();
    if (!document.hidden) pollTimer = setTimeout(read, 60_000);
  };
  const activity = () => {
    if (disposed || document.hidden) return;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(idle, 30_000);
    if (!active) { active = true; close(); void read(); }
  };
  const visibility = () => {
    clearTimeout(idleTimer);
    close();
    active = false;
    if (!document.hidden) activity();
  };
  document.addEventListener('visibilitychange', visibility);
  document.addEventListener('pointerdown', activity, { passive: true });
  document.addEventListener('keydown', activity);
  document.addEventListener('wheel', activity, { passive: true });
  if (document.hidden) active = false;
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
