import { useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Minus, Plus, Maximize2, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Empty, Text, Status } from "./shared";
import { useSearch } from "./search";
import { Highlight } from "./picker";
import { list, nowOf, within, threads, learningRows, type RecordData } from "./model";
import type { Route } from "./routes";

type Go = (r: Partial<Route> & { view: string; state?: string | null }) => void;
import { layout, placeLabels, type GNode, type GEdge } from "./graph-layout";
export { layout };

// The brain as a graph: projects, specialists and learnings, linked by parent, home, hire and source.
export function graphData(s: RecordData) {
  const now = nowOf(s), nodes: GNode[] = [], edges: GEdge[] = [], at = new Map<string, number>();
  const add = (kind: GNode["kind"], id: string, title: string, r: number, recent: boolean) => {
    at.set(kind + ":" + id, nodes.length);
    nodes.push({ key: kind + ":" + id, kind, id, title, r, recent, x: 0, y: 0, vx: 0, vy: 0 });
  };
  const open = threads(list(s.nodes));
  for (const n of list(s.nodes).filter((n) => !n.archived)) {
    const recent = ((s.activity?.counts?.[n.id] || []) as number[]).slice(-24).some(Boolean) || open.some((f) => f.node.id === n.id && f.wt?.wrote && now / 1000 - f.wt.wrote < 3600);
    add("node", n.id, n.title || n.id, 9 + Math.min(9, list(n.jobs?.rows).length / 4), recent);
  }
  for (const sp of list(s.specialists)) add("specialist", `${sp.node}/${sp.id}`, sp.title || sp.id, 6 + Math.min(4, list(sp.jobs).length), list(sp.jobs).some((j) => within(j.time, now, 168)));
  for (const i of learningRows(s).filter((r) => !r.specialist)) add("learning", i.id, i.title || i.id, 4, i.state === "proposed" || within(i.provenance?.time, now, 168));
  const link = (a?: number, b?: number, kind = "") => { if (a != null && b != null && a !== b && !edges.some((e) => (e.a === a && e.b === b) || (e.a === b && e.b === a))) edges.push({ a, b, kind }); };
  for (const n of list(s.nodes)) if (n.parent) link(at.get("node:" + n.id), at.get("node:" + n.parent), "parent");
  for (const sp of list(s.specialists)) {
    const me = at.get(`specialist:${sp.node}/${sp.id}`);
    link(me, at.get("node:" + sp.node), "home");
    for (const j of list(sp.jobs)) link(me, at.get("node:" + j.node), "hire");
    for (const l of list(sp.lessons)) link(me, at.get("node:" + l.from), "lesson");
  }
  for (const i of list(s.items?.all)) link(at.get("learning:" + i.id), at.get("node:" + (i.provenance?.node || i.source)), "source");
  return { nodes, edges };
}

const short = (t: string) => (t.length > 28 ? t.slice(0, 27) + "…" : t);

