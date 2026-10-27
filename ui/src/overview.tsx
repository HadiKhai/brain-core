import { ThreadLink } from "./thread-link";
import { ChevronRight, FolderPlus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Empty, ScreenAction, Tip, Text } from "./shared";
import { Kpi, Segmented, SectionTabs, ListFooter, type Mark } from "./dash";
import { RequestRow, DueRow } from "./inbox";
import { ProposalReader } from "./learnings";
import { adoptable } from "./imports";
import { list, count, needsView, threads, tone, nowOf, within, stamp, allJobs, learningRows, jobTitle, jobState, time, windowState, type RecordData } from "./model";
import { graphData, layout } from "./graph";
import { useMemo } from "react";
import type { Route } from "./routes";

export const greeting = (s: RecordData) => {
  let hour = new Date(nowOf(s)).getHours();
  try { hour = Number(new Intl.DateTimeFormat("en", { hour: "numeric", hourCycle: "h23", timeZone: s.brain?.tz || undefined }).format(new Date(nowOf(s)))); } catch { /* local time */ }
  return hour < 5 ? "Good evening" : hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
};
// One sentence of what needs the operator, built from counts, never from prose.
export function overviewSummary(s: RecordData) {
  const { decisions, due } = needsView(s), ready = due.filter((d) => d.ready);
  const parts = [
    decisions.length ? `${count(decisions.length, "open decision")} wait for you.` : "Nothing waits for you.",
    ready.length ? `${count(ready.length, "job")} ${ready.length === 1 ? "is" : "are"} ready to merge.` : "",
    `${count(threads(list(s.nodes)).length, "thread")} in flight across ${count(list(s.nodes).filter((n) => !n.archived).length, "project")}.`,
  ];
  return parts.filter(Boolean).join(" ");
}

