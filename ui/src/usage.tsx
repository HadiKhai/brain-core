import { Progress } from "@/components/ui/progress";
import {
  PageSection,
  PagedCollection,
  Reader,
  ScreenAction,
  Status,
  Empty,
  Bars,
  DataTable,
  Text,
  Tip,
} from "./shared";
import {
  hasMeasuredUsage,
  list,
  jobTitle,
  windowState,
  routing,
  time,
  tokens,
  count,
  type RecordData,
} from "./model";
import { totalTokens } from "./agents";
function WindowMeter({
  name,
  w,
  row,
  policy,
  zone,
}: {
  name: string;
  w?: RecordData;
  row: RecordData;
  policy: RecordData;
  zone?: string;
}) {
  const v = windowState(w, row, policy),
    remaining = w?.resets_at
      ? Math.max(0, Math.ceil((w.resets_at - Date.now() / 1000) / 60))
      : null;
  return (
    <div className="window-meter">
      <div className="meter-heading">
        <strong>{name}</strong>
        <span className="tabular-nums">
          {v.known ? w?.used_percent + "%" : w?.absent ? "None" : "Unknown"}
        </span>
      </div>
      <div
        className={
          "meter " +
          (v.stale
            ? "stale"
            : v.state === "At stop line"
              ? "danger"
              : v.state === "Near stop line"
                ? "warning"
                : "")
        }
      >
        <Progress value={v.percent} aria-label={name + " · " + v.state} />
        {Number.isFinite(policy.stop_percent) && (
          <Tip text={`Stop at ${policy.stop_percent}%`}>
            <i
              className="stop-line"
              style={{
                left: Math.min(100, Math.max(0, policy.stop_percent)) + "%",
              }}
            />
          </Tip>
        )}
      </div>
      <Status>{v.state}</Status>
      <small>
        {w?.resets_at || w?.reset_time
          ? time(w.reset_time || w.resets_at, zone) +
            (remaining != null
              ? " · " +
                (remaining >= 1440
                  ? `resets in ${Math.round(remaining / 1440)} d`
                  : `resets in ${Math.floor(remaining / 60)} h ${remaining % 60} m`)
              : "")
          : "Reset not recorded"}
      </small>
      <small>
        {v.age == null ? "Reading age unknown" : `${v.age} min old`} ·{" "}
        {w?.source || row.source || "Source unknown"}
      </small>
    </div>
  );
}
function UsageFlags({ n, r }: { n: RecordData; r: RecordData }) {
  const u = r.usage,
    b = r.budget;
  return (
    <div className="flag-stack">
      {u && n.jobs.median_tokens && u.processed > 2 * n.jobs.median_tokens && (
        <Status>Tokens over twice median</Status>
      )}
      {u && b?.tokens && u.processed > b.tokens && (
        <Status>Over token budget</Status>
      )}
      {b?.hours && r.age_h > b.hours && <Status>Over time budget</Status>}
      {u?.compactions >= 3 && (
        <Status>{count(u.compactions, "compaction")}</Status>
      )}
      {u?.subagents && u.sub_share > 0.5 ? (
        <Status>Sub-agents {Math.round(u.sub_share * 100)}%</Status>
      ) : null}
    </div>
  );
}
export function Usage({ s, active }: { s: RecordData; active: boolean }) {
  const p = { ...s.usage_routing_policy, ...s.usage_policy },
    route = routing(s),
    sp = s.spend || {},
    week = sp.since_week;
  const measuredNodes = list(s.nodes).filter(
    (n) =>
      n.spend ||
      list(n.jobs?.rows).some((r) => r.Usage) ||
      list(n.jobs?.open).some((r) => r.usage),
  );
  return (
    <div className="page-stack">
      <ScreenAction active={active}><Reader title="Capacity for the next job" trigger="Check capacity" primary text={route ? `Consider ${route.row.label || "the available subscription"} for new work. Review its current windows and the stop line before starting. Projects with a chosen runtime keep it.` : "No subscription is currently confirmed as available. Refresh the readings before starting more work."} /></ScreenAction>
      <PageSection
        title="Subscription windows"
        description="Account-wide readings · the vertical marker is your stop line"
        action={
          s.usage_other_subscriptions ? (
            <span className="muted">
              {count(s.usage_other_subscriptions, "other subscription")} hidden
            </span>
          ) : undefined
        }
      >
        <div className="usage-intro">
          <p>
            {p.autodetected
              ? "New jobs keep their configured runtime."
              : !Number.isFinite(p.stop_percent)
                ? "Routing unavailable. Run brain usage to inspect subscription configuration."
                : route
                  ? `Unpinned new jobs go to ${route.row.label || "the available subscription"}${route.score[0] ? " · capacity unverified" : ""}. Pinned projects keep their runtime.`
                  : "All subscriptions are at the stop line. Stop starting new jobs."}
          </p>
          <div className="legend">
            <Status>Normal</Status>
            <Status>Near stop line</Status>
            <Status>At stop line</Status>
            <Status>Stale · unverified</Status>
          </div>
        </div>
        {list(s.usage).length ? (
          <PagedCollection name="subscriptions">{list(s.usage).map((row, i) => (
            <article className="subscription" key={row.id || i}>
              <div className="subscription-title">
                <h3>
                  <Text>{row.label || "Subscription"}</Text>
                </h3>
                <p>{row.runtime || "Runtime unknown"}</p>
                <small>
                  Brains: {(row.brains || []).join(", ") || "Not recorded"}
                </small>
                <small>
                  {Number.isFinite(p.stop_percent)
                    ? `Stop at ${p.stop_percent}%`
                    : "Stop line unknown"}
                </small>
                {!p.autodetected &&
                  ["five_hour", "seven_day"].some((k) => {
                    const w = row.windows?.[k];
                    return (
                      w &&
                      !w.absent &&
                      !w.expired &&
                      (!w.resets_at || w.resets_at > Date.now() / 1000) &&
                      w.used_percent >= p.stop_percent
                    );
                  }) && <Status>Skipped for new jobs</Status>}
                {row.note && (
                  <p>
                    <Text>{row.note}</Text>
                  </p>
                )}
              </div>
              <WindowMeter
                name="5-hour"
                w={row.windows?.five_hour}
                row={row}
                policy={p}
                zone={s.brain?.tz}
              />
              <WindowMeter
                name="Weekly"
                w={row.windows?.seven_day}
                row={row}
                policy={p}
                zone={s.brain?.tz}
              />
              {Object.entries(row.windows || {})
                .filter(([k]) => !["five_hour", "seven_day"].includes(k))
                .map(([k, w]: any) => (
                  <WindowMeter
                    key={k}
                    name={
                      "Weekly · " + (w.models?.join(", ") || "Model window")
                    }
                    w={w}
                    row={row}
                    policy={p}
                    zone={s.brain?.tz}
                  />
                ))}
            </article>
          ))}</PagedCollection>
        ) : (
          <Empty
            text="No subscription readings."
            action="Run brain usage to check your subscription setup."
          />
        )}
      </PageSection>
      <PageSection
        title="Spend"
        description="This machine · processed tokens and active time"
      >
        {sp.complete === false && (
          <div className="callout">
            <Status>Partial reading</Status>
            <p>
              {sp.read || 0} of {count(sp.files || 0, "transcript")} counted.
              Totals will update as counting finishes.
            </p>
          </div>
        )}
        {week && (
          <>
            <div className="metric-grid spend-metrics">
              {[
                ["Processed", week.processed],
                ["Fresh input", week.fresh],
                ["Cache read", week.cache_read],
                ["Cache write", week.cache_write],
                ["Output", week.output],
              ].map(([k, v]) => (
                <div className="metric" key={k}>
                  <small>{k} · 7 days</small>
                  <strong>{tokens(v)}</strong>
                </div>
              ))}
            </div>
            <p className="spend-summary">
              {week.active_h} h active · {week.agent_h} agent hours ·{" "}
              {count(week.compactions || 0, "compaction")} ·{" "}
              {Math.round((week.sub_share || 0) * 100)}% of tokens from
              sub-agents
            </p>
          </>
        )}
        {Object.entries(sp.runtimes || {}).length ? (
          Object.entries(sp.runtimes).map(([rt, v]: any) => (
            <div className="runtime-spend" key={rt}>
              <div>
                <h3>{rt}</h3>
                <small>
                  {tokens(
                    Object.values(v.weeks || {})
                      .slice(-1)
                      .map((x: any) => x.tokens)[0],
                  )}{" "}
                  tokens this week
                </small>
              </div>
              <Bars
                label="Last 14 days"
                entries={Object.entries(v.days || {}).map(([key, d]: any) => ({
                  key,
                  ...d,
                }))}
              />
              <Bars
                label="Last 8 weeks · Mondays"
                entries={Object.entries(v.weeks || {}).map(([key, d]: any) => ({
                  key,
                  ...d,
                }))}
              />
            </div>
          ))
        ) : (
          <Empty
            text={
              sp.complete === false
                ? "Counting available usage."
                : "No measured spend yet."
            }
            action="Only subscriptions this brain may read are included."
          />
        )}
      </PageSection>
      <PageSection
        title="Jobs: time and tokens"
        description="Live jobs and measured completions, grouped by project"
      >
        {measuredNodes.length ? (
          <PagedCollection name="projects">{measuredNodes.map((n) => {
            const open = list(n.jobs.open).filter((r) => r.usage),
              measured = list(n.jobs.rows).filter((r) => r.Usage && r.Ended),
              back = n.spend?.total;
            return (
              <div className="ledger-entry" key={n.id}>
                <div className="section-heading">
                  <div>
                    <h3>{n.title || "Project"}</h3>
                    <p>
                      {n.jobs.median_tokens
                        ? `Median ${tokens(n.jobs.median_tokens)} tokens · ${n.jobs.median_active_h} h active · ${count(n.jobs.measured || 0, "measured job")}`
                        : "No merged job measured yet"}
                    </p>
                    {back && (
                      <p>
                        All past sessions: {tokens(back.processed)} tokens ·{" "}
                        {back.active_h} h active ·{" "}
                        {count(back.compactions || 0, "compaction")} ·
                        sub-agents {Math.round((back.sub_share || 0) * 100)}%
                      </p>
                    )}
                  </div>
                </div>
                <Bars
                  label="Finished jobs · processed tokens"
                  entries={measured.slice(-24).map((r) => ({
                    key: jobTitle(r.Job),
                    tokens: totalTokens(r),
                    active_h: r["Active h"],
                  }))}
                />
                <DataTable
                  name="jobs"
                  rows={[
                    ...open.map((r) => ({ ...r, _live: true })),
                    ...measured.slice().reverse(),
                  ]}
                  columns={[
                    {
                      key: "Job",
                      label: "Job",
                      render: (r) => (
                        <div>
                          <Text>{jobTitle(r.Job)}</Text>
                          <UsageFlags n={n} r={r} />
                        </div>
                      ),
                    },
                    {
                      key: "Hours",
                      label: "Wall / active",
                      render: (r) => (
                        <span>
                          {r._live ? r.age_h : (r.Hours ?? "Unmeasured")} h
                          {r._live ? " · open" : ""}
                          <small>
                            {r.usage?.active_h ?? r["Active h"] ?? "Unmeasured"}{" "}
                            h active
                          </small>
                        </span>
                      ),
                    },
                    {
                      key: "tokens",
                      label: "Tokens / fresh in + out",
                      render: (r) => (
                        <span>
                          {r.usage || hasMeasuredUsage(r)
                            ? tokens(r.usage?.processed ?? totalTokens(r))
                            : "Unmeasured"}
                          <small>
                            {tokens(
                              r.usage
                                ? r.usage.fresh + r.usage.output
                                : (Number(r["Fresh in"]) || 0) +
                                    (Number(r.Output) || 0),
                            )}{" "}
                            fresh in + out
                          </small>
                        </span>
                      ),
                    },
                    {
                      key: "Compactions",
                      label: "Compactions",
                      render: (r) =>
                        r.usage?.compactions ?? r.Compactions ?? "Unmeasured",
                      numeric: true,
                    },
                    {
                      key: "Sub-agents",
                      label: "Sub-agents",
                      render: (r) => (
                        <span>
                          {r.usage?.subagents ?? r["Sub-agents"] ?? 0}
                          <small>
                            {r.usage
                              ? Math.round((r.usage.sub_share || 0) * 100) + "%"
                              : r["Sub share"] || "Share unmeasured"}
                          </small>
                        </span>
                      ),
                    },
                    {
                      key: "Models",
                      label: "Models",
                      render: (r) => (
                        <Text>
                          {r.usage
                            ? Object.keys(r.usage.models || {}).join(", ")
                            : r.Models || "Not recorded"}
                        </Text>
                      ),
                    },
                    {
                      key: "Budget",
                      label: "Budget",
                      render: (r) =>
                        r.budget
                          ? [
                              r.budget.tokens
                                ? tokens(r.budget.tokens) + " tokens"
                                : "",
                              r.budget.hours ? r.budget.hours + " h" : "",
                            ]
                              .filter(Boolean)
                              .join(" · ")
                          : r.Budget || "Not recorded",
                    },
                  ]}
                />
              </div>
            );
          })}</PagedCollection>
        ) : (
          <Empty
            text="No jobs measured yet."
            action="Jobs are measured when they close. Use brain spend to inspect past sessions."
          />
        )}
      </PageSection>
    </div>
  );
}
