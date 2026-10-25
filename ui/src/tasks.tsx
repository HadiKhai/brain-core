import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SectionTabs } from "./dash";
import { ReaderRow } from "./detail-panel";
import { Choice, Empty, Status, TechnicalDetails, Text } from "./shared";
import { label, list, safeHref, time, type RecordData } from "./model";
import type { Route } from "./routes";
import "./tasks.css";

type Go = (r: Partial<Route> & { view: string }) => void;
const groups = [
  { title: "Backlog", states: ["backlog"] },
  { title: "Ready", states: ["ready"] },
  { title: "In progress", states: ["in-progress", "in-review", "testing"] },
  { title: "Waiting", states: ["blocked", "parked"] },
  { title: "Completed", states: ["merged", "done"] },
  { title: "Dropped", states: ["dropped"] },
];

export function taskRows(s: RecordData, node?: string): RecordData[] {
  return list(s.nodes).filter(n => !node || n.id === node).flatMap(n => list(n.tasks?.rows).map((task): RecordData => ({ ...task, node: n.id, project: n.title })))
    .sort((a, b) => (a.order || 0) - (b.order || 0) || String(a.id).localeCompare(String(b.id)));
}

function TaskCard({ task: t, rows, s, go }: { task: RecordData; rows: RecordData[]; s: RecordData; go: Go }) {
  const titles = (ids: string[]) => ids.map(id => rows.find(r => r.node === t.node && r.id === id)?.title || id).join(", ");
  const details = [t.description, t.request?.quote && "Source request:\n" + t.request.quote,
    t.park_reason && "Waiting: " + t.park_reason, t.resume && "Resume when: " + t.resume,
    t.drop_reason && "Dropped: " + t.drop_reason,
    list(t.evidence).length ? "Evidence:\n" + t.evidence.join("\n") : "",
    t.source === "linear" && t.status === "done" ? "Completion recorded by the external tracker." : ""].filter(Boolean).join("\n\n");
  return <article className="task-card" data-task-status={t.status}>
    <ReaderRow title={t.title} text={details || "No additional notes."} className="task-reader" trigger="Read task"
      footer={<TechnicalDetails values={{ Task: t.id, Job: t.branch, Commit: t.merge_sha, Code: t.code_path,
        Requirement: t.requirement, Decisions: t.decisions?.join(", "), Source: t.request?.ref }} />}>
      <div className="task-card-copy"><strong><Text>{t.title}</Text></strong><small><Text>{t.project}</Text></small></div>
    </ReaderRow>
    <div className="task-meta"><Status>{label(t.status)}</Status><span>{t.priority}</span><span dir="auto">{t.owner || "Unassigned"}</span></div>
    {!!t.blocked_by?.length && <p className="task-blocker">Blocked by <Text>{titles(t.blocked_by)}</Text></p>}
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
  const rows = useMemo(() => taskRows(s, node), [s, node]);
  const filtered = rows.filter(t => (!query || [t.title, t.project, t.id].join(" ").toLocaleLowerCase().includes(query.toLocaleLowerCase())) && (!owner || t.owner === owner));
  const owners = [...new Set(rows.map(t => String(t.owner || "")).filter(Boolean))].sort();
  const errors = list(s.nodes).filter(n => (!node || n.id === node) && n.tasks?.error);
  return <section className="surface tasks-surface" aria-label="Tasks">
    {errors.map(n => <p role="alert" className="task-blocker" key={n.id}>{n.title}: {n.tasks.error}</p>)}
    <div className="task-filters">
      <Input aria-label="Search tasks" placeholder="Search tasks" value={query} onChange={e => setQuery(e.target.value)} />
      <Choice name="Filter tasks by owner" value={owner} onChange={setOwner} options={[{ value: "", label: "All owners" }, ...owners.map(o => ({ value: o, label: o }))]} />
      <small>{filtered.length} of {rows.length} tasks</small>
    </div>
    <SectionTabs value={view} onValue={setView} tabs={[
      { id: "board", label: "Board", body: filtered.length ? <div className="task-board">
        {groups.filter(g => filtered.some(t => g.states.includes(t.status))).map(g => <section className="task-column" key={g.title} aria-label={g.title + " tasks"}>
          <h2>{g.title} <span>{filtered.filter(t => g.states.includes(t.status)).length}</span></h2>
          {filtered.filter(t => g.states.includes(t.status)).map(t => <TaskCard key={t.node + ":" + t.id} task={t} rows={rows} s={s} go={go} />)}
        </section>)}
      </div> : <Empty text={rows.length ? "No matching tasks." : "No tasks recorded yet."} action={rows.length ? "Try another search or owner." : "Your coordinator can import existing commitments with the task migration command."} /> },
      { id: "dependencies", label: "Dependencies", body: filtered.length ? <ul className="task-dependencies" aria-label="Task dependency map">
        {filtered.map(t => <li key={t.node + ":" + t.id}><strong><Text>{t.title}</Text></strong><Status>{label(t.status)}</Status>
          <span>{t.depends_on?.length ? "Depends on " + t.depends_on.map((id: string) => rows.find(r => r.node === t.node && r.id === id)?.title || id).join(", ") : "No dependencies"}</span>
          {!!t.blocked_by?.length && <small className="task-blocker">{t.blocked_by.length} unfinished</small>}
          {!!t.blocks?.length && <small>Blocks {t.blocks.length}</small>}
        </li>)}
      </ul> : <Empty text="No dependencies to show." /> },
    ]} />
  </section>;
}
