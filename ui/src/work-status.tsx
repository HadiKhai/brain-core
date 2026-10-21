import { Status } from "./shared";
import { jobState, list, nowOf, stamp, type RecordData } from "./model";

export function workStatus(item: RecordData, s: RecordData, ledger?: RecordData) {
  const state = jobState(item.state || item.status || ledger?.Outcome);
  const now = nowOf(s);
  const since = item.blocked_since || item.since || item.first_seen || ledger?.Started || item.started;
  const age = item.age_hours ?? ledger?.age_h;
  const hours = age !== undefined && age !== null && Number.isFinite(Number(age)) ? Number(age) : Number.isFinite(stamp(since)) ? Math.max(0, (now - stamp(since)) / 3600_000) : null;
  const waiting = typeof item.waiting_on === "string" ? item.waiting_on.trim() : "";
  const operator = item.waiting_on_operator === true || /\boperator\b/i.test(waiting);
  // A blocked state is not itself a failure: require an actual failure record.
  const fault = !!item.fault || list(item.failures).length > 0;
  const active = !/^(done|merged|closed|abandoned|cancelled)$/i.test(state.kind);
  const stale = active && hours !== null && hours >= 8;
  const kind = fault ? "Blocked by fault" : operator ? "Waiting on you" : waiting ? "Waiting on " + waiting : state.kind;
  const ageLabel = hours === null ? "" : (hours < 1 ? Math.max(1, Math.floor(hours * 60)) + " min" : Math.floor(hours) + " h") + (/blocked|waiting/i.test(kind) ? " waiting" : " open");
  return { ...state, kind, stale, age: ageLabel, waiting, fault };
}

export function WorkStatus({ item, s, ledger }: { item: RecordData; s: RecordData; ledger?: RecordData }) {
  const status = workStatus(item, s, ledger);
  return <><Status>{status.kind}</Status>{status.stale && <Status>Stale</Status>}{status.age && <span className="tabular-nums">{status.age}</span>}</>;
}
