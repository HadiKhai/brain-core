import { useState } from "react";
import { ArrowUpRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PageSection, FilterToolbar, Text, Reader, Empty, Choice, ScreenAction, ActionLink } from "./shared";
import { list, safeHref, time, type RecordData } from "./model";

export function Correspondence({ s, item, onNode }: { s: RecordData; item: RecordData; onNode: (id: string) => void }) {
  const project = list(s.nodes).find(n => n.id === item.node);
  const conversation = safeHref(item.link) || safeHref(project?.coordinator);
  const body = String(item.text || "").replace(/^(?:inbox:\s*)?\d{4}-\d{2}-\d{2}[^\n]*? from [^:]+:\s*(?:question:\s*)?/i, "");
  const sender = String(item.text || "").match(/ from ([^:]+):/)?.[1];
  const question = /question|answer|\?/i.test(item.text || "");
  return <article className="correspondence">
    <div className="correspondence-heading"><strong dir="auto"><Text>{project?.title || "Workspace"}</Text></strong><small>{sender ? `From ${sender} · ` : ""}{time(item.first_seen, s.brain?.tz)}</small></div>
    <p className="correspondence-excerpt" dir="auto"><Text>{body}</Text></p>
    <div className="correspondence-actions">
      {conversation ? <ActionLink href={conversation}>{question ? "Answer" : "Open conversation"}<ArrowUpRight size={14} /></ActionLink> : project ? <Button variant="outline" size="sm" onClick={() => onNode(project.id)}>Open project</Button> : <Reader title="Request" text={item.text} trigger="Open request" />}
      {(String(item.text || "").length > 180 || project) && <Reader title="Request" text={item.text} trigger="Read request" />}
    </div>
  </article>;
}

export function InboxView({ s, onNode, active }: { s: RecordData; onNode: (id: string) => void; active: boolean }) {
  const [query, setQuery] = useState(""), [project, setProject] = useState("all"), [page, setPage] = useState(0);
  const rows = list(s.inbox).filter(a => (project === "all" || a.node === project) && `${a.text} ${list(s.nodes).find(n => n.id === a.node)?.title || ""}`.toLowerCase().includes(query.toLowerCase()));
  const pages = Math.max(1, Math.ceil(rows.length / 10)), current = Math.min(page, pages - 1);
  const next = rows[0], conversation = next && (safeHref(next.link) || safeHref(list(s.nodes).find(n => n.id === next.node)?.coordinator));
  return <><ScreenAction active={active}>{conversation ? <ActionLink href={conversation}>Answer next question</ActionLink> : <Button variant="default" onClick={() => document.querySelector<HTMLInputElement>('[aria-label="Search inbox"]')?.focus()}>Find a request</Button>}</ScreenAction><PageSection title="Waiting for you" description="Read the request and continue the conversation" action={<span className="count-badge">{rows.length}</span>}>
    <FilterToolbar query={query} onQuery={v => {setQuery(v);setPage(0);}} placeholder="Search inbox">
      <Choice name="Inbox project" value={project} onChange={v => {setProject(v);setPage(0);}} options={[{value:"all",label:"All projects"}, ...list(s.nodes).map(n => ({value:n.id,label:n.title || "Project"}))]} />
      <Button variant="outline" onClick={() => {setQuery("");setProject("all");setPage(0);}}>Clear filters</Button>
    </FilterToolbar>
    <div className="inbox-list">{rows.slice(current * 10, current * 10 + 10).map((a,i) => <Correspondence key={a.id || i} s={s} item={a} onNode={onNode} />)}</div>
    {!rows.length ? <Empty text="Nothing waiting here." action="Questions and review requests will arrive here." /> : <footer className="table-footer"><span>{current * 10 + 1}–{Math.min(rows.length, current * 10 + 10)} of {rows.length} requests</span><div className="pager"><Button variant="outline" disabled={!current} onClick={() => setPage(current - 1)}>Previous</Button><span>{current + 1} / {pages}</span><Button variant="outline" disabled={current === pages - 1} onClick={() => setPage(current + 1)}>Next</Button></div></footer>}
  </PageSection></>;
}
