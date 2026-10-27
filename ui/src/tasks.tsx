import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { SectionTabs } from "./dash";
import { ReaderRow } from "./detail-panel";
import { Choice, Empty, Reader, Status, TechnicalDetails, Text } from "./shared";
import { label, list, safeHref, time, type RecordData } from "./model";
import type { Route } from "./routes";
import { TaskCanvas } from "./task-canvas";
import { dependencyKey, downstream, filterTasks, taskColumn, taskColumns, taskGraph, taskKey, taskTitle, taskTone, type TaskGraph } from "./task-model";
import "./tasks.css";

type Go = (r: Partial<Route> & { view: string }) => void;

export function taskRows(s: RecordData, node?: string): RecordData[] {
  return list(s.nodes).filter(n => !node || n.id === node).flatMap(n => list(n.tasks?.rows).map((task): RecordData => ({ ...task, display_title: taskTitle(task), node: n.id, project: n.title })))
    .sort((a, b) => (a.order || 0) - (b.order || 0) || String(a.id).localeCompare(String(b.id)));
}

function taskDetails(t: RecordData) {
  return ["## Full task\n\n" + t.title, t.description, t.request?.quote && "Source request:\n" + t.request.quote,
    t.park_reason && "Waiting: " + t.park_reason, t.resume && "Resume when: " + t.resume,
    t.drop_reason && "Dropped: " + t.drop_reason,
    list(t.evidence).length ? "Evidence:\n" + t.evidence.join("\n") : "",
    t.source === "linear" && t.status === "done" ? "Completion recorded by the external tracker." : ""].filter(Boolean).join("\n\n") || "No additional notes.";
}

function TaskFacts({ task: t, graph }: { task: RecordData; graph: TaskGraph }) {
  const titles = (refs: string[]) => refs.map(ref => {
    const match = graph.byKey.get(dependencyKey(t, ref));
    return match ? `${match.id}: ${taskTitle(match)}` : `${ref} (outside this view or unavailable)`;
  }).join(", ");
  const unlocks = [...downstream(graph, taskKey(t))].map(key => graph.byKey.get(key)!.id);
  return <div className="task-facts">
    {!!t.depends_on?.length && <p>Depends on: <span dir="auto">{titles(t.depends_on)}</span></p>}
    {!!t.blocked_by?.length && <p className="task-blocker">Blockers: <span dir="auto">{titles(t.blocked_by)}</span></p>}
    {unlocks.length > 0 && <p>Unlocks {unlocks.length} downstream: {unlocks.join(", ")}</p>}
    {t.dispatch_holds?.length ? <p className="task-blocker"><Text>{t.dispatch_holds.join(" · ")}</Text></p> : null}
  </div>;
}

function TaskTechnical({ task: t }: { task: RecordData }) {
  return <TechnicalDetails values={{ Task: t.id, "Original title": t.title, Job: t.branch, Commit: t.merge_sha, Code: t.code_path,
    Requirement: t.requirement, Decisions: t.decisions?.join(", "), Source: t.request?.ref }} />;
}

function TaskCard({ task: t, graph, s, go }: { task: RecordData; graph: TaskGraph; s: RecordData; go: Go }) {
  return <article className="task-card" data-task-status={t.status} data-task-id={t.id} data-tone={taskTone(t)}>
    <div className="task-card-id"><code>{t.id}</code>{taskTone(t) === "blocked" ? <span className="task-blocked">Blocked</span> : t.ready_now ? <span className="task-ready">Ready now</span> : null}</div>
    <ReaderRow literalTitle title={taskTitle(t)} text={taskDetails(t)} className="task-reader" trigger="Read task" footer={<TaskTechnical task={t} />}>
      <div className="task-card-copy"><strong dir="auto">{taskTitle(t)}</strong><small><Text>{t.project}</Text></small></div>
    </ReaderRow>
    <div className="task-meta"><Status>{t.delivery_state === "unproven" ? "Evidence unproven" : label(t.status)}</Status><span>{t.priority}</span><span dir="auto">{t.owner || "Unassigned"}</span></div>
    <p className="task-branch">Job/branch: <code>{t.branch || t.job || "Unlinked"}</code></p>
    <TaskFacts task={t} graph={graph} />
    {t.status === "parked" && <p className="task-blocker">Resume when: <Text>{t.resume || "A resume condition is recorded"}</Text></p>}
    {t.provider_pending && <p className="task-blocker">Tracker update pending</p>}
    <div className="task-links">
      {t.branch && <Button size="xs" variant="outline" onClick={() => go({ view: "project", node: t.node, tab: "threads", thread: t.branch })}>View job</Button>}
      {safeHref(t.external_url) && <a href={safeHref(t.external_url)} target="_blank" rel="noreferrer">Open tracker ↗</a>}
      {t.request?.at && <small>{time(t.request.at, s.brain?.tz)}{t.request.date_basis === "file-observed" ? " · source observed" : ""}</small>}
    </div>
  </article>;
}

