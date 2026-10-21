import { useState } from "react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useContext, useEffect } from "react";
import {
  FilterToolbar,
  Choice,
  Empty,
  Status,
  Text,
  Reader,
  ScreenAction,
  TechnicalDetails,
  Disclosure,
  TimeZoneContext,
} from "./shared";
import { SectionTabs } from "./dash";
import { Pager } from "./inbox";
import { RecordPicker } from "./picker";
import type { Found } from "./search";
import { learningRows, list, quote, time, type RecordData } from "./model";
function DecisionFooter({ item, reviewer, setReviewer, decision, setDecision }: { item: RecordData; reviewer: string; setReviewer: (value: string) => void; decision: string; setDecision: (value: string) => void }) {
  const [feedback, setFeedback] = useState("");
  const command = decision === "accept" ? "brain item accept --reviewed-by " + quote(reviewer || "REVIEWER") + " -- " + quote(item.id) : "brain item reject -- " + quote(item.id);
  return <div className="decision-actions">
    <p>Copy your decision, then run it in your terminal.</p>
    <div className="decision-fields"><Choice name="Decision" disabled={feedback === "Copying…"} value={decision} onChange={value => {setDecision(value);setFeedback("");}} options={[{value:"accept",label:"Accept"},{value:"reject",label:"Reject"}]} />
      {decision === "accept" && <div className="field"><label>Reviewer name<Input disabled={feedback === "Copying…"} value={reviewer} onChange={e => {setReviewer(e.target.value);setFeedback("");}} placeholder="Your name" aria-label="Reviewer name" /></label></div>}
    </div>
    <div className="decision-copy"><Disclosure title="View command"><pre className="verbatim" dir="ltr">{command}</pre></Disclosure>
      <Button disabled={feedback === "Copying…" || (decision === "accept" && !reviewer.trim())} onClick={async () => {setFeedback("Copying…");try {await navigator.clipboard.writeText(command);setFeedback("Copied. Run the command to apply your decision.");} catch {setFeedback("Copy failed. Open the command and copy its text.");}}}>{decision === "accept" ? "Copy acceptance" : "Copy rejection"}</Button>
    </div>
    {feedback && <p role="status">{feedback}</p>}
  </div>;
}
export function ProposalReader({ item: i, primary = false }: { item: RecordData; primary?: boolean }) {
  const [reviewer, setReviewer] = useState(""), [decision, setDecision] = useState("accept");
  const reviewable = i.state === "proposed" && !i.specialist;
  const zone = useContext(TimeZoneContext);
  return <Reader title={i.title || "Learning"} text={i.body || "No additional text recorded."} trigger={reviewable ? "Review proposal" : "Read learning"} primary={primary}
    footer={reviewable ? <DecisionFooter item={i} reviewer={reviewer} setReviewer={setReviewer} decision={decision} setDecision={setDecision} /> : undefined}>
    <dl className="detail-facts">
      {i.scope?.length > 0 && <><dt>Scope</dt><dd>{i.scope.join(", ")}</dd></>}
      {i.provenance?.owners?.length > 0 && <><dt>Source owners</dt><dd>{i.provenance.owners.join(", ")}</dd></>}
      {i.accepted_at && <><dt>Accepted</dt><dd>{time(i.accepted_at, zone)} by {i.accepted_by || "reviewer not recorded"}</dd></>}
    </dl>
    <TechnicalDetails values={{Source:i.provenance?.ref, Repository:i.provenance?.repo, Revision:i.provenance?.commit}} />
  </Reader>;
}
const states = [["all", "All"], ["proposed", "Proposed"], ["active", "Accepted"], ["rejected", "Rejected"], ["quarantined", "Quarantined"]] as const;
export function Learnings({ s, active, initialState }: { s: RecordData; active: boolean; initialState: string | null }) {
  const [query, setQuery] = useState(""),
    [node, setNode] = useState<Found | null>(null),
    [specialist, setSpecialist] = useState<Found | null>(null),
    [state, setState] = useState(initialState || "all"),
    [page, setPage] = useState(0), [size, setSize] = useState(10);
  useEffect(() => { if (initialState) { setState(initialState); setPage(0); } }, [initialState]);
  const rows = learningRows(s),
    scoped = rows.filter((i) => (!node || node.id === i.node) && (!specialist || specialist.id === i.specialist) &&
      `${i.title} ${i.body || ""}`.toLowerCase().includes(query.toLowerCase())),
    filtered = scoped.filter((i) => state === "all" || state === i.state);
  const title = (id: string) => list(s.nodes).find((n) => n.id === id)?.title || "Source project not recorded";
  const reset = () => { setQuery(""); setNode(null); setSpecialist(null); setState("all"); setPage(0); };
  const first = rows.find((r) => r.state === "proposed" && !r.specialist) || rows[0];
  return (
    <><ScreenAction active={active}>{first ? <ProposalReader item={first} primary /> : <Reader title="Shared knowledge" text="Record a lesson from a finished job to start your library. New proposals appear here for review." trigger="Start your library" primary />}</ScreenAction>
    <section className="surface learning-layout" aria-label="Shared knowledge">
      <aside className="filter-panel">
        <FilterToolbar query={query} onQuery={(v) => { setQuery(v); setPage(0); }} placeholder="Search learnings" />
        <RecordPicker s={s} kind="node" label="Project" value={node} onChange={(v) => { setNode(v); setPage(0); }} allLabel="All projects" placeholder="Search projects" />
        <RecordPicker s={s} kind="specialist" label="Specialist" value={specialist} onChange={(v) => { setSpecialist(v); setPage(0); }} allLabel="All specialists" placeholder="Search specialists" />
        <Button variant="outline" disabled={!query && !node && !specialist && state === "all"} onClick={reset}>Clear filters</Button>
      </aside>
      <div className="learning-results">
        <SectionTabs value={state} onValue={(v) => { setState(v); setPage(0); }} tabs={states.map(([id, label]) => ({ id, label, count: scoped.filter((r) => id === "all" || r.state === id).length, body: null }))} />
        <p className="results-meta" role="status">{filtered.length} {filtered.length === 1 ? "learning" : "learnings"}</p>
        <div className="rows">{filtered.slice(page * size, (page + 1) * size).map((i: any, k) => (
          <div className="row learning" data-item key={i.id + ":" + i.specialist + k}>
            <div className="row-main">
              <strong className="row-title"><Text>{i.title || "Untitled learning"}</Text></strong>
              <small>{i.kind || "Library item"} · {title(i.node)} · {i.provenance?.time ? time(i.provenance.time, s.brain?.tz) : "date not recorded"}{i.provenance?.owners?.length ? " · " + i.provenance.owners.join(", ") : ""}</small>
            </div>
            <div className="row-actions">
              <Status>{i.state === "active" ? "Accepted" : i.state === "proposed" ? "Proposed" : i.state}</Status>
              <ProposalReader item={i} />
            </div>
          </div>
        ))}</div>
        {!filtered.length && <Empty text="No learnings match." action="Clear the filters or record a lesson from a finished job." />}
        {filtered.length > 0 && <Pager total={filtered.length} page={page} size={size} onPage={setPage} onSize={setSize} noun="learning" />}
      </div>
    </section></>
  );
}
