import { useState } from "react";
import { usePage } from "./hooks/use-page";
import { Bot, Clock3, GitBranch, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import { JobReview } from "./work";
import { DataTable, Status, Text, Empty, Bars, Reader, ScreenAction, FilterToolbar, Tip, Command, ListFrame, DetailRow, Markdown } from "./shared";
import { Kpi, SectionTabs } from "./dash";
import { PagedRows } from "./inbox";
import { RecordPicker } from "./picker";
import type { Found } from "./search";
import { flight, threads, tone, jobTitle, hasMeasuredUsage, list, hireRow, closed, count, time, tokens, allJobs, nowOf, stamp, jobState, label, type RecordData } from "./model";
import type { Route } from "./routes";
import { ThreadLink } from "./thread-link";
import { WorkStatus } from "./work-status";
import { ReviewActions } from "./review";

type Go = (r: Partial<Route> & { view: string }) => void;
export const totalTokens = (r: RecordData) => ["Fresh in", "Cache read", "Cache write", "Output"].reduce((a, k) => a + (Number(r[k]) || 0), 0);
const median = (values: number[]) => { const v = values.filter(Number.isFinite).sort((a, b) => a - b); return v.length ? v[Math.floor(v.length / 2)] : null; };

export function Specialists({ s, active, item }: { s: RecordData; active: boolean; item?: string | null }) {
  const [query, setQuery] = useState(""), [selected, setSelected] = useState<string | null>(null), [dismissed, setDismissed] = useState<string | null>(null);
  const nodes = list(s.nodes), all = list(s.specialists);
  const rows = all.filter((sp) => `${sp.title} ${(sp.scope || []).join(" ")} ${sp.runtime}`.toLowerCase().includes(query.toLowerCase()));
  const { page, size, setPage, setSize, start, end } = usePage(rows.length, query);
  const current = item && item !== dismissed ? item : selected;
  return <>
    <ScreenAction active={active}><Reader title="Bring in a specialist" text="Choose a role below, read when to hire it, and copy its hire command for your project coordinator. Each hire records its task, runtime and outcome here." trigger="Hire a specialist" primary /></ScreenAction>
    <section className="surface list-surface" aria-label="Specialists">
      <FilterToolbar query={query} onQuery={setQuery} placeholder="Search specialists" />
      <ListFrame total={rows.length} page={page} size={size} onPage={setPage} onSize={setSize} noun="specialist" controls={<ReviewActions all records={rows.filter(sp => sp.status === "proposed").map(sp => ({ kind: "specialist" as const, id: sp.id, node: sp.node }))} />}>
        <div className="rows">{rows.slice(start, end).map((sp, i) => {
          const key = sp.node + "/" + sp.id;
          return <DetailRow className="row specialist" key={key + i} onOpen={() => { setSelected(key); setDismissed(item || null); }}>
            <span className="row-icon"><Bot size={16} /></span>
            <div className="row-main"><Tip text={sp.title || "Specialist"}><strong className="row-title"><Text>{sp.title || "Specialist"}</Text></strong></Tip>
              <small>{[sp.runtime || "Runtime not recorded", nodes.find((n) => n.id === sp.node)?.title || "Workspace", sp.scope?.length ? "Scope: " + sp.scope.join(", ") : ""].filter(Boolean).join(" · ")}</small>
            </div>
            <div className="row-actions"><span className="pill">{count(list(sp.lessons).length, "lesson")}</span>{sp.imported && <Status>Imported · review source</Status>}<Status>{sp.status || "Active"}</Status>
              <SpecialistPanel s={s} sp={sp} open={current === key} onOpenChange={open => { setSelected(open ? key : null); if (!open) setDismissed(item || null); }} />
            </div>
          </DetailRow>;
        })}</div>
        {!rows.length && <Empty text={query ? "No specialist matches." : "No specialists yet."} action={query ? "Clear the search to see every specialist." : "Use brain specialist to add a role for your next job."} icon={Bot} />}
      </ListFrame>
      {item && !rows.slice(start, end).some(sp => sp.node + "/" + sp.id === item) && all.filter(sp => sp.node + "/" + sp.id === item).map(sp => <SpecialistPanel key={item} s={s} sp={sp} open={current === item} onOpenChange={open => { setSelected(open ? item : null); if (!open) setDismissed(item); }} />)}
    </section>
  </>;
}

function SpecialistPanel({ s, sp, open, onOpenChange }: { s: RecordData; sp: RecordData; open: boolean; onOpenChange: (open: boolean) => void }) {
  const nodes = list(s.nodes), lessons = [...list(sp.lessons)].sort((a, b) => (stamp(b.time) || 0) - (stamp(a.time) || 0));
  const related = list(s.items?.all || s.items?.recent).filter(i => i.specialist === sp.id || i.specialist === sp.node + "/" + sp.id || (list(i.scope).length && list(i.scope).some(scope => list(sp.scope).includes(scope))));
  const summary = sp.role_summary || sp.purpose || `${sp.title || "This specialist"} is available for ${list(sp.scope).join(", ") || "a focused project task"}.`;
  const role = sp.role_body || sp.body || "";
  return <Reader title={sp.title || "Specialist"} trigger="Open specialist" text={summary} open={open} onOpenChange={onOpenChange} footer={<>{sp.status === "proposed" && <ReviewActions records={[{ kind: "specialist", id: sp.id, node: sp.node }]} />}{sp.hire_command ? <Command text={sp.hire_command} label="Copy hire command" /> : <p>Ask the project coordinator to hire this role for a specific task.</p>}</>}>
    <dl className="detail-facts"><dt>Runtime</dt><dd>{sp.runtime || "Choose with the coordinator"}</dd><dt>Scope</dt><dd>{list(sp.scope).join(", ") || "Project scope"}</dd><dt>Lessons</dt><dd>{count(lessons.length, "lesson")}</dd><dt>Hires</dt><dd>{count(list(sp.jobs).length, "recorded hire")}</dd></dl>
    <h2>What it’s for / when to hire</h2>
    {role ? <Markdown text={role.replace(/^# [^\n]+\n+/, "")} /> : <p>{sp.when_to_hire || `Bring in ${sp.title || "this role"} when a task needs a focused ${list(sp.scope).join(", ") || "specialist"} review. Agree the brief and expected result with the coordinator first.`}</p>}
    <h2>Latest lessons</h2>
    {lessons.length ? <ul>{lessons.slice(0, 3).map((l, i) => <li key={i}><Text>{l.text}</Text><small>{nodes.find(n => n.id === l.from)?.title || "Project"} · {time(l.time, s.brain?.tz)}</small></li>)}</ul> : <p>No lessons recorded. The next hire can add its outcome and what the role learned.</p>}
    <h2>Related library items</h2>
    {related.length ? <ul>{related.slice(0, 8).map(i => <li key={i.id}><Reader title={i.title || "Library item"} text={i.body || i.title} trigger={i.title || "Read item"} /></li>)}</ul> : <p>No library items are linked to this role yet. Browse Learnings for knowledge in the project’s scope.</p>}
    <h2>Hired by projects</h2>
    {list(sp.jobs).length ? <DataTable name="hires" rows={sp.jobs.map((h: RecordData) => { const r = hireRow(h, nodes); return { ...h, reviews: r.Reviews || h.reviews || "Not recorded", result: h.outcome || (r.Job ? r.Outcome || "Open" : "Outcome not recorded") }; })} columns={[
      { key: "job", label: "Project / job", render: h => <div><strong>{nodes.find(n => n.id === h.node)?.title || "Project"}</strong><small>{jobTitle(h.job)}</small></div> },
      { key: "time", label: "Hired", render: r => time(r.time, s.brain?.tz) },
      { key: "task", label: "Task", render: r => <Text>{r.task || "Task not recorded"}</Text> },
      { key: "reviews", label: "Reviews", numeric: true },
      { key: "runtime", label: "Runtime", render: r => r.runtime || "Not recorded" },
      { key: "result", label: "Outcome", render: r => <Status>{r.result}</Status> },
    ]} /> : <p>No recorded hires. Use the hire command to start the first task and keep its outcome here.</p>}
  </Reader>;
}

function UsageFlags({ n, r }: { n: RecordData; r: RecordData }) {
  const u = r._open?.usage, b = r._open?.budget;
  return <>
    {u && n.jobs.median_tokens && u.processed > 2 * n.jobs.median_tokens && <Status>Tokens over twice median</Status>}
    {u && b?.tokens && u.processed > b.tokens && <Status>Over token budget</Status>}
    {b?.hours && r._open?.age_h > b.hours && <Status>Over time budget</Status>}
    {u?.compactions >= 3 && <Status>{count(u.compactions, "compaction")}</Status>}
    {u?.subagents && u.sub_share > 0.5 ? <Status>Sub-agents {Math.round(u.sub_share * 100)}%</Status> : null}
  </>;
}

export function Jobs({ s, active, go }: { s: RecordData; active: boolean; go: Go }) {
  const [query, setQuery] = useState(""), [project, setProject] = useState<Found | null>(null), [outcome, setOutcome] = useState("all");
  const nodes = list(s.nodes), now = nowOf(s), jobs = allJobs(nodes);
  const kind = (r: RecordData) => (!closed(r) ? "open" : /merged/i.test(r.Outcome) ? "merged" : /abandon/i.test(r.Outcome) ? "abandoned" : "closed");
  const scoped = jobs.filter((r) => (!project || r._node.id === project.id) && `${jobTitle(r.Job)} ${r._node.title} ${r.Outcome || ""} ${r.Lessons || ""}`.toLowerCase().includes(query.toLowerCase()));
  const rows = scoped.filter((r) => outcome === "all" || kind(r) === outcome).sort((a, b) => (stamp(b.Started) || 0) - (stamp(a.Started) || 0));
  const merged7 = jobs.filter((r) => kind(r) === "merged" && now - stamp(r.Ended) <= 7 * 86400_000).length;
  const medHours = median(jobs.filter(closed).map((r) => Number(r.Hours)).filter((h) => h > 0));
  const medTokens = median(jobs.filter((r) => closed(r) && hasMeasuredUsage(r)).map(totalTokens));
  const next = flight(nodes)[0];
  const usage = (r: RecordData) => r._open?.usage;
  return (
    <>
      <ScreenAction active={active}>{next ? <JobReview node={next.node} job={next.job} primary trigger="Check active job" /> : <Reader title="Job history" text="No jobs are in progress. Open a finished job below to see its outcome and lessons." trigger="Check job history" primary />}</ScreenAction>
      <section className="surface kpi-band compact" aria-label="Jobs at a glance">
        <div className="kpi-row">
          <Kpi label="Open" value={jobs.filter((r) => kind(r) === "open").length} note={`across ${count(new Set(jobs.filter((r) => kind(r) === "open").map((r) => r._node.id)).size, "project")}`} />
          <Kpi label="Merged · 7 days" value={merged7} note={`${jobs.filter((r) => kind(r) === "merged").length} all time`} />
          <Kpi label="Median duration" value={medHours == null ? "—" : medHours + " h"} note="finished jobs" />
          <Kpi label="Median tokens" value={medTokens == null ? "—" : tokens(medTokens)} note="measured jobs" />
        </div>
        <Bars unit="hours" label="Finished job durations, oldest to newest" entries={jobs.filter((r) => Number(r.Hours) > 0).sort((a, b) => stamp(a.Started) - stamp(b.Started)).slice(-40).map((r) => ({ key: `${r._node.title} · ${jobTitle(r.Job)}`, tokens: r.Hours }))} />
      </section>
      <section className="surface" aria-label="Job ledger">
        <FilterToolbar query={query} onQuery={setQuery} placeholder="Search jobs">
          <RecordPicker s={s} kind="node" label="Job project" hideLabel value={project} onChange={setProject} allLabel="All projects" placeholder="Search projects" />
        </FilterToolbar>
        <SectionTabs value={outcome} onValue={setOutcome} tabs={[["all", "All"], ["open", "Open"], ["merged", "Merged"], ["abandoned", "Abandoned"]].map(([id, l]) => ({ id, label: l, count: scoped.filter((r) => id === "all" || kind(r) === id).length, body: null }))} />
        <DataTable name="jobs" resetKey={JSON.stringify([query, outcome, project?.id])} rows={rows.map((r) => ({ ...r, _key: r._node.id + r.Job + r.Started }))}
          columns={[
            { key: "Job", label: "Job", sortValue: (r) => jobTitle(r.Job), render: (r) => <div><strong><Text>{jobTitle(r.Job)}</Text></strong><small>{r._node.title || "Project"}</small>{!closed(r) && r._node.jobs?.median_hours != null && Number(r._open?.age_h) > 2 * r._node.jobs.median_hours && <Status>Over twice median</Status>}<UsageFlags n={r._node} r={r} /></div> },
            { key: "Started", label: "Started", sortValue: (r) => stamp(r.Started) || 0, render: (r) => time(r.Started, s.brain?.tz) },
            { key: "Hours", label: "Duration", numeric: true, sortValue: (r) => Number(closed(r) ? r.Hours : r._open?.age_h) || 0, render: (r) => closed(r) ? `${r.Hours || "Unmeasured"} h` : `${r._open?.age_h ?? "Unknown"} h · open` },
            { key: "Usage", label: "Tokens / active", numeric: true, sortValue: (r) => usage(r)?.processed ?? totalTokens(r), render: (r) => <span>{usage(r) ? tokens(usage(r).processed) : hasMeasuredUsage(r) ? tokens(totalTokens(r)) : "Unmeasured"}<small>{usage(r)?.active_h ?? r["Active h"] ?? "Unmeasured"} h active</small></span> },
            { key: "Reviews", label: "Reviews", numeric: true },
            { key: "Runtime", label: "Runtime", render: (r) => <span>{r.Runtime || "Unknown"}<small>{r.Subscription || "Subscription not recorded"}</small></span> },
            { key: "Outcome", label: "Outcome", render: (r) => closed(r) ? <Status>{r.Outcome}</Status> : <WorkStatus s={s} item={list(r._node.work).find(j => j.branch === r.Job) || { state: "In progress" }} ledger={r._open || r} /> },
            { key: "Details", label: "Lessons and cause", render: (r) => <Text>{[r.Cause, r.Lessons ? "Lessons: " + r.Lessons : "", usage(r) ? `Models: ${Object.keys(usage(r).models || {}).join(", ")} · ${count(usage(r).compactions || 0, "compaction")}` : r.Models ? "Models: " + r.Models : "", r._open?.budget ? `Budget: ${[r._open.budget.tokens ? tokens(r._open.budget.tokens) + " tokens" : "", r._open.budget.hours ? r._open.budget.hours + " h" : ""].filter(Boolean).join(" · ")}` : r.Budget ? "Budget: " + r.Budget : ""].filter(Boolean).join(" · ") || "Not recorded"}</Text> },
          ]} />
      </section>
      <section className="surface" aria-label="Recurring work">
        <div className="surface-head"><h2>Recurring work</h2><small>{count(list(s.schedules).length, "schedule")}</small></div>
        <PagedRows items={list(s.schedules)} noun="schedule" render={(a, i) => (
          <div className="row" data-item key={i}>
            <span className="row-icon"><Clock3 size={16} /></span>
            <div className="row-main"><strong className="row-title">{label(a.id || "Scheduled work")}</strong><small>{nodes.find((n) => n.id === a.node)?.title || "Workspace"} · {a.when}</small></div>
            <div className="row-actions"><Status>{a.here ? "On this machine" : "Another machine"}</Status><Button variant="ghost" size="sm" onClick={() => go({ view: "project", node: a.node })}>Open project</Button></div>
          </div>
        )} />
        {!list(s.schedules).length && <p className="muted inset-text">No recurring work. Ask a coordinator to schedule a check or a report.</p>}
      </section>
    </>
  );
}

export function ThreadRow({ s, f, go, showProject = true, highlight = false }: { s: RecordData; f: ReturnType<typeof flight>[number]; go: Go; showProject?: boolean; highlight?: boolean }) {
  const [open, setOpen] = useState(false);
  const { node: n, job: j, wt: w, ledger: l } = f, state = jobState(j.state), due = list(s.due);
  const writing = w?.wrote && Date.now() / 1000 - w.wrote < 120;
  return (
    <DetailRow className={"row thread" + (highlight ? " highlighted" : "")} data-thread={j.branch} onOpen={() => setOpen(true)}>
      <span className={"row-icon tone-" + tone(f, due)}><GitBranch size={16} /></span>
      <div className="row-main">
        <div className="row-line"><strong className="row-title">{jobTitle(j.branch)}</strong>{showProject && <Button variant="link" size="sm" className="inline-link" onClick={() => go({ view: "project", node: n.id })}>{n.title || "Untitled project"}</Button>}</div>
        <small className="thread-meta">
          <WorkStatus item={j} s={s} ledger={l} />
          {state.description && <Tip text={j.state}><span className="flight-description"><Text>{state.description}</Text></span></Tip>}
        </small>
      </div>
      <div className="row-facts">
        {writing ? <Status>Writing now</Status> : null}
        <small><Server size={12} /> {w?.port ? `${w.port}${w.ports > 1 ? "–" + (w.port + w.ports - 1) : ""} · ${list(w.listening).length ? "listening" : "idle"}` : w ? "No port assigned" : "No local working copy"}</small>
        {w && <small>{w.dirty ?? "Unknown"} changes · {w.ahead ?? "Unknown"} ahead · {w.wrote ? "wrote " + time(w.wrote, s.brain?.tz) : w.last ? "commit " + time(w.last, s.brain?.tz) : "activity not recorded"}</small>}
        {l && <small>{Number(l.age_h || 0).toFixed(1)} h · {count(Number(l.Reviews || 0), "review")}{w?.ready ? " · " + (w.ready.ready ? "review checks passed" : "review checks pending") : ""}</small>}
      </div>
      <div className="row-actions">
        <ThreadLink href={j.link} />
        <JobReview node={n} job={j} open={open} onOpenChange={setOpen} />
      </div>
    </DetailRow>
  );
}

export function Threads({ s, active, go }: { s: RecordData; active: boolean; go: Go }) {
  const [query, setQuery] = useState(""), [project, setProject] = useState<Found | null>(null);
  const all = flight(list(s.nodes)), open = threads(list(s.nodes));
  const rows = all.filter((f) => (!project || f.node.id === project.id) && `${jobTitle(f.job.branch)} ${f.node.title} ${f.job.state}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <>
      <ScreenAction active={active}>{open[0] ? <JobReview node={open[0].node} job={open[0].job} primary trigger="Check next thread" /> : <Reader title="Threads" text="No job conversations are open. Start a job from a project coordinator." trigger="How threads start" primary />}</ScreenAction>
      <section className="surface list-surface" aria-label="Threads in flight">
        <FilterToolbar query={query} onQuery={setQuery} placeholder="Search threads">
          <RecordPicker s={s} kind="node" label="Thread project" hideLabel value={project} onChange={setProject} allLabel="All projects" placeholder="Search projects" />
        </FilterToolbar>
        <PagedRows resetKey={JSON.stringify([query, project?.id])} items={rows} noun="thread" render={(f, i) => <ThreadRow key={f.node.id + f.job.branch + i} s={s} f={f} go={go} />} />
        {!rows.length && <Empty text={query || project ? "No thread matches." : "No threads in flight."} action={query || project ? "Clear the filters to see every thread." : "Start a job from a project coordinator."} icon={GitBranch} />}
      </section>
    </>
  );
}