export function Tasks({ s, go, node }: { s: RecordData; go: Go; node?: string }) {
  const [query, setQuery] = useState(""), [owner, setOwner] = useState(""), [view, setView] = useState("board");
  const [filterNode, setFilterNode] = useState(""), [dropped, setDropped] = useState(false);
  const [opened, setOpened] = useState<RecordData | null>(null);
  const rows = useMemo(() => taskRows(s), [s]);
  const graph = useMemo(() => taskGraph(rows), [rows]);
  const scoped = useMemo(() => rows.filter(t => !node || t.node === node), [rows, node]);
  const available = useMemo(() => filterTasks(scoped, "", "", "", dropped), [scoped, dropped]);
  const filtered = useMemo(() => filterTasks(available, node || filterNode, owner, query, dropped), [available, node, filterNode, owner, query, dropped]);
  const owners = [...new Set(scoped.map(t => String(t.owner || "")).filter(Boolean))].sort();
  const nodes = list(s.nodes).filter(n => scoped.some(t => t.node === n.id));
  const errors = list(s.nodes).filter(n => (!node || n.id === node) && n.tasks?.error);
  const columns = dropped ? [...taskColumns, "Dropped"] : [...taskColumns];
  return <section className="surface tasks-surface" data-project={node ? "true" : undefined} aria-label="Tasks">
    {errors.map(n => <p role="alert" className="task-blocker" key={n.id}>{n.title}: {n.tasks.error}</p>)}
    <div className="task-filters">
      <Input aria-label="Search tasks" placeholder="Search tasks or branches" value={query} onChange={e => setQuery(e.target.value)} />
      {!node && <Choice tooltips={false} name="Filter tasks by node" value={filterNode} onChange={setFilterNode} options={[{ value: "", label: "All nodes" }, ...nodes.map(n => ({ value: n.id, label: n.title }))]} />}
      <Choice tooltips={false} name="Filter tasks by owner" value={owner} onChange={setOwner} options={[{ value: "", label: "All owners" }, ...owners.map(o => ({ value: o, label: o }))]} />
      <Button variant={dropped ? "secondary" : "outline"} size="sm" aria-pressed={dropped} onClick={() => setDropped(v => !v)}>Show dropped</Button>
      <small>{filtered.length} of {available.length} tasks · {available.reduce((sum, t) => sum + (t.attempt_count || 0), 0)} attempts{!dropped ? " · dropped hidden" : ""}</small>
    </div>
    {s.brain?.filling_in && <p className="task-loading" role="status">Task snapshot loaded; refreshing workspace details…</p>}
    <SectionTabs value={view} onValue={setView} tabs={[
      { id: "board", label: "Board", body: <>
        {!filtered.length && <Empty text={scoped.length ? "No matching tasks." : "No tasks recorded yet."} action={scoped.length ? "Try another filter or show dropped tasks." : "Your coordinator can import existing commitments with the task migration command."} />}
        <div className="task-board" style={{ "--task-columns": columns.length } as React.CSSProperties}>
          {columns.map(column => {
            const cards = filtered.filter(t => taskColumn(t) === column);
            return <section className="task-column" key={column} aria-label={column + " tasks"}>
              <h2>{column} <span>{cards.length}</span></h2>
              <div className="task-column-cards">{cards.length ? <>
                {cards.filter(t => column !== "Parked" || t.status === "blocked").map(t => <TaskCard key={taskKey(t)} task={t} graph={graph} s={s} go={go} />)}
                {column === "Parked" && cards.some(t => t.status !== "blocked") && <Collapsible className="task-parked-group">
                  <CollapsibleTrigger render={<Button variant="outline" size="sm" className="task-parked-toggle" />}>Show {cards.filter(t => t.status !== "blocked").length} parked tasks</CollapsibleTrigger>
                  <CollapsibleContent keepMounted className="task-parked-cards">
                    {cards.filter(t => t.status !== "blocked").map(t => <TaskCard key={taskKey(t)} task={t} graph={graph} s={s} go={go} />)}
                  </CollapsibleContent>
                </Collapsible>}
              </> : <p className="task-column-empty">No tasks</p>}</div>
            </section>;
          })}
        </div>
      </> },
      { id: "dependencies", label: "Canvas", body: filtered.length ? <TaskCanvas rows={filtered} graph={graph} onOpen={setOpened} /> : <Empty text="No tasks to draw." action="Try another filter or show dropped tasks." /> },
    ]} />
    {opened && <div className="task-canvas-reader"><Reader literalTitle title={taskTitle(opened)} text={taskDetails(opened)} open onOpenChange={open => { if (!open) setOpened(null); }} footer={<TaskTechnical task={opened} />}>
      <p><code>{opened.id}</code> · {opened.priority} · {opened.owner || "Unassigned"}</p>
      <p>Job/branch: <code>{opened.branch || opened.job || "Unlinked"}</code></p>
      <TaskFacts task={opened} graph={graph} />
    </Reader></div>}
  </section>;
}
