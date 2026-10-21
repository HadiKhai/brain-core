import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Disclosure } from "./shared";

export type ReviewRecord = { kind: "item" | "specialist"; id: string; node: string };
type Proposal = ReviewRecord & { title: string; scope: string[]; revision: string };
type Prepared = { token: string; action: string; records: Proposal[]; reviewer: string };
type Receipt = { count: number; reviewer: string; commit: string; changes: { path: string; diff: string }[] };
const ReviewContext = createContext<{ enabled: boolean; proposals: Proposal[]; busy: boolean; prepare: (action: string, records: Proposal[]) => void }>({ enabled: false, proposals: [], busy: false, prepare() {} });
const identity = (r: ReviewRecord) => JSON.stringify([r.kind, r.node, r.id]);
const key = () => new URLSearchParams(location.search).get("k") || "";
async function request(path: string, payload?: unknown) {
  const response = await fetch(path + "?k=" + encodeURIComponent(key()), payload === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json", "X-Brain-Review": key() }, body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Review request failed. Refresh and try again.");
  return result;
}
export function ReviewProvider({ enabled, catalogKey, children }: { enabled: boolean; catalogKey: string; children: ReactNode }) {
  const [proposals, setProposals] = useState<Proposal[]>([]), [revision, setRevision] = useState(0);
  // Own the confirmation and receipt above live row updates: accepted rows can disappear immediately.
  const [prepared, setPrepared] = useState<Prepared | null>(null), [receipt, setReceipt] = useState<Receipt | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    request("/api/review").then(r => { if (active) setProposals(r.proposals || []); }).catch(() => { if (active) setProposals([]); });
    return () => { active = false; };
  }, [enabled, revision, catalogKey]);
  const prepare = async (action: string, selected: Proposal[]) => {
    if (busy) return;
    setBusy(true); setError(""); setReceipt(null);
    try { setPrepared(await request("/api/review/prepare", { action, records: selected.map(({ kind, id, node }) => ({ kind, id, node })) })); }
    catch (e) { setError(e instanceof Error ? e.message : "Review unavailable."); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    if (!prepared || busy) return;
    setBusy(true); setError("");
    try { setReceipt(await request("/api/review/apply", { token: prepared.token })); setPrepared(null); }
    catch (e) { setError(e instanceof Error ? e.message : "Review failed."); setPrepared(null); }
    finally { setBusy(false); setRevision(v => v + 1); }
  };
  const close = () => { if (!busy) { setPrepared(null); setReceipt(null); setError(""); } };
  const groups = new Map<string, number>();
  prepared?.records.forEach(r => { const k = r.node + " · scope: " + (r.scope.join(", ") || "none recorded"); groups.set(k, (groups.get(k) || 0) + 1); });
  return <ReviewContext.Provider value={{ enabled, proposals: enabled ? proposals : [], busy, prepare }}>
    {children}
    <Dialog open={!!prepared || !!receipt || !!error} onOpenChange={open => { if (!open) close(); }}>
      <DialogContent className="review-dialog" showCloseButton={!busy}>
        <DialogHeader><DialogTitle>{error ? "Review needs attention" : receipt ? "Review recorded" : prepared?.action === "reject" ? "Reject these proposals?" : "Approve these proposals?"}</DialogTitle><DialogDescription>{receipt ? "These changes were recorded in your brain." : error ? "Refresh and review the current proposals again." : `Review as ${prepared?.reviewer || "the operator"} via the local UI. Each proposal keeps its current scope.`}</DialogDescription></DialogHeader>
        <div className="review-confirm-scroll">
          {error && <p role="alert">{error}</p>}
          {prepared && <><ul>{[...groups].map(([group, count]) => <li key={group}>{count} · {group}</li>)}</ul><ul>{prepared.records.map(r => <li key={identity(r)}>{r.title} · {r.kind}</li>)}</ul></>}
          {receipt && <><p>{receipt.count} {receipt.count === 1 ? "proposal" : "proposals"} reviewed by {receipt.reviewer} via local UI.</p><p className="verbatim">Commit: {receipt.commit || "No Git repository"}</p>{receipt.changes.map(c => <Disclosure key={c.path} title={c.path + " · exact changes"}><pre className="verbatim">{c.diff}</pre></Disclosure>)}</>}
        </div>
        <div className="flex flex-wrap gap-2">{prepared && !error && <Button disabled={busy} onClick={apply}>{busy ? "Recording…" : prepared.action === "reject" ? `Confirm reject (${prepared.records.length})` : `Confirm approve (${prepared.records.length})`}</Button>}<Button variant="outline" disabled={busy} onClick={close}>{receipt || error ? "Done" : "Cancel"}</Button></div>
      </DialogContent>
    </Dialog>
  </ReviewContext.Provider>;
}
export function ReviewActions({ records, all = false }: { records?: ReviewRecord[]; all?: boolean }) {
  const context = useContext(ReviewContext);
  if (!context.enabled) return null;
  const selected = records ? context.proposals.filter(r => records.some(w => identity(r) === identity(w))) : context.proposals;
  if (!selected.length) return null;
  return <div className="review-actions flex flex-wrap gap-2"><Button disabled={context.busy} onClick={() => context.prepare("accept", selected)}>{all ? `Approve all (${selected.length})` : "Accept"}</Button>{!all && <Button variant="outline" disabled={context.busy} onClick={() => context.prepare("reject", selected)}>Reject</Button>}</div>;
}
