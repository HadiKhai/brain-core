import { useState } from "react";
import { BookOpen, ShieldCheck } from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  PageSection,
  FilterToolbar,
  Choice,
  Empty,
  Status,
  Text,
  Reader,
  PagedCollection,
  ScreenAction,
  TechnicalDetails,
  Command,
} from "./shared";
import { learningRows, list, quote, time, type RecordData } from "./model";
function ProposalReader({ item: i, primary = false }: { item: RecordData; primary?: boolean }) {
  const [reviewer, setReviewer] = useState("");
  const reviewable = i.state === "proposed" && !i.specialist;
  return <Reader title={i.title || "Learning"} text={i.body || "No additional text recorded."} trigger={reviewable ? "Review proposal" : "Read learning"} primary={primary}
    footer={reviewable ? <div className="decision-actions"><p>Review the source, copy your decision, then run it in your terminal. Copying does not apply it.</p><div className="field"><label>Reviewer name<Input value={reviewer} onChange={e => setReviewer(e.target.value)} placeholder="Your name" aria-label="Reviewer name" /></label></div><Command text={"brain item accept --reviewed-by " + quote(reviewer || "REVIEWER") + " -- " + quote(i.id)} label={reviewer ? "Copy acceptance" : "Add reviewer name"} disabled={!reviewer.trim()} /><Command text={"brain item reject -- " + quote(i.id)} label="Copy rejection" /></div> : undefined}>
    <TechnicalDetails values={{Source:i.provenance?.ref, Repository:i.provenance?.repo, Revision:i.provenance?.commit}} />
  </Reader>;
}
export function Learnings({ s, active }: { s: RecordData; active: boolean }) {
  const [query, setQuery] = useState(""),
    [node, setNode] = useState("all"),
    [specialist, setSpecialist] = useState("all"),
    [state, setState] = useState("all");
  const rows = learningRows(s),
    filtered = rows.filter(
      (i) =>
        (node === "all" || node === i.node) &&
        (specialist === "all" || specialist === i.specialist) &&
        (state === "all" || state === i.state) &&
        `${i.title} ${i.body || ""}`
          .toLowerCase()
          .includes(query.toLowerCase()),
    );
  return (
    <><ScreenAction active={active}>{rows[0] ? <ProposalReader item={rows[0]} primary /> : <Reader title="Shared knowledge" text="Record a lesson from a finished job to start your library. New proposals will appear here for review." trigger="Start your library" primary />}</ScreenAction><PageSection
      title="Shared knowledge"
      description="Library items and specialist lessons · proposed items need human review"
    >
      <div className="learning-layout">
        <aside className="filter-panel">
          <FilterToolbar
            query={query}
            onQuery={(v) => {
              setQuery(v);
            }}
            placeholder="Search learnings"
          />
          <Choice
            name="Project"
            value={node}
            onChange={setNode}
            options={[
              { value: "all", label: "All projects" },
              ...list(s.nodes).map((n) => ({
                value: n.id,
                label: n.title || "Untitled project",
              })),
            ]}
          />
          <Choice
            name="Specialist"
            value={specialist}
            onChange={setSpecialist}
            options={[
              { value: "all", label: "All specialists" },
              ...list(s.specialists).map((sp) => ({
                value: sp.node + "/" + sp.id,
                label: sp.title || "Specialist",
              })),
            ]}
          />
          <Choice
            name="State"
            value={state}
            onChange={setState}
            options={[
              { value: "all", label: "All states" },
              ...["active", "proposed", "rejected", "quarantined"].map((v) => ({
                value: v,
                label: `${v === "active" ? "Accepted" : v[0].toUpperCase() + v.slice(1)} (${rows.filter((r) => r.state === v).length})`,
              })),
            ]}
          />
          <Button
            variant="outline"
            onClick={() => {
              setQuery("");
              setNode("all");
              setSpecialist("all");
              setState("all");
            }}
          >
            Clear filters
          </Button>
          <div className="callout">
            <ShieldCheck size={18} />
            <p>
              Lessons inform the next job. Proposed items stay inactive until a
              person reviews their source.
            </p>
          </div>
        </aside>
        <div className="learning-results">
          <p className="results-meta">
            {filtered.length} {filtered.length === 1 ? "learning" : "learnings"}
          </p>
          <PagedCollection name="learnings">{filtered.map((i: any, k) => (
            <article
              className="learning-entry"
              key={i.id + ":" + i.specialist + k}
            >
              <div className="entry-heading">
                <span className="icon-tile">
                  <BookOpen size={18} />
                </span>
                <div>
                  <h3>
                    <Text>{i.title || "Untitled learning"}</Text>
                  </h3>
                  <small>
                    {i.kind || "Library item"} ·{" "}
                    {list(s.nodes).find((n) => n.id === i.node)?.title ||
                      "Source project not recorded"}
                  </small>
                </div>
                <Status>
                  {i.state === "active"
                    ? "Accepted"
                    : i.state === "proposed"
                      ? "Proposed · awaiting review"
                      : i.state}
                </Status>
              </div>
              <div className="provenance">
                <span>
                  {i.provenance?.time
                    ? time(i.provenance.time, s.brain?.tz)
                    : "Date not recorded"}
                </span>
                {i.provenance?.owners?.length > 0 && (
                  <span>Source owners: {i.provenance.owners.join(", ")}</span>
                )}
              </div>
              <ProposalReader item={i} />
              <TechnicalDetails values={{Source:i.provenance?.ref, Repository:i.provenance?.repo, Revision:i.provenance?.commit}} />
              {i.scope?.length > 0 && (
                <p className="muted">Scope: {i.scope.join(", ")}</p>
              )}
              {i.accepted_at && (
                <small>
                  Accepted {time(i.accepted_at, s.brain?.tz)} by{" "}
                  {i.accepted_by || "reviewer not recorded"}
                </small>
              )}

            </article>
          ))}</PagedCollection>
          {!filtered.length && (
            <Empty
              text="No learnings match."
              action="Clear the filters or record a lesson from a finished job."
            />
          )}

        </div>
      </div>
    </PageSection></>
  );
}
