import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Minus, Plus, Maximize2, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group";
import { Empty, Text, Status } from "./shared";
import { useSearch } from "./search";
import { Highlight } from "./picker";
import { list, count, nowOf, within, threads, learningRows, time, type RecordData } from "./model";
import type { Route } from "./routes";
import "./graph.css";

type Go = (r: Partial<Route> & { view: string; state?: string | null }) => void;
import { layout, placeLabels, labelBox, type GNode, type GEdge, type LabelMetrics } from "./graph-layout";
export { layout };

// The brain as a graph: projects, specialists and learnings, linked by parent, home, hire and source.
export function graphData(s: RecordData) {
  const now = nowOf(s), nodes: GNode[] = [], edges: GEdge[] = [], at = new Map<string, number>();
  const latest = (values: unknown[]) => {
    const stamps = values.map((value) => typeof value === "number" ? value : Date.parse(String(value || "")) / 1000).filter(Number.isFinite);
    return stamps.length ? Math.max(...stamps) : null;
  };
  const project = (id: string) => list(s.nodes).find((n) => n.id === id)?.title || id || "Not recorded";
  const add = (kind: GNode["kind"], id: string, title: string, r: number, recent: boolean, home: string, activity: number | null) => {
    at.set(kind + ":" + id, nodes.length);
    nodes.push({ key: kind + ":" + id, kind, id, title, r, recent, project: home, lastActivity: activity, x: 0, y: 0, vx: 0, vy: 0 });
  };
  const open = threads(list(s.nodes));
  for (const n of list(s.nodes).filter((n) => !n.archived)) {
    const recent = ((s.activity?.counts?.[n.id] || []) as number[]).slice(-24).some(Boolean) || open.some((f) => f.node.id === n.id && f.wt?.wrote && now / 1000 - f.wt.wrote < 3600);
    add("node", n.id, n.title || n.id, 9 + Math.min(9, list(n.jobs?.rows).length / 4), recent, project(n.id), latest([n.updated, ...list(s.commits).filter((c) => c.node === n.id).map((c) => c.t), ...list(n.worktrees).map((w) => w.wrote)]));
  }
  // "Recent" means the last day for every kind, so the glow stays a signal, not a background.
  for (const sp of list(s.specialists)) add("specialist", `${sp.node}/${sp.id}`, sp.title || sp.id, 6 + Math.min(4, list(sp.jobs).length), list(sp.jobs).some((j) => within(j.time, now, 24)), project(sp.node), latest([sp.updated, ...list(sp.jobs).map((j) => j.time), ...list(sp.lessons).map((l) => l.time)]));
  for (const i of learningRows(s).filter((r) => !r.specialist)) add("learning", i.id, i.title || i.id, 3.5, within(i.provenance?.time, now, 24), project(i.node), latest([i.provenance?.time]));
  const link = (a?: number, b?: number, kind = "") => { if (a != null && b != null && a !== b && !edges.some((e) => (e.a === a && e.b === b) || (e.a === b && e.b === a))) edges.push({ a, b, kind }); };
  for (const n of list(s.nodes)) if (n.parent) link(at.get("node:" + n.id), at.get("node:" + n.parent), "parent");
  for (const sp of list(s.specialists)) {
    const me = at.get(`specialist:${sp.node}/${sp.id}`);
    link(me, at.get("node:" + sp.node), "home");
    for (const j of list(sp.jobs)) link(me, at.get("node:" + j.node), "hire");
    for (const l of list(sp.lessons)) link(me, at.get("node:" + l.from), "lesson");
  }
  for (const i of list(s.items?.all))
    for (const n of new Set([i.provenance?.node || i.source, ...list(i.provenance?.nodes)].filter(Boolean)))
      link(at.get("learning:" + i.id), at.get("node:" + n), "source");
  return { nodes, edges };
}

const short = (t: string) => (t.length > 28 ? t.slice(0, 27) + "…" : t);
const PAN: Record<string, [number, number]> = { ArrowLeft: [30, 0], ArrowRight: [-30, 0], ArrowUp: [0, 30], ArrowDown: [0, -30] };

