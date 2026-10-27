import { useState } from "react";
import { ArrowUpRight, FolderPlus, GitCommitHorizontal } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DataTable, Status, Text, Empty, Reader, Preview, Command, TechnicalDetails, ScreenAction, Tip, DetailRow } from "./shared";
import { Kpi, Spark, SectionTabs } from "./dash";
import { DueRow, RequestRow, PagedRows, SettlementGuide } from "./inbox";
import { ThreadRow } from "./agents";
import { list, needsView, needAsk, count, time, flight, threads, jobTitle, closed, safeHref, nowOf, within, type RecordData } from "./model";
import { ThreadLink } from "./thread-link";
import { ReaderRow } from "./detail-panel";
import { WorkStatus } from "./work-status";
import { Tasks } from "./tasks";
import { visibleTaskCount } from "./task-model";
import type { Route } from "./routes";

type Go = (r: Partial<Route> & { view: string }) => void;
export const projectState = (n: RecordData) => (n.sealed ? "Sealed" : n.paused ? "Paused" : n.archived ? "Archived" : list(n.blocked).length ? "Needs you" : "Active");
export const ancestors = (nodes: RecordData[], n: RecordData | undefined) => {
  const out: RecordData[] = [];
  for (let p = nodes.find((x) => x.id === n?.parent); p && !out.includes(p); p = nodes.find((x) => x.id === p!.parent)) out.unshift(p);
  return out;
};

