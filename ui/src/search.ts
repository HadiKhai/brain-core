import { useEffect, useRef, useState } from "react";
import type { RecordData } from "./model";
import { localSearch, type Found, type Page } from "./rank";
export { fold, rank, localSearch, type Found, type Page } from "./rank";

// Reference pickers search on the server (`/api/search`). A saved snapshot has no server, so it
// ranks the same records locally with the same rules (bin/brain search_rank / search_records).

const key = () => new URLSearchParams(location.search).get("k");
export async function search(s: RecordData, kind: string, query: string, offset = 0, limit = 20, signal?: AbortSignal): Promise<Page> {
  if (window.__STATE__ || !key()) return localSearch(s, kind, query, offset, limit);
  const params = new URLSearchParams({ k: key()!, kind, q: query, offset: String(offset), limit: String(limit) });
  const r = await fetch("/api/search?" + params, { signal });
  if (!r.ok) throw new Error("Search failed");
  return r.json();
}

// Debounced, paginated, stale-safe: a slower earlier response can never replace a newer one.
export function useSearch(s: RecordData, kind: string, query: string, enabled: boolean) {
  const [items, setItems] = useState<Found[]>([]),
    [next, setNext] = useState<number | null>(null),
    [total, setTotal] = useState(0),
    [state, setState] = useState<"idle" | "loading" | "more" | "error">("idle"),
    [retry, setRetry] = useState(0);
  const latest = useRef(0), sRef = useRef(s);
  sRef.current = s;
  useEffect(() => {
    if (!enabled) return;
    const id = ++latest.current, controller = new AbortController();
    setState("loading");
    const t = setTimeout(() => {
      search(sRef.current, kind, query, 0, 20, controller.signal)
        .then((page) => {
          if (id !== latest.current) return;
          setItems(page.items); setNext(page.next); setTotal(page.total); setState("idle");
        })
        .catch((e) => { if (id === latest.current && e.name !== "AbortError") setState("error"); });
    }, query ? 250 : 0);
    return () => { clearTimeout(t); controller.abort(); };
  }, [kind, query, enabled, retry]);
  const more = () => {
    if (next == null || state !== "idle") return;
    const id = latest.current;
    setState("more");
    search(sRef.current, kind, query, next, 20)
      .then((page) => {
        if (id !== latest.current) return;
        setItems((old) => [...old, ...page.items]); setNext(page.next); setTotal(page.total); setState("idle");
      })
      .catch(() => { if (id === latest.current) setState("error"); });
  };
  return { items, total, next, state, more, retry: () => setRetry((r) => r + 1) };
}
