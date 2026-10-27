import { useState, type ReactNode } from "react";
import { usePage } from "./hooks/use-page";
import { ThreadLink } from "./thread-link";
import { WorkStatus } from "./work-status";
import { Button } from "@/components/ui/button";
import { FilterToolbar, Text, Reader, Empty, ScreenAction, Status, ListFrame, DetailRow } from "./shared";
import { SectionTabs } from "./dash";
import { RecordPicker } from "./picker";
import { list, time, sender, dueParts, readable, needAsk, needsView, type RecordData } from "./model";
import type { Found } from "./search";
import type { Route } from "./routes";

type Go = (r: Partial<Route> & { view: string }) => void;
const body = (item: RecordData) => String(item.text || "").replace(/^(?:inbox:\s*)?\d{4}-\d{2}-\d{2}[^\n]*? from [^:]+:\s*(?:question:\s*)?/i, "");

// One request, one line of context, one action: the sender comes from the record, never the text.
export function RequestRow({ s, item, go, showProject = true, compact = false }: { s: RecordData; item: RecordData; go: Go; showProject?: boolean; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const project = list(s.nodes).find((n) => n.id === item.node);
  const conversation = item.link || project?.coordinator;
  const recommendation = typeof item.recommendation === "string" ? item.recommendation : "";
  const nextAction = typeof item.default === "string" && item.default.trim() ? `Review: ${item.default}` : "";
  const question = /question|answer|\?/i.test(item.text || "");
  const full = [item.text, recommendation, nextAction].filter(Boolean).join("\n\n");
  return (
    <DetailRow className="row request" data-item onOpen={() => setOpen(true)}>
      <div className="row-main">
        <div className="row-line">{showProject && <strong dir="auto"><Text>{project?.title || "Workspace"}</Text></strong>}<small>{sender(item)} · {time(item.first_seen, s.brain?.tz)}</small><WorkStatus item={{ ...item, state: item.state || "waiting", waiting_on_operator: true }} s={s} /></div>
        <p className="row-text" dir="auto"><Text>{compact ? needAsk(body(item)) : body(item)}</Text></p>
        {!compact && recommendation && <p className="row-note" dir="auto">Recommended: <Text>{recommendation}</Text></p>}
      </div>
      <div className="row-actions">
        {conversation ? <ThreadLink href={conversation} label={(!compact && nextAction) || (question ? "Answer" : "Open conversation")} />
          : project ? <Button variant="outline" size="sm" onClick={() => go({ view: "project", node: project.id })}><Text>{(!compact && nextAction) || "Open project"}</Text></Button>
          : null}
        <Reader open={open} onOpenChange={setOpen} title="Request" text={full} trigger="Read request" />
      </div>
    </DetailRow>
  );
}

// A coordinator step names its job, then says what to do; inline code renders as code.
export function DueRow({ s, item, go, showProject = true }: { s: RecordData; item: RecordData; go: Go; showProject?: boolean }) {
  const [open, setOpen] = useState(false);
  const project = list(s.nodes).find((n) => n.id === item.node);
  const { title, label, body } = dueParts(item);
  const coordinator = project?.coordinator;
  return (
    <DetailRow className="row due" data-item onOpen={() => setOpen(true)}>
      <div className="row-main">
        <div className="row-line"><Status>{label}</Status>{(item.first_seen || item.since || item.waiting_on || item.fault || item.failures) && <WorkStatus item={item} s={s} />}{(title || showProject) && <strong dir="auto">{title || project?.title || "Workspace"}</strong>}{title && showProject && <small>{project?.title || "Workspace"}</small>}</div>
        <p className="row-text" dir="auto"><Text zone={s.brain?.tz}>{body}</Text></p>
      </div>
      <div className="row-actions">
        {coordinator ? <ThreadLink href={coordinator} label="Open coordinator" />
          : <Button variant="outline" size="sm" onClick={() => go({ view: "project", node: item.node })}>Open project</Button>}
        <Reader open={open} onOpenChange={setOpen} title={title || "Next step"} text={[body, ...list(item.reasons).map((r) => "- " + r)].join("\n\n")} trigger="Read details" />
      </div>
    </DetailRow>
  );
}

// Compatibility export for callers while they adopt the shared frame.
export function Pager(props: { total: number; page: number; size: number; onPage: (p: number) => void; onSize: (s: number) => void; noun: string }) {
  return <ListFrame {...props}>{null}</ListFrame>;
}

// Any list that can grow: ten rows at a time with a pager, never an unbounded column.
export function PagedRows<T>({ items, render, noun, size: initial = 10, className = "rows", resetKey = "" }: { items: T[]; render: (item: T, i: number) => ReactNode; noun: string; size?: number; className?: string; resetKey?: string }) {
  const { page: current, size, setPage, setSize, start, end } = usePage(items.length, resetKey, initial);
  return <ListFrame total={items.length} page={current} size={size} onPage={setPage} onSize={setSize} noun={noun}>
    <div className={className}>{items.slice(start, end).map((item, i) => render(item, current * size + i))}</div>
  </ListFrame>;
}

export function InboxView({ s, go, tab, active }: { s: RecordData; go: Go; tab: string | null; active: boolean }) {
  const [query, setQuery] = useState(""), [project, setProject] = useState<Found | null>(null);
  const nodes = list(s.nodes), title = (id: string) => nodes.find((n) => n.id === id)?.title || "";
  const match = (a: RecordData) => (!project || a.node === project.id) && `${readable(a.text)} ${a.branch || ""} ${title(a.node)}`.toLowerCase().includes(query.toLowerCase());
  const needs = needsView(s);
  const decisions = needs.decisions.filter(match);
  const requests = needs.inbox.filter(match), steps = needs.due.filter(match).sort((a, b) => Number(!!b.ready) - Number(!!a.ready));
  const current = tab === "due" ? "due" : tab === "decisions" ? "decisions" : "inbox", rows = current === "due" ? steps : current === "decisions" ? decisions : requests;
  const { page, size, setPage, setSize, start, end } = usePage(rows.length, JSON.stringify([current, query, project?.id]));
  const shown = rows.slice(start, end);
  const next = (current === "decisions" ? decisions : requests)[0], conversation = next && (next.link || nodes.find((n) => n.id === next.node)?.coordinator);
  // The primary action follows the tab: the next request, or the next coordinator step.
  const step = steps[0];
  const stepLink = step && nodes.find((n) => n.id === step.node)?.coordinator;
  const reset = () => { setQuery(""); setProject(null); setPage(0); };
  return (
    <>
      <ScreenAction active={active}>{current === "due"
        ? step ? stepLink ? <ThreadLink href={stepLink} primary label={step.ready ? "Review ready job" : "Open next step"} /> : <Button onClick={() => go({ view: "project", node: step.node, tab: "needs" })}>{step.ready ? "Review ready job" : "Open next step"}</Button>
          : <Button onClick={() => go({ view: "inbox" })}>Back to requests</Button>
        : conversation ? <ThreadLink href={conversation} primary label={next?.default ? "Review next step" : /question|answer|\?/i.test(next?.text || "") ? "Answer next question" : "Open next request"} /> : <Button onClick={() => document.querySelector<HTMLInputElement>('[aria-label="Search inbox"]')?.focus()}>Find a request</Button>}</ScreenAction>
      <section className="surface list-surface" aria-label="Waiting for you">
        <FilterToolbar query={query} onQuery={(v) => { setQuery(v); setPage(0); }} placeholder="Search inbox">
          <RecordPicker s={s} kind="node" label="Inbox project" hideLabel value={project} onChange={(v) => { setProject(v); setPage(0); }} allLabel="All projects" placeholder="Search projects" />
          <Button variant="outline" disabled={!query && !project} onClick={reset}>Clear filters</Button>
        </FilterToolbar>
        <SectionTabs value={current} onValue={(v) => { go({ view: "inbox", tab: v === "inbox" ? null : v }); setPage(0); }}
          tabs={[
            { id: "decisions", label: "Open decisions", count: decisions.length, body: null },
            { id: "inbox", label: "Requests", count: requests.length, body: null },
            { id: "due", label: "Coordinator steps", count: steps.length, body: null },
          ]} />
        <ListFrame total={rows.length} page={page} size={size} onPage={setPage} onSize={setSize} noun={current === "due" ? "step" : current === "decisions" ? "decision" : "request"}>
        <div className="rows">{shown.map((a, i) => current === "due" ? <DueRow key={i} s={s} item={a} go={go} /> : <RequestRow key={a.id || i} s={s} item={a} go={go} />)}</div>
        {!rows.length && <Empty text={query || project ? "Nothing matches these filters." : current === "due" ? "All caught up." : "Nothing waiting here."} action={query || project ? "Clear the filters to see every item." : "Questions and review requests arrive here."} />}
        </ListFrame>
      </section>
    </>
  );
}
