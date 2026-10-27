import { useId, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { RecordData } from "./model";
import { Empty } from "./shared";
import { activeTask, connectedActiveTasks, downstream, taskColumn, taskGraph, taskKey, taskLayout, taskTitle, taskTone, type TaskGraph } from "./task-model";

export function TaskCanvas({ rows, graph, onOpen }: { rows: RecordData[]; graph: TaskGraph; onOpen: (t: RecordData) => void }) {
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [selected, setSelected] = useState(""), [hover, setHover] = useState("");
  const [showAll, setShowAll] = useState(false);
  const svg = useRef<SVGSVGElement>(null);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const arrow = useId(), help = useId();
  const eligible = useMemo(() => showAll ? rows : rows.filter(activeTask), [rows, showAll]);
  const all = useMemo(() => taskGraph(eligible), [eligible]);
  const linked = useMemo(() => showAll ? eligible.filter(t => all.incoming.get(taskKey(t))!.size || all.outgoing.get(taskKey(t))!.size) : connectedActiveTasks(rows), [eligible, all, showAll, rows]);
  const unlinked = useMemo(() => showAll ? eligible.filter(t => !all.incoming.get(taskKey(t))!.size && !all.outgoing.get(taskKey(t))!.size) : [], [eligible, all, showAll]);
  const visible = useMemo(() => taskGraph(linked), [linked]);
  const layout = useMemo(() => taskLayout(visible), [visible]);
  const active = hover || (visible.byKey.has(selected) ? selected : "");
  const unlocked = useMemo(() => downstream(graph, active), [graph, active]);
  const chosen = graph.byKey.get(active);
  // Keep world units at reading scale. A large snapshot must still be readable after
  // zooming in; sizing the viewBox to the entire graph makes that impossible.
  const width = 1000, height = 600;
  const fitScale = Math.min(1, width / layout.width, height / layout.height);
  const fit = () => setView({ x: (width - layout.width * fitScale) / 2, y: (height - layout.height * fitScale) / 2, k: fitScale });
  const zoom = (factor: number) => setView(v => {
    const k = Math.max(fitScale / 2, Math.min(3, v.k * factor)), ratio = k / v.k;
    return { k, x: width / 2 - (width / 2 - v.x) * ratio, y: height / 2 - (height / 2 - v.y) * ratio };
  });
  return <div className="task-canvas-shell">
    <div className="task-canvas-tools">
      <p>{linked.length} connected {showAll ? "" : "active "}tasks · Dependency → dependent <span>· Ready now has a green border</span></p>
      <div role="group" aria-label="Canvas controls">
        <Button variant={showAll ? "secondary" : "outline"} size="sm" aria-pressed={showAll} onClick={() => { setShowAll(v => !v); setView({ x: 0, y: 0, k: 1 }); }}>Show all {rows.length} tasks</Button>
        {linked.length > 0 && <>
        <Button variant="outline" size="sm" onClick={() => zoom(1.25)} aria-label="Zoom in tasks">+</Button>
        <Button variant="outline" size="sm" onClick={() => zoom(0.8)} aria-label="Zoom out tasks">−</Button>
        <Button variant="outline" size="sm" onClick={fit}>Fit tasks</Button>
        <Button variant="outline" size="sm" onClick={() => setView({ x: 0, y: 0, k: 1 })}>Reset view</Button>
        </>}
      </div>
    </div>
    <div className="task-status-legend" aria-label="Task status legend">{["backlog", "ready", "in-progress", "blocked", "parked", "done", "unproven", ...(showAll ? ["dropped"] : [])].map(tone => <span key={tone}><i data-tone={tone} />{tone === "in-progress" ? "In progress" : tone[0].toUpperCase() + tone.slice(1)}</span>)}</div>
    {!linked.length ? <Empty text={showAll ? "No dependency links in these tasks." : "No active dependency links."} action={`${rows.length} tasks match these filters. Link dependencies to see unlock chains here; use the board to see their workflow.`} /> : <>
    <svg ref={svg} className="task-canvas" viewBox={`0 0 ${width} ${height}`} role="group" aria-label="Task dependency canvas" aria-describedby={help} tabIndex={0} data-zoom={view.k}
      onPointerDown={e => {
        if ((e.target as Element).closest(".task-graph-node")) return;
        drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={e => {
        const start = drag.current, matrix = svg.current?.getScreenCTM();
        if (!start || !matrix) return;
        const a = new DOMPoint(start.x, start.y).matrixTransform(matrix.inverse());
        const b = new DOMPoint(e.clientX, e.clientY).matrixTransform(matrix.inverse());
        setView(v => ({ ...v, x: start.vx + b.x - a.x, y: start.vy + b.y - a.y }));
      }}
      onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}
      onKeyDown={e => {
        const moves: Record<string, [number, number]> = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] };
        if (moves[e.key]) { e.preventDefault(); const [x, y] = moves[e.key]; setView(v => ({ ...v, x: v.x + x, y: v.y + y })); }
        else if (["+", "=", "-", "Home"].includes(e.key)) { e.preventDefault(); if (e.key === "Home") fit(); else zoom(e.key === "-" ? 0.8 : 1.25); }
        else if (e.key === "Escape") { setSelected(""); setHover(""); }
      }}>
      <defs><marker id={arrow} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker></defs>
      <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
        {visible.edges.map(({ from, to }) => {
          const a = layout.positions.get(from)!, b = layout.positions.get(to)!;
          const lit = (from === active || unlocked.has(from)) && unlocked.has(to);
          return <path key={from + ">" + to} data-task-from={from} data-task-to={to} className={`task-graph-edge${lit ? " highlighted" : ""}`}
            d={`M ${a.x + 248} ${a.y + 44} C ${a.x + 274} ${a.y + 44}, ${b.x - 26} ${b.y + 44}, ${b.x - 3} ${b.y + 44}`} markerEnd={`url(#${arrow})`} />;
        })}
        {linked.map(t => {
          const key = taskKey(t), p = layout.positions.get(key)!;
          const activate = () => { setSelected(key); onOpen(t); };
          return <g key={key} transform={`translate(${p.x} ${p.y})`} role="button" tabIndex={0} aria-label={`Open task ${t.id}: ${taskTitle(t)}`} data-tone={taskTone(t)}
            data-task-key={key} data-ready={t.ready_now === true || undefined} data-downstream={unlocked.has(key) || undefined}
            className={`task-graph-node${active === key ? " selected" : ""}`}
            onPointerEnter={() => setHover(key)} onPointerLeave={() => setHover("")}
            onFocus={e => {
              setHover(key);
              if (e.currentTarget.matches(":focus-visible")) {
                setView(v => ({ ...v, x: width / 2 - (p.x + 124) * v.k, y: height / 2 - (p.y + 44) * v.k }));
              }
            }} onBlur={() => setHover("")} onClick={activate}
            onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); activate(); } }}>
            <title>{`${t.id} · ${taskTitle(t)}\n${t.project} · ${taskTone(t) === "blocked" ? "Blocked" : taskColumn(t)}\n${t.ready_now ? "Ready now" : "Not ready to start"}`}</title>
            <rect width="248" height="88" rx="10" />
            <text x="12" y="20" className="task-graph-id">{String(t.id).slice(0, 29)} · {t.priority}</text>
            <foreignObject x="12" y="30" width="224" height="22"><div className="task-graph-title" dir="auto">{taskTitle(t)}</div></foreignObject>
            <text x="12" y="66" className="task-graph-state">{taskTone(t) === "blocked" ? "Blocked" : taskColumn(t)}{t.ready_now ? " · Ready now" : ""} · {String(t.project).slice(0, 20)}</text>
          </g>;
        })}
      </g>
    </svg>
    <div className="task-canvas-context" role="status">
      {chosen ? <><strong>{chosen.id}</strong> unlocks {unlocked.size} downstream tasks{unlocked.size ? `: ${[...unlocked].slice(0, 8).map(key => graph.byKey.get(key)?.id).join(", ")}${unlocked.size > 8 ? "…" : ""}` : ""}.</> : "Hover or focus a task to trace everything it unlocks. Click to read it."}
    </div>
    <p id={help} className="task-canvas-help">Drag the background to pan. Use + / − to zoom, arrow keys to pan and Home to fit. Tab to a task and press Enter to read it. Filters hide tasks and their edges; downstream counts include hidden tasks.</p>
    </>}
    {unlinked.length > 0 && <section className="task-unlinked" aria-label="Tasks without links"><h3>{unlinked.length} tasks without links</h3><p>These tasks stay in a list until dependencies are recorded.</p><ul>{unlinked.map(t => <li key={taskKey(t)}><Button variant="outline" size="sm" data-tone={taskTone(t)} onClick={() => onOpen(t)}><i className="task-status-dot" aria-hidden /><span>{t.id}: {taskTitle(t)}</span><small>{taskTone(t) === "blocked" ? "Blocked" : taskColumn(t)}</small></Button></li>)}</ul></section>}
  </div>;
}
