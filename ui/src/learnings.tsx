import { useState } from "react";
import { usePage } from "./hooks/use-page";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useContext, useEffect } from "react";
import {
  ListFrame,
  DetailRow,
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
import { ReviewActions } from "./review";
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
export function ProposalReader({ item: i, primary = false, open, onOpenChange }: { item: RecordData; primary?: boolean; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const [reviewer, setReviewer] = useState(""), [decision, setDecision] = useState("accept");
  const reviewable = i.state === "proposed" && !i.specialist;
  const zone = useContext(TimeZoneContext);
  return <Reader open={open} onOpenChange={onOpenChange} title={i.title || "Learning"} text={i.body || "No additional text recorded."} trigger={reviewable ? "Review proposal" : "Read learning"} primary={primary}
    footer={reviewable ? <><ReviewActions records={[{kind:"item",id:i.id,node:i.review_node || i.node || i.provenance?.node || "root"}]} /><DecisionFooter item={i} reviewer={reviewer} setReviewer={setReviewer} decision={decision} setDecision={setDecision} /></> : undefined}>
    <dl className="detail-facts">
      {i.scope?.length > 0 && <><dt>Scope</dt><dd>{i.scope.join(", ")}</dd></>}
      {i.provenance?.owners?.length > 0 && <><dt>Source owners</dt><dd>{i.provenance.owners.join(", ")}</dd></>}
      {i.accepted_at && <><dt>Accepted</dt><dd>{time(i.accepted_at, zone)} by {i.accepted_by || "reviewer not recorded"}</dd></>}
    </dl>
    <TechnicalDetails values={{Source:i.provenance?.ref, Repository:i.provenance?.repo, Revision:i.provenance?.commit}} />
  </Reader>;
}
const states = [["all", "All"], ["proposed", "Proposed"], ["active", "Accepted"], ["rejected", "Rejected"], ["quarantined", "Quarantined"]] as const;
export function Learnings({ s, active, initialState, item }: { s: RecordData; active: boolean; initialState: string | null; item?: string | null }) {
  const [selected, setSelected] = useState<string | null>(null), [dismissed, setDismissed] = useState<string | null>(null);
  const current = selected || (item !== dismissed ? item : null);
  const [query, setQuery] = useState(""),
    [node, setNode] = useState<Found | null>(null),
    [specialist, setSpecialist] = useState<Found | null>(null),
    [state, setState] = useState(initialState || "all");
  useEffect(() => { if (initialState) { setState(initialState); setPage(0); } }, [initialState]);
  const rows = learningRows(s),
    scoped = rows.filter((i) => (!node || node.id === i.node) && (!specialist || specialist.id === i.specialist) &&
      `${i.title} ${i.body || ""}`.toLowerCase().includes(query.toLowerCase())),
    filtered = scoped.filter((i) => state === "all" || state === i.state);
  const { page, size, setPage, setSize, start, end } = usePage(filtered.length, JSON.stringify([query, node?.id, specialist?.id, state]));
  const title = (id: string) => list(s.nodes).find((n) => n.id === id)?.title || "Source project not recorded";
  const reset = () => { setQuery(""); setNode(null); setSpecialist(null); setState("all"); setPage(0); };
  const first = rows.find((r) => r.state === "proposed" && !r.specialist) || rows[0];
  return (
    <><ScreenAction active={active}>{first ? <ProposalReader item={first} primary /> : <Reader title="Shared knowledge" text="Record a lesson from a finished job to start your library. New proposals appear here for review." trigger="Start your library" primary />}</ScreenAction>
    <section className="surface learning-layout list-surface" aria-label="Shared knowledge">
      <aside className="filter-panel">
        <FilterToolbar query={query} onQuery={(v) => { setQuery(v); setPage(0); }} placeholder="Search learnings" />
        <RecordPicker s={s} kind="node" label="Project" value={node} onChange={(v) => { setNode(v); setPage(0); }} allLabel="All projects" placeholder="Search projects" />
        <RecordPicker s={s} kind="specialist" label="Specialist" value={specialist} onChange={(v) => { setSpecialist(v); setPage(0); }} allLabel="All specialists" placeholder="Search specialists" />
        <Button variant="outline" disabled={!query && !node && !specialist && state === "all"} onClick={reset}>Clear filters</Button>
      </aside>
      <div className="learning-results">
        <SectionTabs value={state} onValue={(v) => { setState(v); setPage(0); }} tabs={states.map(([id, label]) => ({ id, label, count: scoped.filter((r) => id === "all" || r.state === id).length, body: null }))} />
        <ReviewActions all records={filtered.filter(i => i.state === "proposed" && !i.specialist).map(i => ({kind:"item",id:i.id,node:i.review_node || i.node || "root"}))} />
        <ListFrame total={filtered.length} page={page} size={size} onPage={setPage} onSize={setSize} noun="learning">
        <div className="rows">{filtered.slice(start, end).map((i: any, k) => (
          <DetailRow onOpen={() => setSelected(i.id)} className="row learning" data-item key={i.id + ":" + i.specialist + k}>
            <div className="row-main">
              <strong className="row-title"><Text>{i.title || "Untitled learning"}</Text></strong>
              <small>{i.kind || "Library item"} · {title(i.node)} · {i.provenance?.time ? time(i.provenance.time, s.brain?.tz) : "date not recorded"}{i.provenance?.owners?.length ? " · " + i.provenance.owners.join(", ") : ""}</small>
            </div>
            <div className="row-actions">
              <Status>{i.state === "active" ? "Accepted" : i.state === "proposed" ? "Proposed" : i.state}</Status>
              <ProposalReader item={i} open={current === i.id} onOpenChange={open => {setSelected(open ? i.id : null); if (!open) setDismissed(item || null);}} />
            </div>
          </DetailRow>
        ))}</div>
        {!filtered.length && <Empty text="No learnings match." action="Clear the filters or record a lesson from a finished job." />}
        </ListFrame>
        {item && !filtered.slice(start, end).some(i => i.id === item) && rows.filter(i => i.id === item).map(i => <ProposalReader key={i.id} item={i} open={current === i.id} onOpenChange={open => {setSelected(open ? i.id : null);if (!open) setDismissed(item);}} />)}
      </div>
    </section></>
  );
}
