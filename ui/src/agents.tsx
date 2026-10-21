import { useState } from "react";
import { ArrowUpRight, Bot, Clock3, GitBranch, Server } from "lucide-react";
import { Button } from "@/components/ui/button";
import { JobReview } from "./work";
import { DataTable, Status, Text, Empty, Disclosure, Bars, Reader, ScreenAction, FilterToolbar, Tip, ActionLink } from "./shared";
import { Kpi, SectionTabs } from "./dash";
import { Pager } from "./inbox";
import { RecordPicker } from "./picker";
import type { Found } from "./search";
import { flight, threads, tone, jobTitle, hasMeasuredUsage, list, hireRow, closed, count, time, tokens, allJobs, nowOf, stamp, jobState, safeHref, label, type RecordData } from "./model";
import type { Route } from "./routes";

type Go = (r: Partial<Route> & { view: string }) => void;
export const totalTokens = (r: RecordData) => ["Fresh in", "Cache read", "Cache write", "Output"].reduce((a, k) => a + (Number(r[k]) || 0), 0);
const median = (values: number[]) => { const v = values.filter(Number.isFinite).sort((a, b) => a - b); return v.length ? v[Math.floor(v.length / 2)] : null; };

export function Specialists({ s, active }: { s: RecordData; active: boolean }) {
  const [query, setQuery] = useState(""), [page, setPage] = useState(0), [size, setSize] = useState(10);
  const nodes = list(s.nodes), all = list(s.specialists);
  const rows = all.filter((sp) => `${sp.title} ${(sp.scope || []).join(" ")} ${sp.runtime}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <>
      <ScreenAction active={active}><Reader title="Bring in a specialist" text="Ask a project coordinator to hire a specialist for the next job. Each hire records its task, runtime and outcome here." trigger="Hire a specialist" primary /></ScreenAction>
      <section className="surface" aria-label="Specialists">
        <FilterToolbar query={query} onQuery={(v) => { setQuery(v); setPage(0); }} placeholder="Search specialists" />
        <div className="rows">{rows.slice(page * size, (page + 1) * size).map((sp, i) => (
          <div className="row specialist" data-item key={sp.node + sp.id + i}>
            <span className="row-icon"><Bot size={16} /></span>
            <div className="row-main">
              <Tip text={sp.title || "Specialist"}><strong className="row-title"><Text>{sp.title || "Specialist"}</Text></strong></Tip>
              <small>{[sp.runtime || "Runtime not recorded", nodes.find((n) => n.id === sp.node)?.title || "Workspace", sp.scope?.length ? "Scope: " + sp.scope.join(", ") : ""].filter(Boolean).join(" · ")}</small>
            </div>
            <div className="row-actions">
              {list(sp.lessons).length > 0 && <span className="pill">{count(list(sp.lessons).length, "lesson")}</span>}
              {sp.imported && <Status>Imported · review source</Status>}
              <Status>{sp.status || "Active"}</Status>
              <Reader title={sp.title || "Specialist"} trigger="Open specialist" text={[sp.scope?.length ? "Scope: " + sp.scope.join(", ") : "", ...list(sp.lessons).map((l) => "- " + l.text)].filter(Boolean).join("\n\n") || "No role notes recorded yet."}>
                {list(sp.jobs).length ? <Disclosure title={count(sp.jobs.length, "recorded hire")} initial>
                  <DataTable name="hires" rows={sp.jobs.map((h: RecordData) => { const r = hireRow(h, nodes); return { ...h, reviews: r.Reviews || h.reviews || "Not recorded", result: h.outcome || (r.Job ? r.Outcome || "Open" : "Outcome not recorded") }; })}
                    columns={[
                      { key: "job", label: "Project / job", render: (h) => <div><strong>{nodes.find((n) => n.id === h.node)?.title || "Project"}</strong><small>{jobTitle(h.job)}</small></div> },
                      { key: "time", label: "Hired", render: (r) => time(r.time, s.brain?.tz) },
                      { key: "task", label: "Task", render: (r) => <Text>{r.task || "Task not recorded"}</Text> },
                      { key: "reviews", label: "Reviews", numeric: true },
                      { key: "runtime", label: "Runtime / subscription", render: (r) => <span>{r.runtime || "Unknown"}<small>{r.subscription || "Subscription not recorded"}</small></span> },
                      { key: "result", label: "Outcome", render: (r) => <Status>{r.result}</Status> },
                    ]} />
                </Disclosure> : <p className="muted">No recorded hires.</p>}
              </Reader>
            </div>
          </div>
        ))}</div>
        {!rows.length && <Empty text={query ? "No specialist matches." : "No specialists yet."} action={query ? "Clear the search to see every specialist." : "Use brain specialist to add a role for your next job."} icon={Bot} />}
        {rows.length > 0 && <Pager total={rows.length} page={page} size={size} onPage={setPage} onSize={setSize} noun="specialist" />}
      </section>
    </>
  );
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
        <DataTable name="jobs" key={outcome + (project?.id || "")} rows={rows.map((r) => ({ ...r, _key: r._node.id + r.Job + r.Started }))}
          columns={[
            { key: "Job", label: "Job", sortValue: (r) => jobTitle(r.Job), render: (r) => <div><strong><Text>{jobTitle(r.Job)}</Text></strong><small>{r._node.title || "Project"}</small>{!closed(r) && r._node.jobs?.median_hours != null && Number(r._open?.age_h) > 2 * r._node.jobs.median_hours && <Status>Over twice median</Status>}<UsageFlags n={r._node} r={r} /></div> },
            { key: "Started", label: "Started", sortValue: (r) => stamp(r.Started) || 0, render: (r) => time(r.Started, s.brain?.tz) },
            { key: "Hours", label: "Duration", numeric: true, sortValue: (r) => Number(closed(r) ? r.Hours : r._open?.age_h) || 0, render: (r) => closed(r) ? `${r.Hours || "Unmeasured"} h` : `${r._open?.age_h ?? "Unknown"} h · open` },
            { key: "Usage", label: "Tokens / active", numeric: true, sortValue: (r) => usage(r)?.processed ?? totalTokens(r), render: (r) => <span>{usage(r) ? tokens(usage(r).processed) : hasMeasuredUsage(r) ? tokens(totalTokens(r)) : "Unmeasured"}<small>{usage(r)?.active_h ?? r["Active h"] ?? "Unmeasured"} h active</small></span> },
            { key: "Reviews", label: "Reviews", numeric: true },
            { key: "Runtime", label: "Runtime", render: (r) => <span>{r.Runtime || "Unknown"}<small>{r.Subscription || "Subscription not recorded"}</small></span> },
            { key: "Outcome", label: "Outcome", render: (r) => <Status>{r.Outcome || "Open"}</Status> },
            { key: "Details", label: "Lessons and cause", render: (r) => <Text>{[r.Cause, r.Lessons ? "Lessons: " + r.Lessons : "", usage(r) ? `Models: ${Object.keys(usage(r).models || {}).join(", ")} · ${count(usage(r).compactions || 0, "compaction")}` : r.Models ? "Models: " + r.Models : "", r._open?.budget ? `Budget: ${[r._open.budget.tokens ? tokens(r._open.budget.tokens) + " tokens" : "", r._open.budget.hours ? r._open.budget.hours + " h" : ""].filter(Boolean).join(" · ")}` : r.Budget ? "Budget: " + r.Budget : ""].filter(Boolean).join(" · ") || "Not recorded"}</Text> },
          ]} />
      </section>
      <section className="surface" aria-label="Recurring work">
        <div className="surface-head"><h2>Recurring work</h2><small>{count(list(s.schedules).length, "schedule")}</small></div>
        <div className="rows">{list(s.schedules).map((a, i) => (
          <div className="row" data-item key={i}>
            <span className="row-icon"><Clock3 size={16} /></span>
            <div className="row-main"><strong className="row-title">{label(a.id || "Scheduled work")}</strong><small>{nodes.find((n) => n.id === a.node)?.title || "Workspace"} · {a.when}</small></div>
            <div className="row-actions"><Status>{a.here ? "On this machine" : "Another machine"}</Status><Button variant="ghost" size="sm" onClick={() => go({ view: "project", node: a.node })}>Open project</Button></div>
          </div>
        ))}</div>
        {!list(s.schedules).length && <p className="muted inset-text">No recurring work. Ask a coordinator to schedule a check or a report.</p>}
      </section>
    </>
  );
}

export function ThreadRow({ s, f, go, showProject = true, highlight = false }: { s: RecordData; f: ReturnType<typeof flight>[number]; go: Go; showProject?: boolean; highlight?: boolean }) {
  const { node: n, job: j, wt: w, ledger: l } = f, state = jobState(j.state), due = list(s.due);
  const writing = w?.wrote && Date.now() / 1000 - w.wrote < 120;
  return (
    <div className={"row thread" + (highlight ? " highlighted" : "")} data-item data-thread={j.branch}>
      <span className={"row-icon tone-" + tone(f, due)}><GitBranch size={16} /></span>
      <div className="row-main">
        <div className="row-line"><strong className="row-title">{jobTitle(j.branch)}</strong>{showProject && <Button variant="link" size="sm" className="inline-link" onClick={() => go({ view: "project", node: n.id })}>{n.title || "Untitled project"}</Button>}</div>
        <small className="thread-meta">
          <Status>{state.kind}</Status>
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
        {safeHref(j.link) && <ActionLink href={j.link}>Open conversation <ArrowUpRight size={14} /></ActionLink>}
        <JobReview node={n} job={j} />
      </div>
    </div>
  );
}

export function Threads({ s, active, go }: { s: RecordData; active: boolean; go: Go }) {
  const [query, setQuery] = useState(""), [project, setProject] = useState<Found | null>(null);
  const all = flight(list(s.nodes)), open = threads(list(s.nodes));
  const rows = all.filter((f) => (!project || f.node.id === project.id) && `${jobTitle(f.job.branch)} ${f.node.title} ${f.job.state}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <>
      <ScreenAction active={active}>{open[0] ? <JobReview node={open[0].node} job={open[0].job} primary trigger="Check next thread" /> : <Reader title="Threads" text="No job conversations are open. Start a job from a project coordinator." trigger="How threads start" primary />}</ScreenAction>
      <section className="surface" aria-label="Threads in flight">
        <FilterToolbar query={query} onQuery={setQuery} placeholder="Search threads">
          <RecordPicker s={s} kind="node" label="Thread project" hideLabel value={project} onChange={setProject} allLabel="All projects" placeholder="Search projects" />
        </FilterToolbar>
        <div className="rows">{rows.map((f, i) => <ThreadRow key={f.node.id + f.job.branch + i} s={s} f={f} go={go} />)}</div>
        {!rows.length && <Empty text={query || project ? "No thread matches." : "No threads in flight."} action={query || project ? "Clear the filters to see every thread." : "Start a job from a project coordinator."} icon={GitBranch} />}
      </section>
    </>
  );
}
