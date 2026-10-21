import { useState, type ReactNode } from "react";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { FilterToolbar, Text, Reader, Empty, Choice, ScreenAction, ActionLink, Status } from "./shared";
import { SectionTabs } from "./dash";
import { RecordPicker } from "./picker";
import { list, safeHref, time, count, sender, dueParts, readable, type RecordData } from "./model";
import type { Found } from "./search";
import type { Route } from "./routes";

type Go = (r: Partial<Route> & { view: string }) => void;
const body = (item: RecordData) => String(item.text || "").replace(/^(?:inbox:\s*)?\d{4}-\d{2}-\d{2}[^\n]*? from [^:]+:\s*(?:question:\s*)?/i, "");

// One request, one line of context, one action: the sender comes from the record, never the text.
export function RequestRow({ s, item, go, showProject = true }: { s: RecordData; item: RecordData; go: Go; showProject?: boolean }) {
  const project = list(s.nodes).find((n) => n.id === item.node);
  const conversation = safeHref(item.link) || safeHref(project?.coordinator);
  const recommendation = typeof item.recommendation === "string" ? item.recommendation : "";
  const nextAction = typeof item.default === "string" && item.default.trim() ? `Review: ${item.default}` : "";
  const question = /question|answer|\?/i.test(item.text || "");
  const full = [item.text, recommendation, nextAction].filter(Boolean).join("\n\n");
  return (
    <div className="row request" data-item>
      <div className="row-main">
        <div className="row-line">{showProject && <strong dir="auto"><Text>{project?.title || "Workspace"}</Text></strong>}<small>{sender(item)} · {time(item.first_seen, s.brain?.tz)}</small></div>
        <p className="row-text" dir="auto"><Text>{body(item)}</Text></p>
        {recommendation && <p className="row-note" dir="auto">Recommended: <Text>{recommendation}</Text></p>}
      </div>
      <div className="row-actions">
        {conversation ? <ActionLink href={conversation}><Text>{nextAction || (question ? "Answer" : "Open conversation")}</Text><ArrowUpRight size={14} /></ActionLink>
          : project ? <Button variant="outline" size="sm" onClick={() => go({ view: "project", node: project.id })}><Text>{nextAction || "Open project"}</Text></Button>
          : null}
        <Reader title="Request" text={full} trigger="Read request" />
      </div>
    </div>
  );
}

// A coordinator step names its job, then says what to do; inline code renders as code.
export function DueRow({ s, item, go, showProject = true }: { s: RecordData; item: RecordData; go: Go; showProject?: boolean }) {
  const project = list(s.nodes).find((n) => n.id === item.node);
  const { title, label, body } = dueParts(item);
  const coordinator = safeHref(project?.coordinator);
  return (
    <div className="row due" data-item>
      <div className="row-main">
        <div className="row-line"><Status>{label}</Status>{(title || showProject) && <strong dir="auto">{title || project?.title || "Workspace"}</strong>}{title && showProject && <small>{project?.title || "Workspace"}</small>}</div>
        <p className="row-text" dir="auto"><Text zone={s.brain?.tz}>{body}</Text></p>
      </div>
      <div className="row-actions">
        {coordinator ? <ActionLink href={coordinator}>Open coordinator <ArrowUpRight size={14} /></ActionLink>
          : <Button variant="outline" size="sm" onClick={() => go({ view: "project", node: item.node })}>Open project</Button>}
        <Reader title={title || "Next step"} text={[body, ...list(item.reasons).map((r) => "- " + r)].join("\n\n")} trigger="Read details" />
      </div>
    </div>
  );
}

export function Pager({ total, page, size, onPage, onSize, noun }: { total: number; page: number; size: number; onPage: (p: number) => void; onSize: (s: number) => void; noun: string }) {
  const pages = Math.max(1, Math.ceil(total / size)), current = Math.min(page, pages - 1);
  return (
    <footer className="table-footer">
      <span>{total ? `${current * size + 1}–${Math.min(total, (current + 1) * size)} of ${count(total, noun)}` : `0 ${noun}s`}</span>
      {pages > 1 && <div className="pager">
        <Choice name="Rows per page" value={String(size)} onChange={(v) => { onSize(Number(v)); onPage(0); }} options={["10", "25", "50"].map((value) => ({ value, label: value }))} />
        <Button variant="ghost" size="sm" disabled={!current} onClick={() => onPage(0)}>First</Button>
        <Button variant="outline" size="sm" disabled={!current} onClick={() => onPage(current - 1)}>Previous</Button>
        <span className="tabular-nums">{current + 1} / {pages}</span>
        <Button variant="outline" size="sm" disabled={current === pages - 1} onClick={() => onPage(current + 1)}>Next</Button>
        <Button variant="ghost" size="sm" disabled={current === pages - 1} onClick={() => onPage(pages - 1)}>Last</Button>
      </div>}
    </footer>
  );
}

