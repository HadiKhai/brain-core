import { useContext } from "react";
import { Reader, TechnicalDetails, TimeZoneContext } from "./shared";
import { list, time, type RecordData } from "./model";
import { ThreadLink } from "./thread-link";
import { workStatus } from "./work-status";

export function JobReview({ node, job, primary = false, trigger = "Check job", open, onOpenChange }: { node: RecordData; job: RecordData; primary?: boolean; trigger?: string; open?: boolean; onOpenChange?: (open: boolean) => void }) {
  const zone = useContext(TimeZoneContext);
  const branch = job.branch || job.Job;
  const local = list(node.worktrees).find(w => w.branch === branch);
  const ledger = list(node.jobs?.open).find(r => r.Job === branch) || list(node.jobs?.rows).find(r => r.Job === branch);
  const conversation = job.link || node.coordinator;
  const status = workStatus(job, {}, ledger);
  const text = `## Current state\n${[status.kind, status.stale ? "Stale" : "", status.age].filter(Boolean).join(" · ")}${status.description ? "\n\n" + status.description : ""}\n\n## Review\n${local?.ready?.ready ? "Review checks passed." : list(local?.ready?.reasons).join("\n\n") || "No review result recorded yet."}\n\n## Local activity\n${local ? `${local.dirty ?? "Unknown"} changed files · ${local.ahead ?? "Unknown"} commits ahead` : "No local working copy"}\n\n## Time and reviews\n${ledger?.age_h ?? ledger?.Hours ?? "Unmeasured"} hours · ${ledger?.Reviews ?? "No"} reviews\n\nStarted ${time(ledger?.Started, zone)}${ledger?.Cause ? `\n\n${ledger.Cause}` : ""}`;
  return <Reader title={node.title || "Job review"} text={text} trigger={trigger} primary={primary} open={open} onOpenChange={onOpenChange}
    footer={conversation ? <ThreadLink href={conversation} label="Continue in conversation" /> : <p>No conversation is linked. Ask the project coordinator to resume this work.</p>}>
    <TechnicalDetails values={{Job:branch}} />
  </Reader>;
}
