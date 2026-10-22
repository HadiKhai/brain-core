// Pure ranking shared by the picker's snapshot fallback; mirrors bin/brain search_fold/search_rank/ui_search.
type RecordData = Record<string, any>;
const list = (v: any): RecordData[] => (Array.isArray(v) ? v : []);
export type Found = { kind: string; id: string; title: string; secondary: string };
export type Page = { items: Found[]; total: number; offset: number; next: number | null };

export function fold(text: any) {
  return String(text ?? "")
    .normalize("NFKD")
    .replace(/[̀-ͯؐ-ًؚ-ٰٟۖ-ۭـ]/g, "")
    .replace(/[أإآٱ]/g, "ا")
    .replace(/ى/g, "ي")
    .replace(/ة/g, "ه")
    .replace(/ؤ/g, "و")
    .replace(/ئ/g, "ي")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
}
export function rank(name: string, query: string): [number, number] | null {
  const n = fold(name), q = fold(query);
  if (!q || n === q) return [0, 0];
  if (n.startsWith(q)) return [1, 0];
  const word = new RegExp("(?:^|(?<=[\\s\\-_/(·.,:]))" + q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).exec(n);
  if (word) return [2, word.index];
  const at = n.indexOf(q);
  return at >= 0 ? [3, at] : null;
}
export function records(s: RecordData) {
  const nodes = list(s.nodes), title = (id: string) => nodes.find((n) => n.id === id)?.title || "Workspace";
  const out: (Found & { recent: string; used: number })[] = [];
  for (const n of nodes.filter((n) => !n.archived))
    out.push({ kind: "node", id: n.id, title: n.title || n.id, secondary: (n.parent ? title(n.parent) + " · " : "Workspace root · ") + (n.updated_ago || "update time not recorded"), recent: n.updated || "", used: list(n.jobs?.rows).length });
  for (const sp of list(s.specialists))
    out.push({ kind: "specialist", id: `${sp.node}/${sp.id}`, title: sp.title || sp.id, secondary: [sp.runtime, title(sp.node), `${list(sp.jobs).length} hires`].filter(Boolean).join(" · "), recent: list(sp.jobs).map((j) => String(j.time || "")).sort().pop() || "", used: list(sp.jobs).length });
  for (const i of list(s.items?.all)) {
    const kind = String(i.kind || "Item");
    out.push({ kind: "learning", id: i.id, title: i.title || i.id, secondary: [kind[0].toUpperCase() + kind.slice(1).toLowerCase(), title(i.provenance?.node), i.status === "proposed" ? "proposed" : ""].filter(Boolean).join(" · "), recent: String(i.provenance?.time || ""), used: 0 });
  }
  for (const n of nodes)
    for (const r of list(n.jobs?.rows))
      out.push({ kind: "job", id: `${n.id}/${r.Job}/${r.Started}`, title: r.Job || "job", secondary: [n.title, r.Outcome || "open", r.Runtime].filter(Boolean).join(" · "), recent: String(r.Started || ""), used: 0 });
  return out;
}
export function localSearch(s: RecordData, kind: string, query: string, offset = 0, limit = 20): Page {
  let rows = records(s).filter((r) => kind === "all" || r.kind === kind);
  const cmp = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);
  if (query.trim()) {
    rows = rows
      .map((r) => ({ r, k: rank(r.title, query) }))
      .filter((x): x is { r: (typeof rows)[number]; k: [number, number] } => !!x.k)
      .sort((a, b) => a.k[0] - b.k[0] || a.k[1] - b.k[1] || cmp(fold(a.r.title), fold(b.r.title)) || cmp(a.r.id, b.r.id))
      .map((x) => x.r);
  } else rows.sort((a, b) => cmp(b.recent, a.recent) || b.used - a.used || cmp(fold(a.title), fold(b.title)));
  const items = rows.slice(offset, offset + limit).map(({ kind, id, title, secondary }) => ({ kind, id, title, secondary }));
  return { items, total: rows.length, offset, next: offset + limit < rows.length ? offset + limit : null };
}