export function Graph({ s, go }: { s: RecordData; go: Go }) {
  const { nodes, edges } = useMemo(() => { const g = graphData(s); layout(g.nodes, g.edges); return g; }, [s.nodes, s.specialists, s.items]); // eslint-disable-line react-hooks/exhaustive-deps
  const [query, setQuery] = useState(""), [selected, setSelected] = useState<string | null>(null), [hover, setHover] = useState<string | null>(null);
  const [kinds, setKinds] = useState({ node: true, specialist: true, learning: true });
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const found = useSearch(s, "all", query, query.trim().length > 0);
  const matched = new Set(query.trim() ? found.items.map((f) => f.kind + ":" + f.id) : []);
  const visible = nodes.filter((p) => kinds[p.kind]);
  const shown = new Set(visible.map((p) => p.key));
  const labels = placeLabels(visible, new Set([...matched, ...(selected ? [selected] : []), ...(hover ? [hover] : [])]));
  const pad = 40, xs = visible.map((p) => p.x), ys = visible.map((p) => p.y);
  // Never zoom a small graph in: labels stay at their reading size and nodes keep their scale.
  const raw = visible.length ? { x: Math.min(...xs) - pad, y: Math.min(...ys) - pad, w: Math.max(...xs) - Math.min(...xs) + pad * 2, h: Math.max(...ys) - Math.min(...ys) + pad * 2 + 20 } : { x: -100, y: -100, w: 200, h: 200 };
  const minW = 820, minH = 520, box = { x: raw.x - Math.max(0, minW - raw.w) / 2, y: raw.y - Math.max(0, minH - raw.h) / 2, w: Math.max(minW, raw.w), h: Math.max(minH, raw.h) };
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null), svg = useRef<SVGSVGElement>(null);
  const focus = (key: string) => {
    const p = nodes.find((n) => n.key === key); if (!p) return;
    setSelected(key);
    const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
    setView((v) => ({ ...v, x: (cx - p.x) * v.k, y: (cy - p.y) * v.k }));
  };
  const chosen = nodes.find((n) => n.key === selected);
  const open = (p: GNode) => p.kind === "node" ? go({ view: "project", node: p.id }) : p.kind === "specialist" ? go({ view: "specialists" }) : go({ view: "learnings" });
  const counts = { node: nodes.filter((n) => n.kind === "node").length, specialist: nodes.filter((n) => n.kind === "specialist").length, learning: nodes.filter((n) => n.kind === "learning").length };
  const names = { node: "Projects", specialist: "Specialists", learning: "Learnings" } as const;
  return (
    <section className="surface graph-layout" aria-label="Brain graph">
      <div className="graph-tools">
        <InputGroup className="graph-search">
          <InputGroupAddon><Search size={16} /></InputGroupAddon>
          <InputGroupInput aria-label="Search the graph" placeholder="Search projects, specialists, learnings" value={query} onChange={(e) => setQuery(e.target.value)} />
        </InputGroup>
        <div className="graph-kinds" role="group" aria-label="Show">
          {(Object.keys(names) as (keyof typeof names)[]).map((k) => (
            <Button key={k} variant={kinds[k] ? "secondary" : "ghost"} size="sm" aria-pressed={kinds[k]} onClick={() => setKinds({ ...kinds, [k]: !kinds[k] })}>
              <i className={"swatch kind-" + k} />{names[k]} <span className="tab-count">{counts[k]}</span>
            </Button>
          ))}
        </div>
        <div className="graph-zoom">
          <Button variant="ghost" size="icon-sm" aria-label="Zoom in" onClick={() => setView((v) => ({ ...v, k: Math.min(4, v.k * 1.25) }))}><Plus size={15} /></Button>
          <Button variant="ghost" size="icon-sm" aria-label="Zoom out" onClick={() => setView((v) => ({ ...v, k: Math.max(0.5, v.k / 1.25) }))}><Minus size={15} /></Button>
          <Button variant="ghost" size="icon-sm" aria-label="Fit graph" onClick={() => setView({ k: 1, x: 0, y: 0 })}><Maximize2 size={15} /></Button>
        </div>
      </div>
      <div className="graph-body">
        <div className="graph-canvas">
          {visible.length ? (
            <svg ref={svg} viewBox={`${box.x} ${box.y} ${box.w} ${box.h}`} role="img" aria-label={`Graph of ${counts.node} projects, ${counts.specialist} specialists and ${counts.learning} learnings${query ? `; ${matched.size} match “${query}”` : ""}`}
              className={query.trim() ? "searching" : ""}
              onPointerDown={(e: ReactPointerEvent<SVGSVGElement>) => { drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }; }}
              onPointerMove={(e) => { if (!drag.current || !svg.current) return; const scale = box.w / svg.current.clientWidth; setView((v) => ({ ...v, x: drag.current!.vx + (e.clientX - drag.current!.x) * scale, y: drag.current!.vy + (e.clientY - drag.current!.y) * scale })); }}
              onPointerUp={() => (drag.current = null)} onPointerLeave={() => (drag.current = null)}
              onWheel={(e) => { if (!e.ctrlKey && !e.metaKey) return; e.preventDefault(); setView((v) => ({ ...v, k: Math.max(0.5, Math.min(4, v.k * (e.deltaY < 0 ? 1.1 : 0.9))) })); }}>
              <defs>
                <filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="4" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
              </defs>
              <g transform={`translate(${box.x + box.w / 2 + view.x} ${box.y + box.h / 2 + view.y}) scale(${view.k}) translate(${-(box.x + box.w / 2)} ${-(box.y + box.h / 2)})`}>
                {edges.filter((e) => shown.has(nodes[e.a].key) && shown.has(nodes[e.b].key)).map((e, i) => {
                  const a = nodes[e.a], b = nodes[e.b], lit = matched.has(a.key) || matched.has(b.key) || selected === a.key || selected === b.key;
                  return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={"edge edge-" + e.kind + (lit ? " lit" : "")} />;
                })}
                {visible.map((p) => {
                  const hit = matched.has(p.key);
                  return (
                    <g key={p.key} className={`gnode kind-${p.kind}${hit ? " match" : ""}${p.recent ? " recent" : ""}${selected === p.key ? " selected" : ""}`} transform={`translate(${p.x} ${p.y})`}
                      onPointerEnter={() => setHover(p.key)} onPointerLeave={() => setHover(null)} onClick={() => focus(p.key)}>
                      {(hit || p.recent) && <circle r={p.r + 5} className="halo" filter="url(#glow)" />}
                      <circle r={p.r} className="dot" />
                      {labels.has(p.key) && <text y={p.r + 13} textAnchor="middle">{short(p.title)}</text>}
                    </g>
                  );
                })}
              </g>
            </svg>
          ) : <Empty text="Nothing to draw yet." action="Projects, specialists and learnings appear here as your brain grows." />}
          <div className="graph-legend"><span><i className="halo-swatch" />Glows: a search match or activity in the last day (projects) or week (hires, learnings)</span></div>
        </div>
        <aside className="graph-side" aria-label={query.trim() ? "Matches" : "Selected"}>
          {chosen && <div className="graph-chosen">
            <small>{names[chosen.kind].slice(0, -1)}</small>
            <strong><Text>{chosen.title}</Text></strong>
            <small>{edges.filter((e) => nodes[e.a].key === chosen.key || nodes[e.b].key === chosen.key).length} links{chosen.recent ? " · recently active" : ""}</small>
            <Button size="sm" variant="outline" onClick={() => open(chosen)}>{chosen.kind === "node" ? "Open project" : chosen.kind === "specialist" ? "Open specialists" : "Open learnings"}</Button>
          </div>}
          {query.trim() ? <>
            <p className="results-meta" role="status">{found.state === "loading" ? "Searching…" : `${found.total} ${found.total === 1 ? "match" : "matches"}`}</p>
            <div className="rows">{found.items.filter((f) => f.kind !== "job").slice(0, 12).map((f) => (
              <Button variant="ghost" className="row match-row" data-item key={f.kind + f.id} onClick={() => focus(f.kind + ":" + f.id)}>
                <span className={"swatch kind-" + f.kind} />
                <span className="row-main"><strong className="row-title"><Highlight text={f.title} query={query} /></strong><small>{f.secondary}</small></span>
              </Button>
            ))}</div>
          </> : !chosen && <div className="graph-hint">
            <p>Search to light up matching records. Select a node to see its links.</p>
            <Status>{`${nodes.filter((n) => n.recent).length} recently active`}</Status>
          </div>}
        </aside>
      </div>
    </section>
  );
}