export function Overview({ s, go, onAdd, active }: { s: RecordData; go: (r: Partial<Route> & { view: string }) => void; onAdd: () => void; active: boolean }) {
  const nodes = list(s.nodes), now = nowOf(s);
  const { decisions, due } = needsView(s);
  const open = threads(nodes), ready = due.filter((d) => d.ready);
  const jobs = allJobs(nodes);
  const merged = (from: number, to: number) => jobs.filter((r) => /merged/i.test(r.Outcome || "") && stamp(r.Ended) <= now - from * 3600_000 && stamp(r.Ended) > now - to * 3600_000).length;
  const mergedToday = merged(0, 24), mergedYesterday = merged(24, 48);
  const order = (f: { job: RecordData; wt?: RecordData }) => ["blocked", "ready", "review", "working", "idle"].indexOf(tone(f, due));
  const fresh = decisions.filter((i) => within(i.first_seen, now, 24)).length;
  const started = jobs.filter((r) => r._open && within(r.Started, now, 24)).length;
  const marks: Mark[] = open.map((f) => ({ key: f.node.id + f.job.branch, tone: tone(f, due), label: `${f.node.title || "Project"} · ${jobTitle(f.job.branch)} · ${jobState(f.job.state).kind}` }));
  const proposals = learningRows(s).filter((r) => r.state === "proposed" && !r.specialist);
  const candidates = list(s.scan).filter(adoptable);
  const nextInbox = decisions[0], conversation = nextInbox && (nextInbox.link || nodes.find((n) => n.id === nextInbox.node)?.coordinator);
  return (
    <div className="page-stack overview">
      <ScreenAction active={active}>
        {conversation ? <ThreadLink href={conversation} primary label={/question|\?/i.test(nextInbox.text || "") ? "Answer next question" : "Open next request"} />
          : ready.length ? <Button onClick={() => go({ view: "project", node: ready[0].node, tab: "needs" })}>Review ready job</Button>
          : <Button onClick={() => go({ view: "inbox" })}>Open Inbox</Button>}
      </ScreenAction>
      <section className="surface kpi-band" aria-label="Today at a glance">
        <Kpi big label="Needs you" value={decisions.length} note={count(decisions.length, "open decision")} trend={{ delta: fresh, text: `${fresh} new today`, good: "down" }} />
        <div className="kpi-rest">
          <div className="kpi-row">
            <Kpi label="Merges today" value={mergedToday} trend={{ delta: mergedToday - mergedYesterday, text: `${mergedToday - mergedYesterday >= 0 ? "+" : ""}${mergedToday - mergedYesterday} vs yesterday` }} />
            <Kpi label="Threads live" value={open.length} trend={{ delta: started, text: `${started} started today` }} />
            <Kpi label="Ready to merge" value={ready.length} note={ready.length ? "waiting for a merge" : "none waiting"} />
            <Kpi label="Verify queue" value={s.machine?.queued ?? "—"} note={s.machine?.verifying != null ? `${s.machine.verifying} of ${s.machine.verify_limit ?? "?"} verifying` : "queue not readable"} />
          </div>
          <Segmented marks={marks} caption={`Each mark is one open thread across ${count(new Set(open.map((f) => f.node.id)).size, "project")}`} />
          <Pulse s={s} />
          <Meters s={s} />
        </div>
      </section>
      <div className="overview-grid">
        <section className="surface needs" aria-label="Needs you" data-needs-action>
          <SectionTabs
            tabs={[
              { id: "decisions", label: "Open decisions", count: decisions.length, body: decisions.length ? <>
                  <div className="rows">{decisions.slice(0, 5).map((a, i) => <RequestRow key={a.id || i} s={s} item={a} go={go} compact />)}</div>
                  <ListFooter shown={Math.min(5, decisions.length)} total={decisions.length} noun="decisions" action={<Button variant="ghost" size="sm" onClick={() => go({ view: "inbox", tab: "decisions" })}>View all <ChevronRight size={14} /></Button>} />
                </> : <Empty text="Nothing waits for you." action="Questions from your projects arrive here." /> },
              { id: "due", label: "Coordinator steps", count: due.length, body: due.length ? <>
                  <div className="rows">{[...due].sort((a, b) => Number(!!b.ready) - Number(!!a.ready)).slice(0, 5).map((d, i) => <DueRow key={i} s={s} item={d} go={go} />)}</div>
                  <ListFooter shown={Math.min(5, due.length)} total={due.length} noun="steps" action={<Button variant="ghost" size="sm" onClick={() => go({ view: "inbox", tab: "due" })}>View all <ChevronRight size={14} /></Button>} />
                </> : <Empty text="All caught up." action="Hand-backs and status checks arrive here." /> },
              { id: "proposals", label: "Proposals", count: proposals.length, body: proposals.length ? <>
                  <div className="rows">{proposals.slice(0, 5).map((p, i) => (
                    <div className="row" data-item key={p.id + i}>
                      <div className="row-main"><strong className="row-title"><Text>{p.title || "Proposed learning"}</Text></strong><small>{p.kind || "Lesson"} · {nodes.find((n) => n.id === p.node)?.title || "Workspace"}</small></div>
                      <div className="row-actions"><ProposalReader item={p} /></div>
                    </div>))}</div>
                  <ListFooter shown={Math.min(5, proposals.length)} total={proposals.length} noun="proposals" action={<Button variant="ghost" size="sm" onClick={() => go({ view: "learnings", state: "proposed" })}>View all <ChevronRight size={14} /></Button>} />
                </> : <Empty text="No proposals to review." action="Finished jobs propose lessons here." /> },
            ]}
          />
        </section>
        <section className="surface projects-rail" aria-label="Moving now">
          <div className="surface-head"><h2>Moving now</h2><Button variant="ghost" size="sm" onClick={() => go({ view: "threads" })}>View all <ChevronRight size={14} /></Button></div>
          {open.length ? <div className="rows">{[...open].sort((a, b) => order(a) - order(b)).slice(0, 5).map((f) => (
            <Button variant="ghost" className="row moving" data-item key={f.node.id + f.job.branch} onClick={() => go({ view: "project", node: f.node.id, thread: f.job.branch, tab: "threads" })}>
              <i className={"dot tone-" + tone(f, due)} />
              <span className="row-main"><strong className="row-title">{jobTitle(f.job.branch)}</strong><small>{f.node.title || "Project"} · {jobState(f.job.state).kind}</small></span>
              <span className="row-facts compact-facts"><small className="tabular-nums">{elapsed(f.ledger?.Started, now)}</small><small>{count(Number(f.ledger?.Reviews || 0), "review")}{f.wt?.ready?.ready ? " · checks passed" : ""}</small></span>
            </Button>
          ))}</div> : <Empty text="Nothing is running." action="Start a job from a project coordinator." />}
          {open.length > 5 && <ListFooter shown={5} total={open.length} noun="threads" />}
          <div className="rail-graph">
            <MiniGraph s={s} />
            <div className="list-footer"><span>{count(list(s.nodes).filter((n) => !n.archived).length, "project")} · {count(list(s.specialists).length, "specialist")} · {count(list(s.items?.all).length, "learning")}</span><Button variant="ghost" size="sm" onClick={() => go({ view: "graph" })}>Open graph <ChevronRight size={14} /></Button></div>
          </div>
          <div className="rail-action">
            {candidates.length
              ? <Button variant="outline" size="sm" onClick={onAdd}><FolderPlus size={14} />Review {count(candidates.length, "repository")} to add</Button>
              : <Tip text="Discover local repositories and add them as projects"><Button variant="ghost" size="sm" onClick={onAdd}><FolderPlus size={14} />Add projects</Button></Tip>}
          </div>
        </section>
      </div>
    </div>
  );
}