export function Graph({ s, go }: { s: RecordData; go: Go }) {
  const svg = useRef<SVGSVGElement>(null);
  const [metrics, setMetrics] = useState<Map<string, LabelMetrics>>(() => new Map());
  const graph = useMemo(() => graphData(s), [s.nodes, s.specialists, s.items, s.activity, s.commits]); // eslint-disable-line react-hooks/exhaustive-deps
  // Activity and machine readings may refresh without changing graph structure. Run the force
  // solver only for topology or node-size changes, retaining the operator's settled map.
  const topology = JSON.stringify([graph.nodes.map((p) => [p.key, p.r]), graph.edges]);
  const positions = useMemo(() => {
    const points = graph.nodes.map((p) => ({ ...p }));
    layout(points, graph.edges);
    return points.map((p) => ({ x: p.x, y: p.y }));
  }, [topology]); // eslint-disable-line react-hooks/exhaustive-deps
  const nodes = graph.nodes.map((p, i) => ({ ...p, ...positions[i], labelMetrics: metrics.get(p.title) })), edges = graph.edges;
  const [query, setQuery] = useState(""), [selected, setSelected] = useState<string | null>(null), [hover, setHover] = useState<string | null>(null), [focused, setFocused] = useState<string | null>(null);
  const tooltipId = useId();
  const [kinds, setKinds] = useState({ node: true, specialist: true, learning: true });
  const [view, setView] = useState({ k: 1, x: 0, y: 0 });
  const found = useSearch(s, "all", query, query.trim().length > 0);
  const matched = new Set(query.trim() ? found.items.map((f) => f.kind + ":" + f.id) : []);
  const visible = nodes.filter((p) => kinds[p.kind]);
  const shown = new Set(visible.map((p) => p.key));
  const degrees = new Map(nodes.map((p) => [p.key, 0]));
  for (const edge of edges) { for (const index of [edge.a, edge.b]) degrees.set(nodes[index].key, (degrees.get(nodes[index].key) || 0) + 1); }
  const permanent = new Set(visible.filter((p) => p.kind === "node").map((p) => p.key));
  for (const kind of ["specialist", "learning"]) {
    visible.filter((p) => p.kind === kind).sort((a, b) => (degrees.get(b.key) || 0) - (degrees.get(a.key) || 0) || a.key.localeCompare(b.key)).slice(0, 3).forEach((p) => permanent.add(p.key));
  }
  const labels = placeLabels(visible, new Set([...permanent, ...matched, ...(selected ? [selected] : []), ...(hover ? [hover] : []), ...(focused ? [focused] : [])]), permanent);
  const pad = 40, xs = visible.map((p) => p.x), ys = visible.map((p) => p.y);
  for (const p of visible) { const at = labels.get(p.key); if (!at || !permanent.has(p.key)) continue; const bounds = labelBox(p, at); xs.push(bounds.x0, bounds.x1); ys.push(bounds.y0, bounds.y1); }
  // Never zoom a small graph in: labels stay at their reading size and nodes keep their scale.
  const raw = visible.length ? { x: Math.min(...xs) - pad, y: Math.min(...ys) - pad, w: Math.max(...xs) - Math.min(...xs) + pad * 2, h: Math.max(...ys) - Math.min(...ys) + pad * 2 + 20 } : { x: -100, y: -100, w: 200, h: 200 };
  const minW = 820, minH = 520, box = { x: raw.x - Math.max(0, minW - raw.w) / 2, y: raw.y - Math.max(0, minH - raw.h) / 2, w: Math.max(minW, raw.w), h: Math.max(minH, raw.h) };
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const focus = (key: string) => {
    const p = nodes.find((n) => n.key === key); if (!p) return;
    setSelected(key);
    const cx = box.x + box.w / 2, cy = box.y + box.h / 2;
    setView((v) => ({ ...v, x: (cx - p.x) * v.k, y: (cy - p.y) * v.k }));
  };
  const chosen = nodes.find((n) => n.key === selected);
  const open = (p: GNode) => p.kind === "node" ? go({ view: "project", node: p.id }) : p.kind === "specialist" ? go({ view: "specialists", item: p.id }) : go({ view: "learnings", item: p.id });
  const tip = nodes.find((n) => n.key === (hover || focused));
  const viewRef = useRef(view);
  viewRef.current = view;
  const hasNodes = visible.length > 0;
  const titles = JSON.stringify(graph.nodes.map((p) => p.title));
  useLayoutEffect(() => {
    const element = svg.current;
    if (!element) return;
    let active = true;
    const measure = () => {
      if (!active) return;
      const context = document.createElement("canvas").getContext("2d");
      if (!context) return;
      const text = element.querySelector(".gnode text");
      const style = getComputedStyle(text || element);
      context.font = `${style.fontWeight} 11px ${style.fontFamily}`;
      const measured = new Map<string, LabelMetrics>();
      for (const title of JSON.parse(titles) as string[]) {
        const m = context.measureText(short(title));
        measured.set(title, {
          width: Math.max(m.width, m.actualBoundingBoxLeft + m.actualBoundingBoxRight),
          ascent: Math.max(m.fontBoundingBoxAscent || 0, m.actualBoundingBoxAscent, 11) + 2,
          descent: Math.max(m.fontBoundingBoxDescent || 0, m.actualBoundingBoxDescent, 3) + 2,
        });
      }
      setMetrics(measured);
    };
    measure();
    void document.fonts.ready.then(measure);
    return () => { active = false; };
  }, [titles, hasNodes]);
  useEffect(() => {
    const element = svg.current;
    if (!element) return;
    const clamp = (k: number) => Math.max(0.5, Math.min(4, k));
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      setView((v) => ({ ...v, k: clamp(v.k * Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * 0.005)) }));
    };
    let gestureScale: number | null = null;
    const gesture = (event: Event) => {
      event.preventDefault();
      if (event.type === "gesturestart") gestureScale = viewRef.current.k;
      else if (event.type === "gesturechange" && gestureScale != null) {
        const scale = (event as Event & { scale?: number }).scale;
        const base = gestureScale;
        if (scale != null && Number.isFinite(scale)) setView((v) => ({ ...v, k: clamp(base * scale) }));
      } else if (event.type === "gestureend") gestureScale = null;
    };
    // React wheel handlers may be passive. Pinch must be cancelled synchronously at the native
    // target, while ordinary wheel events remain available to the page scroll chain.
    element.addEventListener("wheel", wheel, { passive: false, capture: true });
    for (const type of ["gesturestart", "gesturechange", "gestureend"]) element.addEventListener(type, gesture, { passive: false, capture: true });
    return () => {
      element.removeEventListener("wheel", wheel, true);
      for (const type of ["gesturestart", "gesturechange", "gestureend"]) element.removeEventListener(type, gesture, true);
    };
  }, [hasNodes]);
  const counts = { node: nodes.filter((n) => n.kind === "node").length, specialist: nodes.filter((n) => n.kind === "specialist").length, learning: nodes.filter((n) => n.kind === "learning").length };
  const names = { node: "Projects", specialist: "Specialists", learning: "Learnings" } as const;
  return (
    <section className="surface fill-card graph-layout" aria-label="Brain graph">
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
            <svg ref={svg} viewBox={`${box.x} ${box.y} ${box.w} ${box.h}`} role="group" tabIndex={0} data-zoom={view.k} aria-describedby="graph-keyboard-help" aria-label={`Graph of ${counts.node} projects, ${counts.specialist} specialists and ${counts.learning} learnings${query ? `; ${matched.size} match “${query}”` : ""}`}
              className={query.trim() ? "searching" : ""}
              onPointerDown={(e: ReactPointerEvent<SVGSVGElement>) => { if ((e.target as Element).closest(".gnode")) return; drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }; }}
              onPointerMove={(e) => { if (!drag.current || !svg.current) return; const scale = box.w / svg.current.clientWidth; setView((v) => ({ ...v, x: drag.current!.vx + (e.clientX - drag.current!.x) * scale, y: drag.current!.vy + (e.clientY - drag.current!.y) * scale })); }}
              onPointerUp={() => (drag.current = null)} onPointerLeave={() => (drag.current = null)}
              onKeyDown={(e) => {
                if (e.key === "Escape") { setHover(null); setFocused(null); return; }
                const pan = PAN[e.key];
                if (pan) { e.preventDefault(); setView((v) => ({ ...v, x: v.x + pan[0], y: v.y + pan[1] })); }
                else if (["+", "=", "-", "Home"].includes(e.key)) { e.preventDefault(); setView((v) => e.key === "Home" ? { k: 1, x: 0, y: 0 } : { ...v, k: Math.max(0.5, Math.min(4, v.k * (e.key === "-" ? 0.8 : 1.25))) }); }
              }}>
              <defs>
                <filter id="glow" x="-100%" y="-100%" width="300%" height="300%"><feGaussianBlur stdDeviation="4" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge></filter>
              </defs>
              <g data-graph-viewport transform={`translate(${box.x + box.w / 2 + view.x} ${box.y + box.h / 2 + view.y}) scale(${view.k}) translate(${-(box.x + box.w / 2)} ${-(box.y + box.h / 2)})`}>
                {edges.filter((e) => shown.has(nodes[e.a].key) && shown.has(nodes[e.b].key)).map((e, i) => {
                  const a = nodes[e.a], b = nodes[e.b], lit = matched.has(a.key) || matched.has(b.key) || selected === a.key || selected === b.key;
                  return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={"edge edge-" + e.kind + (lit ? " lit" : "")} />;
                })}
                {visible.map((p) => {
                  const hit = matched.has(p.key), at = labels.get(p.key);
                  return (
                    <g key={p.key} className={`gnode kind-${p.kind}${hit ? " match" : ""}${p.recent ? " recent" : ""}${selected === p.key ? " selected" : ""}`} transform={`translate(${p.x} ${p.y})`}
                      role="button" tabIndex={0} data-graph-key={p.key} data-links={degrees.get(p.key) || 0} aria-label={`Open ${p.kind === "node" ? "project" : p.kind}: ${p.title}`} aria-describedby={tip?.key === p.key ? tooltipId : undefined}
                      onFocus={() => setFocused(p.key)} onBlur={() => setFocused(null)}
                      onPointerEnter={() => setHover(p.key)} onPointerLeave={() => setHover(null)} onClick={() => open(p)}
                      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); open(p); } }}>
                      {(hit || p.recent) && <circle r={p.r + 5} className="halo" filter="url(#glow)" />}
                      <circle r={p.r} className="dot" />
                      {at && <>
                        {Math.hypot(at.x - p.x, at.y - p.y) > p.r + 30 && <line className="graph-label-leader" x1={0} y1={0} x2={at.x - p.x} y2={at.y - p.y - 6} />}
                        <text x={at.x - p.x} y={at.y - p.y} textAnchor={at.anchor}>{short(p.title)}</text>
                      </>}
                    </g>
                  );
                })}
              </g>
            </svg>
          ) : <Empty text="Nothing to draw yet." action="Projects, specialists and learnings appear here as your brain grows." />}
          {tip && <div role="tooltip" id={tooltipId} className="graph-tooltip">
            <small>{tip.kind === "node" ? "Project" : tip.kind === "specialist" ? "Specialist" : "Learning"}</small>
            <strong data-graph-name>{tip.title}</strong>
            <span data-graph-project>Project: {tip.project || "Not recorded"}</span>
            <span>Last activity: {time(tip.lastActivity, s.brain?.tz)}</span>
            <span>{count(degrees.get(tip.key) || 0, "link")}</span>
          </div>}
          <p id="graph-keyboard-help" className="sr-only">Use arrow keys to pan, plus and minus to zoom, Home to fit. Tab to a record and press Enter to open it.</p>
          <div className="graph-legend"><span><i className="halo-swatch" />Glows: a search match, or activity in the last day (commits, hires, new learnings)</span></div>
        </div>
        <aside className="graph-side" aria-label={query.trim() ? "Matches" : "Selected"}>
          {chosen && <div className="graph-chosen">
            <small>{names[chosen.kind].slice(0, -1)}</small>
            <strong><Text>{chosen.title}</Text></strong>
            <small>{edges.filter((e) => nodes[e.a].key === chosen.key || nodes[e.b].key === chosen.key).length} links{chosen.recent ? " · recently active" : ""}</small>
            <Button size="sm" variant="outline" onClick={() => open(chosen)}>{chosen.kind === "node" ? "Open project" : chosen.kind === "specialist" ? "Open specialists" : "Open learnings"}</Button>
          </div>}
          {query.trim() ? <>
            <p className="results-meta" role="status">{found.state === "loading" ? "Searching…" : ((n) => `${n} ${n === 1 ? "match" : "matches"}`)(found.items.filter((f) => f.kind !== "job").length)}</p>
            <div className="rows">{found.items.filter((f) => f.kind !== "job").slice(0, 12).map((f) => (
              <Button variant="ghost" className="row match-row" data-item key={f.kind + f.id} onClick={() => focus(f.kind + ":" + f.id)}>
                <span className={"swatch kind-" + f.kind} />
                <span className="row-main"><strong className="row-title"><Highlight text={f.title} query={query} /></strong><small>{f.secondary}</small></span>
              </Button>
            ))}</div>
          </> : !chosen && <>
            <div className="graph-hint">
              <p>Search to light up matching records. Select a project to see its links.</p>
              <Status>{`${nodes.filter((n) => n.recent).length} recently active`}</Status>
            </div>
            {/* The list view beside the map: every project, by how much it links. */}
            <div className="rows">{nodes.filter((n) => n.kind === "node").map((p) => ({ p, links: edges.filter((e) => nodes[e.a].key === p.key || nodes[e.b].key === p.key).length }))
              .sort((a, b) => b.links - a.links).slice(0, 12).map(({ p, links }) => (
                <Button variant="ghost" className="row match-row" data-item key={p.key} onClick={() => focus(p.key)} onPointerEnter={() => setHover(p.key)} onPointerLeave={() => setHover(null)}>
                  <span className={"swatch kind-node" + (p.recent ? " recent" : "")} />
                  <span className="row-main"><strong className="row-title">{p.title}</strong><small>{count(links, "link")}{p.recent ? " · active today" : ""}</small></span>
                </Button>))}</div>
          </>}
        </aside>
      </div>
    </section>
  );
}
