import { Progress } from "@/components/ui/progress";
import {
  PageSection,
  SectionBodyRow,
  PagedCollection,
  Reader,
  Preview,
  ScreenAction,
  Status,
  Empty,
  Bars,
  DataTable,
  Text,
  Tip,
} from "./shared";
import {
  list,
  machineHealth,
  windowState,
  usageStopPercent,
  routing,
  time,
  tokens,
  count,
  type RecordData,
} from "./model";
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
  const stop = usageStopPercent(row, policy);
  const v = windowState(w, row, policy),
    remaining = w?.resets_at
      ? Math.max(0, Math.ceil((w.resets_at - Date.now() / 1000) / 60))
      : null;
  const reset = w?.resets_at || w?.reset_time
    ? remaining != null ? (remaining >= 1440 ? `resets in ${Math.round(remaining / 1440)} d` : `resets in ${Math.floor(remaining / 60)} h ${remaining % 60} m`) : "reset " + time(w.reset_time || w.resets_at, zone)
    : "reset not recorded";
  return (
    <div className="window-meter">
      <div className="meter-heading">
        <strong>{name}</strong>
        <span className="tabular-nums meter-value">{v.known ? w?.used_percent + "%" : w?.absent ? "None" : "Unknown"}</span>
      </div>
      <div className={"meter " + (v.stale ? "stale" : v.state === "At stop line" ? "danger" : v.state === "Near stop line" ? "warning" : "")}>
        <Progress value={v.percent} aria-label={name + " · " + v.state} />
        {Number.isFinite(stop) && (
          <Tip text={`Stop at ${stop}%`} style={{ left: Math.min(100, Math.max(0, stop)) + "%" }}><i className="stop-line" /></Tip>
        )}
      </div>
      <Tip text={`${w?.resets_at || w?.reset_time ? "Resets " + time(w.reset_time || w.resets_at, zone) : "Reset not recorded"} · ${v.age == null ? "reading age unknown" : `read ${v.age} min ago`} · ${w?.source || row.source || "source unknown"}`}>
        <small className="meter-foot">{v.state !== "Normal" && <Status>{v.state}</Status>}<span>{reset} · {v.age == null ? "age unknown" : `${v.age} min old`}</span></small>
      </Tip>
    </div>
  );
}
export function Usage({ s, active }: { s: RecordData; active: boolean }) {
  const p = { ...s.usage_routing_policy, ...s.usage_policy },
    route = routing(s),
    sp = s.spend || {},
    week = sp.since_week;
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
            <article className="subscription" data-item key={row.id || i}>
              <div className="subscription-title">
                <div className="row-line"><h3><Text>{row.label || "Subscription"}</Text></h3>{!p.autodetected && ["five_hour", "seven_day"].some((k) => { const w = row.windows?.[k]; return w && !w.absent && !w.expired && (!w.resets_at || w.resets_at > Date.now() / 1000) && w.used_percent >= usageStopPercent(row, p); }) && <Status>Skipped for new jobs</Status>}</div>
                {row.note ? <small className="row-note clamp-1"><Text>{row.note}</Text></small> : <small>{row.runtime || "Runtime unknown"}{(row.brains || []).length ? " · " + row.brains.join(", ") : ""}</small>}
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
              <div className="row-actions">
                <Reader title={row.label || "Subscription reading"} trigger="Inspect reading" text={[row.note || "", `Read ${time(row.read_at, s.brain?.tz)} · ${row.source || "source unknown"}`, `Brains: ${(row.brains || []).join(", ") || "Not recorded"} · ${Number.isFinite(usageStopPercent(row, p)) ? `stop at ${usageStopPercent(row, p)}%` : "stop line unknown"}`, ...Object.entries(row.windows || {}).map(([name, w]: any) => `${name === "five_hour" ? "5-hour" : "Weekly"}${w.models ? " · " + w.models.join(", ") : ""}: ${w.absent ? "None" : `${w.used_percent ?? "Unknown"}%`} · ${windowState(w, row, p).state} · resets ${time(w.resets_at, s.brain?.tz)}`)].filter(Boolean).join("\n\n")} />
              </div>
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
            <SectionBodyRow><p className="spend-summary">
              {week.active_h} h active · {week.agent_h} agent hours ·{" "}
              {count(week.compactions || 0, "compaction")} ·{" "}
              {Math.round((week.sub_share || 0) * 100)}% of tokens from
              sub-agents
            </p></SectionBodyRow>
          </>
        )}
        {Object.entries(sp.runtimes || {}).length ? (
          Object.entries(sp.runtimes).map(([rt, v]: any) => (
            <div className="runtime-spend" key={rt}>
              <div>
                <h3>{rt}</h3>
                <Reader title={`${rt} usage`} trigger="Inspect usage" text={Object.entries(v.weeks || {}).map(([when,d]: any) => `${when}: ${tokens(d.tokens)} tokens · ${d.active_h || 0} active hours`).join("\n\n")} />
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
      <PageSection title="Machine" description="Local capacity for the next job">
        <div className="machine-grid">
          {[
            ["Processor load", s.machine?.load?.[0], `${s.machine?.cpus || "Unknown"} cores · 5 min ${s.machine?.load?.[1] ?? "unknown"}`],
            ["Memory free", s.machine?.mem_free_gb, `of ${s.machine?.mem_total_gb ?? "unknown"} GB`],
            ["Disk free", s.machine?.disk_free_gb, `of ${s.machine?.disk_total_gb ?? "unknown"} GB`],
          ].map(([name, value, note]) => (
            <div className="kpi" data-kpi key={name}><span className="kpi-label">{name}</span><strong className="kpi-value">{value ?? "Unknown"}</strong><span className="kpi-note">{note}</span></div>
          ))}
        </div>
        <div className="machine-health">
          {machineHealth(s.machine || {}).map((h) => (
            <div className={h.state === "Critical pressure" ? "health-critical" : h.state === "Near capacity" ? "health-warning" : ""} key={h.name}>
              <div className="meter-heading"><small>{h.name}</small><Status>{h.state}</Status></div>
              <Progress value={h.percent} aria-label={h.name + " · " + h.state} />
            </div>
          ))}
        </div>
        {list(s.nodes).some((n) => n.jobs?.median_hours != null) && <p className="pace-list">Job pace: {list(s.nodes).filter((n) => n.jobs?.median_hours != null).map((n) => `${n.title} · median ${n.jobs.median_hours} h`).join(" · ")}</p>}
      </PageSection>
    </div>
  );
}