// A project's page: what needs you, its threads and jobs, then its notes.
export function Project({ s, route, go, active }: { s: RecordData; route: Route; go: Go; active: boolean }) {
  const nodes = list(s.nodes), n = nodes.find((x) => x.id === route.node);
  if (!n) return <section className="surface"><Empty text="Project unavailable in this reading." action="Choose a project in the sidebar, or refresh the workspace." /></section>;
  const now = nowOf(s);
  const { decisions: needs } = needsView(s, n.id);
  const work = flight([n]), open = threads([n]), rows = [...list(n.jobs?.rows)].reverse();
  const activity: number[] = s.activity?.counts?.[n.id] || [];
  const feed = [
    ...list(s.commits).filter((c) => c.node === n.id).map((c): RecordData => ({ t: c.t, text: c.subject, kind: c.where === "home" ? "Home commit" : "Brain commit" })),
    ...list(n.decisions).map((d) => ({ t: d.t, text: d.title, said: d.said, kind: "Decision" })),
  ].sort((a, b) => (b.t || 0) - (a.t || 0));
  const notes = Object.entries(n.status || {}).filter(([k]) => !/^blocked/i.test(k));
  const coordinator = n.coordinator;
  return (
    <>
      <ScreenAction active={active}>{coordinator ? <ThreadLink href={coordinator} label="Open coordinator" primary /> : <Reader title="Set up a coordinator" text="No coordinator is linked on this machine. Open the machine-setup skill in your agent and ask it to connect this project’s coordinator. It checks the host setup, records the conversation and gives this project a place to continue work." trigger="Set up coordinator" primary />}</ScreenAction>
      <section className="surface kpi-band compact" aria-label="Project at a glance">
        <div className="kpi-row">
          <Kpi label="Needs you" value={needs.length} note={count(needs.length, "open decision")} />
          <Kpi label="Threads" value={open.length} note={`${count(list(n.worktrees).filter((w) => list(w.listening).length).length, "listening")} locally`} />
          <Kpi label="Jobs merged" value={n.jobs?.merged || 0} note={n.jobs?.median_hours != null ? `median ${n.jobs.median_hours} h · ${n.jobs.median_reviews ?? "?"} reviews` : "no median yet"} trend={{ delta: rows.filter((r) => /merged/i.test(r.Outcome || "") && within(r.Ended, now, 168)).length, text: `${rows.filter((r) => /merged/i.test(r.Outcome || "") && within(r.Ended, now, 168)).length} this week` }} />
          <Kpi label={`Commits · ${activity.length || s.hours || 72} h`} value={activity.reduce((a, b) => a + b, 0)} note={n.updated ? "status " + (n.updated_ago || time(n.updated, s.brain?.tz)) : "status time not recorded"}>
            <Spark values={activity} label={`Commits per hour, last ${activity.length} hours`} />
          </Kpi>
        </div>
      </section>
      <section className="surface" aria-label="Work">
        <SectionTabs value={route.tab && ["needs", "threads", "jobs", "activity", "tasks"].includes(route.tab) ? route.tab : needs.length ? "needs" : "threads"}
          onValue={(tab) => go({ view: "project", node: n.id, tab })}
          tabs={[
            { id: "needs", label: "Needs you", count: needs.length, body: needs.length ? <PagedRows items={needs} noun="item" render={(a: RecordData, i) =>
                a.kind === "blocked" && !a.id && typeof a.text === "string" && !("first_seen" in a)
                  ? <BlockedRow key={"b" + i} item={a} s={s} />
                  : "first_seen" in a ? <RequestRow key={a.id || i} s={s} item={a} go={go} showProject={false} compact /> : <DueRow key={"d" + i} s={s} item={a} go={go} showProject={false} />} /> : <Empty text="Nothing needs you here." action="Open questions and decisions waiting on you arrive here." /> },
            { id: "threads", label: "Threads", count: work.length, body: work.length ? <PagedRows resetKey={route.thread || ""} items={[...work].sort((a, b) => Number(b.job.branch === route.thread) - Number(a.job.branch === route.thread))} noun="thread" render={(f, i) => <ThreadRow key={f.job.branch + i} s={s} f={f} go={go} showProject={false} highlight={route.thread === f.job.branch} />} />
                : <Empty text="No threads in flight." action="Start a job from this project's coordinator." /> },
            { id: "tasks", label: "Tasks", count: visibleTaskCount(list(n.tasks?.rows)), body: <Tasks s={s} go={go} node={n.id} /> },
            { id: "jobs", label: "Jobs", count: rows.length, body: <><SettlementGuide node={n} /><DataTable rows={rows} name="jobs" columns={[
                { key: "Job", label: "Job", render: (r) => <Text>{jobTitle(r.Job)}</Text> },
                { key: "Started", label: "Started", render: (r) => time(r.Started, s.brain?.tz) },
                { key: "Hours", label: "Hours", numeric: true, render: (r) => closed(r) ? r.Hours || "Unmeasured" : "open" },
                { key: "Outcome", label: "Outcome", render: (r) => <div><Status>{r.Outcome || "Open"}</Status>{r.Cause && <small><Text>{r.Cause}</Text></small>}</div> },
                { key: "Reviews", label: "Reviews", numeric: true },
              ]} /></> },
            { id: "activity", label: "Activity", count: feed.length, body: feed.length ? <PagedRows className="rows log" items={feed} noun="entry" render={(r, i) => (
                <ReaderRow key={i} title={r.kind || "Activity"} text={[r.text, r.said].filter(Boolean).join("\n\n")}>
                  <span className="row-icon"><GitCommitHorizontal size={16} /></span>
                  <div className="row-main"><strong className="row-title"><Text>{r.text}</Text></strong><small>{r.kind}</small></div>
                  <small className="tabular-nums">{time(r.t, s.brain?.tz)}</small>
                </ReaderRow>)} /> : <Empty text="No recent activity." action="Commits and decisions for this project appear here." /> },
          ]} />
      </section>
      <section className="surface" aria-label="Notes">
        <SectionTabs tabs={[
          ...notes.map(([k, v]) => ({ id: "note-" + k, label: k, body: <div className="note-body"><Preview text={v} title={k} /></div> })),
          ...(list(n.decisions).length ? [{ id: "decisions", label: "Decisions", count: n.decisions.length, body: <PagedRows items={n.decisions as RecordData[]} noun="decision" render={(d: RecordData, i: number) => (
              <ReaderRow key={i} title="Decision" text={[d.title, d.said].filter(Boolean).join("\n\n")}><div className="row-main"><strong className="row-title"><Text>{d.title}</Text></strong>{d.said && <small className="clamp-1"><Text>{d.said}</Text></small>}</div><small className="tabular-nums">{time(d.t || d.time, s.brain?.tz)}</small></ReaderRow>)} /> }] : []),
          ...(list(n.limits).length ? [{ id: "limits", label: "Limits", count: n.limits.length, body: <PagedRows items={n.limits as string[]} noun="limit" render={(t: string, i: number) => <ReaderRow key={i} title="Limit" text={t}><div className="row-main"><p className="row-text plain"><Text>{t}</Text></p></div></ReaderRow>} /> }] : []),
          ...(list(n.reports).length ? [{ id: "reports", label: "Reports", count: n.reports.length, body: <PagedRows items={n.reports as RecordData[]} noun="report" render={(r: RecordData, i: number) => (
              <ReaderRow key={i} title={r.title || "Report"} text="Ask the coordinator to open this report for review." trigger="Open report details" footer={<TechnicalDetails values={{ Location: r.path }} />}><div className="row-main"><strong className="row-title"><Text>{r.title}</Text></strong></div></ReaderRow>)} /> }] : []),
          { id: "about", label: "About", body: <div className="about">
              <dl className="detail-facts">
                <dt>State</dt><dd><Status>{projectState(n)}</Status></dd>
                {n.runtime && <><dt>Runtime</dt><dd>{n.runtime}</dd></>}
                {n.owners?.length > 0 && <><dt>Owners</dt><dd>{n.owners.join(", ")}</dd></>}
                {n.labels?.length > 0 && <><dt>Labels</dt><dd>{n.labels.join(" · ")}</dd></>}
                {n.updated && <><dt>Updated</dt><dd>{time(n.updated, s.brain?.tz)}</dd></>}
                {n.home && <><dt>Home</dt><dd>On this machine</dd></>}
              </dl>
              {(!n.owners?.length || !n.updated || !n.home) && <small>Not recorded: {[!n.owners?.length && "owners", !n.updated && "updated", !n.home && "home"].filter(Boolean).join(", ")}</small>}
              {safeHref(n.preview_url) && <a href={safeHref(n.preview_url)}>Open preview <ArrowUpRight size={14} /></a>}
              {n.never_name > 0 && <p>{count(n.never_name, "restricted word")} must stay out of this project’s files. <Reader title="Privacy instructions" text="Ask your coordinator to check restricted words before sharing this project. The command inspects the local files." trigger="Check privacy instructions" footer={<Command text={"brain guard " + n.id} />} /></p>}
              <TechnicalDetails values={{ Home: n.home, Preview: n.preview, "Preview tour": n.preview_tour }} />
            </div> },
        ]} />
      </section>
    </>
  );
}

