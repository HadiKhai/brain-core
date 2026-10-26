import { DataTable, Empty } from "./shared";
import { list, tokens, type RecordData } from "./model";
import "./throughput.css";

export type ThroughputDay = {
  date: string;
  merged: number;
  merges_per_hour: number | null;
  minutes_per_merge: number | null;
  measured_merges: number;
  tokens_per_merge: number | null;
  verify_runs: number;
  verify_reused: number;
  reruns: number;
  verify_measured_jobs: number;
  queue_seconds: number | null;
  queue_measured_jobs: number;
};

const decimal = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "Unmeasured";

export function Throughput({ nodes, embedded = false }: { nodes: RecordData[]; embedded?: boolean }) {
  const rows = nodes.flatMap(node => list(node.jobs?.throughput).map((day): RecordData => ({
    ...day, project: node.title || node.id || "Project", _key: `${node.id}:${day.date}`,
  }))).sort((a, b) => String(b.date).localeCompare(String(a.date)) || String(a.project).localeCompare(String(b.project)));
  return <section className={`daily-throughput ${embedded ? "pb-4" : "surface"}`} aria-label="Daily throughput">
    {embedded ? <small>Last 14 days · measured merges</small> : <div className="surface-head"><h2>Daily throughput</h2><small>Last 14 days · measured merges</small></div>}
    <p>Calendar-day rates; today uses elapsed hours. Tokens and admission waits show their measurement coverage. Other waiting causes are unmeasured.</p>
    {rows.length ? <DataTable name="throughput-days" rows={rows} details={false} columns={[
      { key: "date", label: "Day" },
      { key: "project", label: "Project" },
      { key: "merged", label: "Merged", numeric: true },
      { key: "merges_per_hour", label: "Merges / hour", numeric: true, render: row => decimal(row.merges_per_hour) },
      { key: "minutes_per_merge", label: "Minutes / merge", numeric: true, render: row => decimal(row.minutes_per_merge) },
      { key: "tokens_per_merge", label: "Tokens / merge", numeric: true, render: row => <span>{row.tokens_per_merge == null ? "Unmeasured" : tokens(row.tokens_per_merge)}<small>{row.measured_merges} / {row.merged} merges measured</small></span> },
      { key: "verify_runs", label: "Verification", numeric: true, render: row => <span>{row.verify_measured_jobs ? `${row.verify_runs} runs · ${row.reruns} reruns · ${row.verify_reused} reused` : "Unmeasured"}<small>{row.verify_measured_jobs} / {row.merged} jobs measured</small></span> },
      { key: "queue_seconds", label: "Admission wait", numeric: true, render: row => <span>{row.queue_seconds == null ? "Unmeasured" : `${decimal(row.queue_seconds)} s`}<small>{row.queue_measured_jobs} / {row.merged} jobs measured</small></span> },
    ]} /> : <Empty text="No recorded merges in the last 14 days." action="Daily rates appear as jobs land." />}
  </section>;
}
