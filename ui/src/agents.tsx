import { JobReview } from "./work";
import { Bot } from "lucide-react";
import {
  PageSection,
  DataTable,
  Status,
  Text,
  Empty,
  Disclosure,
  Bars,
  PagedCollection,
  Reader,
  ScreenAction,
} from "./shared";
import {
  flight,
  jobTitle,
  hasMeasuredUsage,
  list,
  hireRow,
  closed,
  count,
  time,
  tokens,
  type RecordData,
} from "./model";
export const totalTokens = (r: RecordData) =>
  ["Fresh in", "Cache read", "Cache write", "Output"].reduce(
    (a, k) => a + (Number(r[k]) || 0),
    0,
  );
export function Agents({ s, active }: { s: RecordData; active: boolean }) {
  const nodes = list(s.nodes), next = flight(nodes)[0];
  return (
    <div className="page-stack">
      <ScreenAction active={active}>{next ? <JobReview node={next.node} job={next.job} primary trigger="Check active job" /> : <Reader title="Job history" text="No jobs are currently in progress. Browse a completed job below to inspect its outcome and lessons." trigger="Check job history" primary />}</ScreenAction>
      <PageSection
        title="Job ledger"
        description="Every job stays here, including completed and abandoned work"
      >
        {nodes.filter((n) => list(n.jobs?.rows).length).length ? (
          <PagedCollection name="projects">{nodes
            .filter((n) => list(n.jobs?.rows).length)
            .map((n) => (
              <article className="ledger-entry" key={n.id}>
                <div className="section-heading">
                  <div>
                    <h3>{n.title || "Project"}</h3>
                    <p>
                      {count(list(n.jobs.open).length, "open job")} ·{" "}
                      {n.jobs.merged || 0} merged · {n.jobs.abandoned || 0}{" "}
                      abandoned
                    </p>
                  </div>
                  <small>
                    Median {n.jobs.median_hours ?? "unmeasured"} h ·{" "}
                    {n.jobs.median_reviews ?? "unmeasured"} reviews
                  </small>
                </div>
                <Bars
                  unit="hours"
                  label="Finished job durations"
                  entries={list(n.jobs.rows)
                    .filter((r) => Number(r.Hours) > 0)
                    .slice(-24)
                    .map((r) => ({
                      key: jobTitle(r.Job),
                      tokens: r.Hours,
                      active_h: r.Hours,
                    }))}
                />
                <DataTable
                  name="jobs"
                  rows={[...list(n.jobs.rows)]
                    .reverse()
                    .map((r) => ({ ...r, _key: r.Job + r.Started }))}
                  columns={[
                    {
                      key: "Job",
                      label: "Job",
                      render: (r) => (
                        <div>
                          <strong>
                            <Text>{jobTitle(r.Job)}</Text>
                          </strong>
                          {!closed(r) &&
                            Number(
                              n.jobs.open.find(
                                (o: RecordData) =>
                                  o.Job === r.Job && o.Started === r.Started,
                              )?.age_h,
                            ) >
                              2 * n.jobs.median_hours &&
                            n.jobs.median_hours != null && (
                              <Status>Over twice median</Status>
                            )}
                        </div>
                      ),
                    },
                    {
                      key: "Started",
                      label: "Started",
                      render: (r) => time(r.Started, s.brain?.tz),
                    },
                    {
                      key: "Hours",
                      label: "Duration",
                      render: (r) =>
                        closed(r)
                          ? `${r.Hours || "Unmeasured"} h`
                          : `${n.jobs.open.find((o: RecordData) => o.Job === r.Job && o.Started === r.Started)?.age_h ?? "Unknown"} h · open`,
                    },
                    {
                      key: "Usage",
                      label: "Tokens / active",
                      render: (r) => {
                        const u = n.jobs.open.find(
                          (o: RecordData) =>
                            o.Job === r.Job && o.Started === r.Started,
                        )?.usage;
                        return (
                          <span>
                            {u
                              ? tokens(u.processed)
                              : hasMeasuredUsage(r)
                                ? tokens(totalTokens(r))
                                : "Unmeasured"}
                            <small>
                              {u?.active_h ?? r["Active h"] ?? "Unmeasured"} h
                              active
                            </small>
                          </span>
                        );
                      },
                    },
                    { key: "Reviews", label: "Reviews", numeric: true },
                    {
                      key: "Runtime",
                      label: "Runtime",
                      render: (r) => (
                        <span>
                          {r.Runtime || "Unknown"}
                          <small>
                            {r.Subscription || "Subscription not recorded"}
                          </small>
                        </span>
                      ),
                    },
                    {
                      key: "Outcome",
                      label: "Outcome / cause",
                      render: (r) => (
                        <div>
                          <Status>{r.Outcome || "Open"}</Status>
                          {r.Cause && (
                            <p>
                              <Text>{r.Cause}</Text>
                            </p>
                          )}
                        </div>
                      ),
                    },
                    {
                      key: "Lessons",
                      label: "Lessons",
                      render: (r) => <Text>{r.Lessons || "Not recorded"}</Text>,
                    },
                  ]}
                />
              </article>
            ))}</PagedCollection>
        ) : (
          <Empty
            text="No recorded jobs."
            action="Your first job will start this ledger."
          />
        )}
      </PageSection>      <PageSection
        title="Specialists"
        description="The people and agents you can bring into a job"
      >
        {list(s.specialists).length ? (
          <PagedCollection name="specialists">{list(s.specialists).map((sp, i) => (
            <article className="specialist-entry" key={sp.node + sp.id + i}>
              <div className="entry-heading">
                <span className="icon-tile">
                  <Bot size={20} />
                </span>
                <div>
                  <h3>
                    <Text>{sp.title || "Specialist"}</Text>
                  </h3>
                  <small>
                    {sp.runtime || "Runtime not recorded"} ·{" "}
                    {count(list(sp.lessons).length, "lesson")} ·{" "}
                    {nodes.find((n) => n.id === sp.node)?.title || "Workspace"}
                  </small>
                </div>
                <Status>{sp.status || "Active"}</Status>
              </div>
              {sp.scope?.length > 0 && (
                <p className="muted">Scope: {sp.scope.join(", ")}</p>
              )}
              <Reader title={sp.title || "Specialist"} text={[sp.scope?.join(", "), ...list(sp.lessons).map(l => l.text)].filter(Boolean).join("\n\n") || "No role notes recorded yet."} trigger="Read role" />
              {sp.imported && <Status>Imported · review source</Status>}
              {list(sp.jobs).length ? (
                <Disclosure
                  title={count(sp.jobs.length, "recorded hire")}
                  initial
                >
                  <DataTable
                    name="hires"
                    rows={sp.jobs.map((h: RecordData) => {
                      const r = hireRow(h, nodes);
                      return {
                        ...h,
                        reviews: r.Reviews || h.reviews || "Not recorded",
                        result:
                          h.outcome ||
                          (r.Job
                            ? r.Outcome || "Open"
                            : "Outcome not recorded"),
                      };
                    })}
                    columns={[
                      {
                        key: "job",
                        label: "Project / job",
                        render: (h) => (
                          <div>
                            <strong>
                              {nodes.find((n) => n.id === h.node)?.title ||
                                "Project"}
                            </strong>
                            <small>Hired for this project</small>
                          </div>
                        ),
                      },
                      {
                        key: "time",
                        label: "Hired",
                        render: (r) => time(r.time, s.brain?.tz),
                      },
                      {
                        key: "task",
                        label: "Task",
                        render: (r) => (
                          <Text>{r.task || "Task not recorded"}</Text>
                        ),
                      },
                      { key: "reviews", label: "Reviews", numeric: true },
                      {
                        key: "runtime",
                        label: "Runtime / subscription",
                        render: (r) => (
                          <span>
                            {r.runtime || "Unknown"}
                            <small>
                              {r.subscription || "Subscription not recorded"}
                            </small>
                          </span>
                        ),
                      },
                      {
                        key: "result",
                        label: "Outcome",
                        render: (r) => <Status>{r.result}</Status>,
                      },
                    ]}
                  />
                </Disclosure>
              ) : (
                <p className="muted">No recorded hires.</p>
              )}
            </article>
          ))}</PagedCollection>
        ) : (
          <Empty
            text="No specialists yet."
            action="Use brain specialist to add a role for your next job."
            icon={Bot}
          />
        )}
      </PageSection>

    </div>
  );
}