// Any list that can grow: ten rows at a time with a pager, never an unbounded column.
export function PagedRows<T>({ items, render, noun, size: initial = 10, className = "rows" }: { items: T[]; render: (item: T, i: number) => ReactNode; noun: string; size?: number; className?: string }) {
  const [page, setPage] = useState(0), [size, setSize] = useState(initial);
  const pages = Math.max(1, Math.ceil(items.length / size)), current = Math.min(page, pages - 1);
  return <>
    <div className={className}>{items.slice(current * size, (current + 1) * size).map((item, i) => render(item, current * size + i))}</div>
    {items.length > initial && <Pager total={items.length} page={current} size={size} onPage={setPage} onSize={setSize} noun={noun} />}
  </>;
}

export function InboxView({ s, go, tab, active }: { s: RecordData; go: Go; tab: string | null; active: boolean }) {
  const [query, setQuery] = useState(""), [project, setProject] = useState<Found | null>(null), [page, setPage] = useState(0), [size, setSize] = useState(10);
  const nodes = list(s.nodes), title = (id: string) => nodes.find((n) => n.id === id)?.title || "";
  const match = (a: RecordData) => (!project || a.node === project.id) && `${readable(a.text)} ${a.branch || ""} ${title(a.node)}`.toLowerCase().includes(query.toLowerCase());
  const requests = list(s.inbox).filter(match), steps = list(s.due).filter(match).sort((a, b) => Number(!!b.ready) - Number(!!a.ready));
  const current = tab === "due" ? "due" : "inbox", rows = current === "due" ? steps : requests;
  const shown = rows.slice(page * size, (page + 1) * size);
  const next = requests[0], conversation = next && (safeHref(next.link) || safeHref(nodes.find((n) => n.id === next.node)?.coordinator));
  // The primary action follows the tab: the next request, or the next coordinator step.
  const step = steps[0];
  const stepLink = step && safeHref(nodes.find((n) => n.id === step.node)?.coordinator);
  const reset = () => { setQuery(""); setProject(null); setPage(0); };
  return (
    <>
      <ScreenAction active={active}>{current === "due"
        ? step ? stepLink ? <ActionLink href={stepLink} primary>{step.ready ? "Review ready job" : "Open next step"}</ActionLink> : <Button onClick={() => go({ view: "project", node: step.node, tab: "needs" })}>{step.ready ? "Review ready job" : "Open next step"}</Button>
          : <Button onClick={() => go({ view: "inbox" })}>Back to requests</Button>
        : conversation ? <ActionLink href={conversation} primary>{next?.default ? "Review next step" : /question|answer|\?/i.test(next?.text || "") ? "Answer next question" : "Open next request"}</ActionLink> : <Button onClick={() => document.querySelector<HTMLInputElement>('[aria-label="Search inbox"]')?.focus()}>Find a request</Button>}</ScreenAction>
      <section className="surface" aria-label="Waiting for you">
        <FilterToolbar query={query} onQuery={(v) => { setQuery(v); setPage(0); }} placeholder="Search inbox">
          <RecordPicker s={s} kind="node" label="Inbox project" hideLabel value={project} onChange={(v) => { setProject(v); setPage(0); }} allLabel="All projects" placeholder="Search projects" />
          <Button variant="outline" disabled={!query && !project} onClick={reset}>Clear filters</Button>
        </FilterToolbar>
        <SectionTabs value={current} onValue={(v) => { go({ view: "inbox", tab: v === "due" ? "due" : null }); setPage(0); }}
          tabs={[
            { id: "inbox", label: "Requests", count: requests.length, body: null },
            { id: "due", label: "Coordinator steps", count: steps.length, body: null },
          ]} />
        <div className="rows">{shown.map((a, i) => current === "due" ? <DueRow key={i} s={s} item={a} go={go} /> : <RequestRow key={a.id || i} s={s} item={a} go={go} />)}</div>
        {!rows.length && <Empty text={query || project ? "Nothing matches these filters." : current === "due" ? "All caught up." : "Nothing waiting here."} action={query || project ? "Clear the filters to see every item." : "Questions and review requests arrive here."} />}
        {rows.length > 0 && <Pager total={rows.length} page={page} size={size} onPage={setPage} onSize={setSize} noun={current === "due" ? "step" : "request"} />}
      </section>
    </>
  );
}
