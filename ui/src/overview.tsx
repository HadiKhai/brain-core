import type React from 'react';
import {
  Activity,
  ArrowUpRight,
  BookOpen,
  CheckCheck,
  FolderGit2,
  GitBranch,
  GitCommitHorizontal,
  Inbox,
  Layers,
  Clock3,
  Server,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  PageSection,
  Empty,
  Status,
  Text,
  Disclosure,
  Tip,
  Bars,
} from "./shared";
import {
  flight,
  treeRows,
  list,
  count,
  time,
  tokens,
  safeHref,
  label,
  type RecordData,
} from "./model";
export function Overview({
  s,
  onNode,
  onView,
}: {
  s: RecordData;
  onNode: (id: string) => void;
  onView: (id: string) => void;
}) {
  const nodes = list(s.nodes),
    jobs = flight(nodes),
    naming = (id: string) =>
      nodes.find((n) => n.id === id)?.title || "Workspace";
  const metrics = [
    {
      icon: Inbox,
      label: "Needs you",
      value: list(s.attention).length,
      note:
        count(new Set(list(s.attention).map((a) => a.node)).size, "project") +
        " awaiting attention",
    },
    {
      icon: GitBranch,
      label: "Jobs in flight",
      value: jobs.filter((j) => j.job.state !== "Worktree only").length,
      note: count(
        jobs.filter((j) => j.job.state === "Worktree only").length,
        "other worktree",
      ),
    },
    {
      icon: Layers,
      label: "Worktrees",
      value: nodes.reduce((a, n) => a + list(n.worktrees).length, 0),
      note: count(
        nodes
          .flatMap((n) => list(n.worktrees))
          .filter((w) => list(w.listening).length).length,
        "listening locally",
      ),
    },
    {
      icon: GitCommitHorizontal,
      label: "Commits · 24 hours",
      value: s.commits_24h || 0,
      note: `${s.commits_total || 0} across ${s.hours || 72} hours`,
    },
  ];
  const feed: RecordData[] = [
    ...list(s.commits).map((c) => ({
      ...c,
      text: c.subject,
      kind: c.where === "home" ? "Home commit" : "Brain commit",
    })),
    ...nodes.flatMap((n) =>
      list(n.decisions).map((d) => ({
        ...d,
        node: n.id,
        text: d.title,
        kind: "Decision",
      })),
    ),
  ].sort((a: RecordData, b: RecordData) => (b.t || 0) - (a.t || 0));
  return (
    <div className="page-stack">
      <div className="metric-grid">
        {metrics.map((m) => (
          <div className="metric" key={m.label}>
            <div className="metric-label">
              <m.icon size={17} />
              {m.label}
            </div>
            <strong>{m.value}</strong>
            <p>{m.note}</p>
          </div>
        ))}
      </div>
      <div className="overview-split">
        <PageSection
          title="Inbox"
          description="Decisions and questions for you"
          action={<span className="count-badge">{list(s.inbox).length}</span>}
        >
          {list(s.inbox).length ? (
            list(s.inbox).map((a, i) => (
              <div className="request-row" key={a.id || i}>
                <span className="icon-tile amber">
                  <Inbox size={17} />
                </span>
                <div>
                  <Button
                    variant="ghost"
                    className="text-link"
                    onClick={() => onNode(a.node)}
                  >
                    {naming(a.node)}
                  </Button>
                  <p>
                    <Text zone={s.brain?.tz}>{a.text}</Text>
                  </p>
                  <small>
                    {a.first_seen
                      ? "First seen " + time(a.first_seen, s.brain?.tz)
                      : "Awaiting your response"}
                  </small>
                </div>
              </div>
            ))
          ) : (
            <Empty
              text="Nothing needs you."
              action="Questions from your projects will arrive here."
            />
          )}
        </PageSection>
        <PageSection
          title="Due"
          description="Next actions for coordinators"
          action={<span className="count-badge">{list(s.due).length}</span>}
        >
          {list(s.due).length ? (
            Object.entries(Object.groupBy(list(s.due), (a) => a.node)).map(
              ([id, items]) => (
                <Disclosure
                  key={id}
                  title={naming(id) + " · " + items!.length}
                  initial
                >
                  {items!.map((a, i) => (
                    <div className="due-row" key={i}>
                      <Status>
                        {a.ready
                          ? "Ready to merge"
                          : (
                              {
                                handback: "Hand back",
                                "stale-status": "Update status",
                                "lesson-outcome": "Record lessons",
                                inbox: "Inbox",
                              } as Record<string, string>
                            )[a.kind] || a.kind}
                      </Status>
                      <Text zone={s.brain?.tz}>{a.text}</Text>
                      {list(a.reasons).length > 0 && (
                        <small>{a.reasons.join(" · ")}</small>
                      )}
                    </div>
                  ))}
                </Disclosure>
              ),
            )
          ) : (
            <Empty
              text="All caught up."
              action="Hand-backs and status checks will appear here."
              icon={CheckCheck}
            />
          )}
        </PageSection>
      </div>
      <PageSection
        title="Projects"
        description="Your workspace, connected"
        action={
          <span className="count-badge">
            {nodes.filter((n) => !n.archived).length}
          </span>
        }
      >
        <div className="project-tree">
          {treeRows(nodes).map(({node:n,depth}) => (
              <Button
                variant="ghost"
                className={"project-node " + (n.parent ? "child-node" : "")}
                style={{"--tree-depth":Math.min(depth,4)} as React.CSSProperties}
                key={n.id}
                onClick={() => onNode(n.id)}
              >
                <span className="icon-tile">
                  <FolderGit2 size={19} />
                </span>
                <span className="grow">
                  <strong>{n.title || "Untitled project"}</strong>
                  <small>
                    {n.parent ? naming(n.parent) : "Workspace root"} ·{" "}
                    {n.updated_ago || "Update time not recorded"}
                  </small>
                  <small>
                    {count(
                      flight([n]).filter((j) => j.job.state !== "Worktree only")
                        .length,
                      "active job",
                    )}{" "}
                    ·{" "}
                    {list(n.worktrees).filter((w) => list(w.listening).length).length + " listening"}{" "}
                    ·{" "}
                    {list(s.attention).filter((a) => a.node === n.id).length + " for you"}
                  </small>
                </span>
                <Tip
                  text={`${n.runtime || "Runtime not recorded"} · Owners: ${(n.owners || []).join(", ") || "not recorded"} · ${n.updated ? "Updated " + time(n.updated, s.brain?.tz) : "Update time not recorded"}`}
                >
                  <Status>
                    {n.sealed
                      ? "Sealed"
                      : n.paused
                        ? "Paused"
                        : list(n.blocked).length
                          ? "Needs you"
                          : "Active"}
                  </Status>
                </Tip>
                <ArrowUpRight size={16} />
              </Button>
            ))}
        </div>
      </PageSection>
      <PageSection
        title="Work in flight"
        description="Active jobs and their local worktrees"
        action={
          <Button variant="ghost" onClick={() => onView("agents")}>
            Full job ledger <ArrowUpRight size={14} />
          </Button>
        }
      >
        {jobs.length ? (
          <div className="flight-list">
            <div className="flight-head">
              <span>Project / job</span>
              <span>State</span>
              <span>Local activity</span>
            </div>
            {jobs.map(({ node: n, job: j, wt: w, ledger: l }, i) => (
              <div className="flight-row" key={n.id + j.branch + i}>
                <div className="flight-identity">
                  <span className="icon-tile">
                    <GitBranch size={17} />
                  </span>
                  <div>
                    <Button
                      variant="ghost"
                      className="text-link"
                      onClick={() => onNode(n.id)}
                    >
                      {n.title || "Untitled project"}
                    </Button>
                    <p>{j.branch}</p>
                    <small>
                      {j.runtime || l?.Runtime || "Runtime not recorded"}{" "}
                      {safeHref(j.link) && (
                        <>
                          ·{" "}
                          <a href={safeHref(j.link)}>
                            Open conversation <ArrowUpRight size={11} />
                          </a>
                        </>
                      )}
                    </small>
                  </div>
                </div>
                <div>
                  <Status>{j.state || "In progress"}</Status>
                  {l && (
                    <small>
                      {Number(l.age_h || 0).toFixed(1)} h ·{" "}
                      {count(Number(l.Reviews || 0), "review")}
                    </small>
                  )}
                  {w?.ready && (
                    <Disclosure
                      title={
                        w.ready.ready
                          ? "Ready packet verified"
                          : "Readiness details"
                      }
                    >
                      <Text>
                        {list(w.ready.reasons).join(" · ") ||
                          "No outstanding checks"}
                      </Text>
                    </Disclosure>
                  )}
                </div>
                <div className="local-activity">
                  {w ? (
                    <>
                      {w.wrote && Date.now() / 1000 - w.wrote < 120 ? (
                        <Status>Writing now</Status>
                      ) : null}
                      <span>
                        <Server size={14} />
                        {w.port
                          ? `${w.port}${w.ports > 1 ? "–" + (w.port + w.ports - 1) : ""}`
                          : "No port assigned"}{" "}
                        · {list(w.listening).length ? "Listening" : "Idle"}
                      </span>
                      <small>
                        {w.dirty ?? "Unknown"} changes · {w.ahead ?? "Unknown"}{" "}
                        ahead
                      </small>
                      <small>
                        {w.wrote
                          ? "Wrote " + time(w.wrote, s.brain?.tz)
                          : w.last
                            ? "Commit " + time(w.last, s.brain?.tz)
                            : "Activity not recorded"}
                      </small>
                    </>
                  ) : (
                    <small>No local worktree</small>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Empty
            text="No jobs in flight."
            action="Start a job from a project coordinator."
            icon={GitBranch}
          />
        )}
      </PageSection>
      <div className="overview-split activity-split">
        <PageSection
          title="Activity"
          description={`Commits by hour · last ${s.hours || 72} hours`}
        >
          <div className="heat-legend">
            <span>
              <i />
              No commits
            </span>
            <span>
              <i className="filled" />
              More commits
            </span>
          </div>
          {Object.entries(s.activity?.counts || {}).length ? (
            Object.entries(s.activity.counts).map(([id, counts]: any) => (
              <div className="heat-row" key={id}>
                <Button
                  variant="ghost"
                  className="text-link"
                  onClick={() => onNode(id)}
                >
                  {naming(id)}
                </Button>
                <div className="heat-grid">
                  {counts.map((v: number, i: number) => (
                    <Tip
                      key={i}
                      text={`${time(s.activity.start + i * 3600, s.brain?.tz)} · ${count(v, "commit")}\n${list(
                        s.commits,
                      )
                        .filter(
                          (c) =>
                            c.node === id &&
                            Math.floor((c.t - s.activity.start) / 3600) === i,
                        )
                        .slice(0, 5)
                        .map((c) => c.subject)
                        .join("\n")}`}
                    >
                      <i
                        style={{ opacity: v ? Math.min(1, 0.3 + v * 0.15) : 1 }}
                        className={v ? "filled" : ""}
                      />
                    </Tip>
                  ))}
                </div>
              </div>
            ))
          ) : (
            <Empty text="No recent commits." icon={Activity} />
          )}
        </PageSection>
        <div className="page-stack">
          <PageSection
            title="Library"
            description={count(s.items?.count || 0, "shared item")}
            action={
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onView("learnings")}
              >
                View all <ArrowUpRight size={14} />
              </Button>
            }
          >
            {list(s.items?.recent).length ? (
              list(s.items.recent).map((i, k) => (
                <div className="compact-row" key={k}>
                  <BookOpen size={16} />
                  <div>
                    <Text>{i.title || "Untitled learning"}</Text>
                    <small>{naming(i.source || i.provenance?.node)}</small>
                  </div>
                </div>
              ))
            ) : (
              <Empty
                text="Your library starts here."
                action="Finish a job and record what it taught you."
              />
            )}
          </PageSection>
          <PageSection title="Schedules" description="Recurring work">
            {list(s.schedules).length ? (
              list(s.schedules).map((a, i) => (
                <div className="compact-row" key={i}>
                  <Clock3 size={16} />
                  <div>
                    <strong>{label(a.id || "Scheduled work")}</strong>
                    <small>
                      {naming(a.node)} · {a.when}
                    </small>
                  </div>
                  <Status>
                    {a.here ? "On this machine" : "Another machine"}
                  </Status>
                </div>
              ))
            ) : (
              <Empty
                text="No schedules yet."
                action="Use brain schedule to plan recurring work."
              />
            )}
          </PageSection>
        </div>
      </div>
      <PageSection
        title="Latest"
        description="Commits and decisions, newest first"
      >
        {feed.length ? (
          <>
            {feed.slice(0, 40).map((r, i) => (
              <div className="feed-row" key={i}>
                <span className="icon-tile">
                  <GitCommitHorizontal size={17} />
                </span>
                <div>
                  <Text>{r.text}</Text>
                  {r.said && (
                    <p>
                      <Text>{r.said}</Text>
                    </p>
                  )}
                  <small>
                    {naming(r.node)} · {r.kind}
                  </small>
                </div>
                <small>{time(r.t, s.brain?.tz)}</small>
              </div>
            ))}
          </>
        ) : (
          <Empty text="No recent activity." />
        )}
      </PageSection>
      <PageSection title="Machine" description="Local capacity and job pace">
        <div className="machine-grid">
          {[
            [
              "Processor load",
              s.machine?.load?.[0],
              `${s.machine?.cpus || "Unknown"} cores · 5 min ${s.machine?.load?.[1] ?? "unknown"}`,
            ],
            [
              "Memory free",
              s.machine?.mem_free_gb,
              `of ${s.machine?.mem_total_gb ?? "unknown"} GB`,
            ],
            [
              "Disk free",
              s.machine?.disk_free_gb,
              `of ${s.machine?.disk_total_gb ?? "unknown"} GB`,
            ],
            [
              "Tokens · 7 days",
              tokens(s.spend?.since_week?.processed),
              "processed",
            ],
          ].map(([name, value, note]) => (
            <div key={name}>
              <small>{name}</small>
              <strong>
                {value ?? "Unknown"} <small>{note}</small>
              </strong>
            </div>
          ))}
        </div>
        <div className="compact-row">
          {nodes
            .filter((n) => n.jobs?.median_hours != null)
            .map((n) => (
              <Tip key={n.id} text={`${n.title}: ${n.jobs.merged} merged jobs`}>
                <span>
                  {n.title} · median {n.jobs.median_hours} h
                </span>
              </Tip>
            ))}
        </div>
      </PageSection>
    </div>
  );
}