export const elapsed = (start: any, now: number) => {
  const t = stamp(start);
  if (!Number.isFinite(t)) return "time not recorded";
  const m = Math.max(0, Math.round((now - t) / 60000));
  return m < 60 ? `${m} min` : m < 2880 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.round(m / 1440)} d`;
};

// The last 24 hours as glowing ticks: merges, questions, failures and commits, placed by time.
export function Pulse({ s }: { s: RecordData }) {
  const now = nowOf(s), day = 24 * 3600_000, zone = s.brain?.tz;
  const events: { t: number; kind: string; text: string }[] = [];
  for (const r of allJobs(list(s.nodes))) {
    if (/merged/i.test(r.Outcome || "") && within(r.Ended, now, 24)) events.push({ t: stamp(r.Ended), kind: "merge", text: `Merged · ${r._node.title} · ${jobTitle(r.Job)}` });
    if (/abandon|fail/i.test(r.Outcome || "") && within(r.Ended, now, 24)) events.push({ t: stamp(r.Ended), kind: "failure", text: `${r.Outcome} · ${r._node.title} · ${jobTitle(r.Job)}` });
  }
  for (const i of list(s.inbox)) if (within(i.first_seen, now, 24)) events.push({ t: stamp(i.first_seen), kind: "question", text: `Waiting · ${list(s.nodes).find((n) => n.id === i.node)?.title || "Workspace"}` });
  for (const c of list(s.commits)) if (within(c.t, now, 24)) events.push({ t: stamp(c.t), kind: "commit", text: `Commit · ${list(s.nodes).find((n) => n.id === c.node)?.title || "Workspace"} · ${c.subject}` });
  const shown = events.sort((a, b) => (a.kind === "commit" ? 0 : 1) - (b.kind === "commit" ? 0 : 1)).slice(-160);
  return (
    <div className="pulse-strip" aria-label={`Last 24 hours: ${events.filter((e) => e.kind === "merge").length} merges, ${events.filter((e) => e.kind === "question").length} new requests, ${events.filter((e) => e.kind === "failure").length} failures, ${events.filter((e) => e.kind === "commit").length} commits`}>
      <span className="pulse-label">24 h</span>
      <div className="pulse-track">
        {shown.map((e, i) => (
          <Tip key={i} text={`${time(e.t / 1000, zone)} · ${e.text}`} style={{ left: `${Math.min(100, Math.max(0, 100 - ((now - e.t) / day) * 100))}%` }}>
            <i className={"tick kind-" + e.kind} />
          </Tip>
        ))}
      </div>
      <span className="pulse-label">now</span>
    </div>
  );
}

// Compact gauges: each subscription's weekly window, machine load and heat.
export function Meters({ s }: { s: RecordData }) {
  const policy = { ...s.usage_routing_policy, ...s.usage_policy };
  const load = Number(s.machine?.load?.[0]) / Number(s.machine?.cpus);
  const gauges = [
    ...list(s.usage).slice(0, 3).map((row) => {
      const w = row.windows?.seven_day, v = windowState(w, row, policy);
      return { label: `${row.runtime || row.label || "Subscription"} · week`, value: v.known ? `${w.used_percent}%` : v.state, percent: v.percent, tone: /stop/i.test(v.state) ? (/At/.test(v.state) ? "danger" : "warn") : "" };
    }),
    { label: "Machine load", value: Number.isFinite(load) ? `${Math.round(load * 100)}%` : "Unknown", percent: Number.isFinite(load) ? Math.min(100, load * 100) : 0, tone: load > 0.9 ? "danger" : load > 0.7 ? "warn" : "" },
    { label: "Heat", value: s.machine?.thermal === "pressure" ? "Throttled" : s.machine?.thermal === "normal" ? "Normal" : "Unknown", percent: s.machine?.thermal === "pressure" ? 100 : s.machine?.thermal === "normal" ? 20 : 0, tone: s.machine?.thermal === "pressure" ? "danger" : "" },
  ];
  return (
    <div className="meters">
      {gauges.map((g) => (
        <div className="kpi meter-kpi" key={g.label}>
          <span className="kpi-label">{g.label}</span>
          <span className="meter-value">{g.value}</span>
          <span className={"meter-bar " + g.tone} role="meter" aria-label={g.label} aria-valuenow={Math.round(g.percent)} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${g.percent}%` }} /></span>
        </div>
      ))}
    </div>
  );
}

function MiniGraph({ s }: { s: RecordData }) {
  const g = useMemo(() => { const d = graphData(s); layout(d.nodes, d.edges, 160); return d; }, [s.nodes, s.specialists, s.items]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!g.nodes.length) return null;
  const xs = g.nodes.map((p) => p.x), ys = g.nodes.map((p) => p.y), pad = 24;
  const box = `${Math.min(...xs) - pad} ${Math.min(...ys) - pad} ${Math.max(...xs) - Math.min(...xs) + pad * 2} ${Math.max(...ys) - Math.min(...ys) + pad * 2}`;
  return (
    <svg viewBox={box} role="img" aria-label="Preview of the brain graph" preserveAspectRatio="xMidYMid meet">
      {g.edges.map((e, i) => <line key={i} x1={g.nodes[e.a].x} y1={g.nodes[e.a].y} x2={g.nodes[e.b].x} y2={g.nodes[e.b].y} className="edge" />)}
      {g.nodes.map((p) => <g key={p.key} className={`gnode kind-${p.kind}${p.recent ? " recent" : ""}`} transform={`translate(${p.x} ${p.y})`}>{p.recent && <circle r={p.r + 5} className="halo" />}<circle r={p.r} className="dot" /></g>)}
    </svg>
  );
}