export function Projects({ s, go, onAdd }: { s: RecordData; go: Go; onAdd: () => void }) {
  const nodes = list(s.nodes).filter((n) => !n.archived), open = threads(nodes);
  if (!nodes.length) return (
    <section className="surface" aria-label="Projects">
      <Empty text="No projects yet." action="Discover local repositories, then choose which become projects." icon={FolderPlus} />
      <div className="discover-inline"><Command text="brain ui --scan FOLDER" label="Copy discovery command" /><Button variant="outline" onClick={onAdd}>Add projects</Button></div>
    </section>
  );
  return (
    <section className="surface" aria-label="Projects">
      <DataTable name="projects" details={false} onRowOpen={(n) => go({ view: "project", node: n.id })} rows={nodes.map((n) => ({ ...n, _needs: needsView(s, n.id).decisions.length, _threads: open.filter((f) => f.node.id === n.id).length, _key: n.id }))}
        columns={[
          { key: "title", label: "Project", render: (n) => <div><strong><Tip text={n.title || "Project"}><span className="clamp-1"><Text>{n.title || "Untitled project"}</Text></span></Tip></strong><small>{nodes.find((p) => p.id === n.parent)?.title || "Workspace root"}</small></div> },
          { key: "_needs", label: "Needs you", numeric: true },
          { key: "_threads", label: "Threads", numeric: true },
          { key: "state", label: "State", sortValue: projectState, render: (n) => <Status>{projectState(n)}</Status> },
          { key: "updated", label: "Updated", render: (n) => n.updated_ago || "Not recorded" },
          { key: "open", label: "Open", render: (n) => <Button variant="link" size="sm" className="item-action" onClick={() => go({ view: "project", node: n.id })}>Open project</Button> },
        ]} />
    </section>
  );
}

function BlockedRow({ item, s }: { item: RecordData; s: RecordData }) {
  const [open, setOpen] = useState(false);
  return <DetailRow className="row" onOpen={() => setOpen(true)}><div className="row-main"><div className="row-line"><WorkStatus item={{ state: "Blocked", ...item }} s={s} /></div><p className="row-text"><Text>{needAsk(item.text)}</Text></p></div><div className="row-actions"><Reader title="Blocked work" text={item.text} trigger="Read" open={open} onOpenChange={setOpen} /></div></DetailRow>;
}
