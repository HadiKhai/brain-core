import { useContext } from "react";
import { Reader, ActionLink, TechnicalDetails, TimeZoneContext } from "./shared";
import { list, safeHref, time, type RecordData } from "./model";

export function JobReview({ node, job, primary = false, trigger = "Check job" }: { node: RecordData; job: RecordData; primary?: boolean; trigger?: string }) {
  const zone = useContext(TimeZoneContext);
  const branch = job.branch || job.Job;
  const local = list(node.worktrees).find(w => w.branch === branch);
  const ledger = list(node.jobs?.open).find(r => r.Job === branch) || list(node.jobs?.rows).find(r => r.Job === branch);
  const conversation = safeHref(job.link) || safeHref(node.coordinator);
  const text = `## Current state\n${job.state || ledger?.Outcome || "In progress"}\n\n## Review\n${local?.ready?.ready ? "Review checks passed." : list(local?.ready?.reasons).join("\n\n") || "No review result recorded yet."}\n\n## Local activity\n${local ? `${local.dirty ?? "Unknown"} changed files · ${local.ahead ?? "Unknown"} commits ahead` : "No local working copy"}\n\n## Time and reviews\n${ledger?.age_h ?? ledger?.Hours ?? "Unmeasured"} hours · ${ledger?.Reviews ?? "No"} reviews\n\nStarted ${time(ledger?.Started, zone)}${ledger?.Cause ? `\n\n${ledger.Cause}` : ""}`;
  return <Reader title={node.title || "Job review"} text={text} trigger={trigger} primary={primary}
    footer={conversation ? <ActionLink href={conversation}>Continue in conversation</ActionLink> : <p>No conversation is linked. Ask the project coordinator to resume this work.</p>}>
    <TechnicalDetails values={{Job:branch}} />
  </Reader>;
}
