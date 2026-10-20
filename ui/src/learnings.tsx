import { useState } from "react";
import { BookOpen, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  PageSection,
  FilterToolbar,
  Choice,
  Empty,
  Status,
  Text,
  Markdown,
  Disclosure,
  Command,
} from "./shared";
import { learningRows, list, quote, time, type RecordData } from "./model";
export function Learnings({ s }: { s: RecordData }) {
  const [query, setQuery] = useState(""),
    [node, setNode] = useState("all"),
    [specialist, setSpecialist] = useState("all"),
    [state, setState] = useState("all"),
    [limit, setLimit] = useState(10);
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
    <PageSection
      title="Shared knowledge"
      description="Library items and specialist lessons · proposed items need human review"
    >
      <div className="learning-layout">
        <aside className="filter-panel">
          <FilterToolbar
            query={query}
            onQuery={(v) => {
              setQuery(v);
              setLimit(10);
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
              setLimit(10);
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
          {filtered.slice(0, limit).map((i: any, k) => (
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
                {i.provenance?.ref && <Text>{i.provenance.ref}</Text>}
                {i.provenance?.repo && <Text>{i.provenance.repo}</Text>}
                {i.provenance?.commit && (
                  <span>Commit {String(i.provenance.commit).slice(0, 7)}</span>
                )}
                {i.provenance?.owners?.length > 0 && (
                  <span>Source owners: {i.provenance.owners.join(", ")}</span>
                )}
              </div>
              {i.body && (
                <Disclosure title="Read learning">
                  <Markdown text={i.body} />
                </Disclosure>
              )}
              {i.scope?.length > 0 && (
                <p className="muted">Scope: {i.scope.join(", ")}</p>
              )}
              {i.accepted_at && (
                <small>
                  Accepted {time(i.accepted_at, s.brain?.tz)} by{" "}
                  {i.accepted_by || "reviewer not recorded"}
                </small>
              )}
              {i.state === "proposed" && !i.specialist && (
                <Disclosure title="Review and decide">
                  <p className="muted">
                    Review the source. Replace REVIEWER with the operator or a
                    non-revoked principal, then run your decision in the
                    terminal.
                  </p>
                  <Command
                    text={
                      "brain item accept --reviewed-by 'REVIEWER' -- " +
                      quote(i.id)
                    }
                  />
                  <Command text={"brain item reject -- " + quote(i.id)} />
                </Disclosure>
              )}
            </article>
          ))}
          {!filtered.length && (
            <Empty
              text="No learnings match."
              action="Clear the filters or record a lesson from a finished job."
            />
          )}
          {filtered.length > limit && (
            <Button variant="outline" onClick={() => setLimit(limit + 10)}>
              Show 10 more
            </Button>
          )}
        </div>
      </div>
    </PageSection>
  );
}
