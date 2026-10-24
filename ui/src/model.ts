// The Python API owns this read-only snapshot. Optional fields stay optional across upgrades.
export type RecordData = Record<string, any>;
export const list = (v: any): RecordData[] => (Array.isArray(v) ? v : []);
export const label = (v: any) => {
  const text = String(v ?? "");
  return /\s/.test(text)
    ? text
    : text.replace(/[_-]/g, " ").replace(/^./, (c) => c.toUpperCase());
};
export const count = (n: number, word: string) =>
  `${n} ${word}${n === 1 ? "" : "s"}`;
export function jobState(value: any): { kind: string; description: string } {
  const text = String(value || "In progress").trim();
  const match = text.match(/^(ready to merge|in progress|worktree only|needs review|needs you|reviewing|review|blocked|paused|working|active|done|merged)(?=$|[\s:·—-])/i);
  if (!match) return { kind: "In progress", description: text === "In progress" ? "" : text };
  return { kind: label(match[1]), description: text.slice(match[1].length).replace(/^[\s:·—-]+/, "") };
}
export const tokens = (n: any) =>
  Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(Number(n) || 0);
export const quote = (s: any) => "'" + String(s).replace(/'/g, "'\\''") + "'";
// Desktop protocol registration alone does not prove a thread-opening route.
// Until a handler is verified, desktop references are copyable text, never anchors.
export const safeHref = (s: any) => {
  try {
    const url = new URL(String(s));
    return /^https?:$/.test(url.protocol) ? String(s) : undefined;
  } catch { return undefined; }
};
// Operational identifiers remain available in explicit technical readers, never as task copy.
export function humanText(value: any): string {
  const links: string[] = [];
  const text = String(value ?? "").replace(/\b(?:https?:|t3-thread:)\/\/[^\s<>"'`)\]]+/gi, link => `\uE000${links.push(link) - 1}\uE001`);
  return text
    .replace(/(?:~\/|(?<![:\w/<])\/)[^\s<>"'`),;]+/g, s => "the project files" + (s.match(/[.!?]+$/)?.[0] || ""))
    .replace(/\bjob\/[\w./-]+/g, s => "this job" + (s.match(/[.!?]+$/)?.[0] || ""))
    .replace(/\b(?:[\w.-]+\/)+[\w.-]+\.(?:md|json|jsonl|txt|html|py|tsx?|jsx?)\b/g, "the source document")
    .replace(/\breview report the project files\b/gi, "Review the report")
    .replace(/\b[a-f0-9]{16,64}\b/gi, "linked record")
    .replace(/\bready packet(?:s)?\b/gi, "review checks")
    .replace(/\bhand[- ]?backs?\b/gi, "review")
    .replace(/\bworktrees?\b/gi, (s) => /s$/.test(s) ? "working copies" : "working copy")
    .replace(/\uE000(\d+)\uE001/g, (original, i) => links[Number(i)] || original);
}

export function time(value: any, zone?: string) {
  if (!value) return "Not recorded";
  const d = new Date(typeof value === "number" ? value * 1000 : value);
  if (!Number.isFinite(+d)) return "Time not recorded";
  try {
    return new Intl.DateTimeFormat("en", {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZone: zone || undefined,
    }).format(d);
  } catch {
    return d.toLocaleString();
  }
}
export function readable(v: any, zone?: string): string {
  return String(v ?? "")
    .replace(
      /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi,
      "Linked conversation",
    )
    .replace(
      /\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})? ?(?:Z|[+-]\d{2}:\d{2})/g,
      (s) => time(s.replace(" ", "T").replace(/ ([+-])/, "$1"), zone),
    )
    .replace(/\b(\d+) ([A-Za-z -]+)\(s\)/g, (_, n, w) => count(Number(n), w))
    .replace(/^(?:inbox:\s*)?(\S.*? from worker:)\s*question:\s*/i, "$1 ");
}
export const closed = (r: RecordData) =>
  !!r.Ended ||
  /^(merged|abandoned|closed|done|cancelled|completed)$/i.test(r.Outcome || "");
export function flight(nodes: RecordData[]): {
  node: RecordData;
  job: RecordData;
  wt: RecordData | undefined;
  ledger: RecordData | undefined;
}[] {
  return nodes.flatMap((n) => {
    const rows = list(n.jobs?.rows),
      open = list(n.jobs?.open);
    const doneBranches = new Set(rows.filter(closed).map((r) => r.Job));
    const works = list(n.work).filter(
      (w) =>
        !/^(closed|merged|done|abandoned|completed|cancelled)(?:\b|\s*[·:—-])/i.test(
          w.state || "",
        ) &&
        (!doneBranches.has(w.branch) || open.some((r) => r.Job === w.branch)),
    );
    const branches = new Set(works.map((w) => w.branch));
    return [
      ...works.map((w) => ({
        node: n,
        job: w,
        wt: list(n.worktrees).find((t) => t.branch === w.branch),
        ledger: open.find((r) => r.Job === w.branch),
      })),
      ...list(n.worktrees)
        .filter((w) => !branches.has(w.branch) && !doneBranches.has(w.branch))
        .map((w) => ({
          node: n,
          job: { branch: w.branch, state: "Worktree only" },
          wt: w,
          ledger: open.find((r) => r.Job === w.branch),
        })),
    ];
  });
}
export function hireRow(hire: RecordData, nodes: RecordData[]) {
  const rows = list(nodes.find((n) => n.id === hire.node)?.jobs?.rows).filter(
    (r) => r.Job === hire.job,
  );
  return (
    (hire.started
      ? rows.find((r) => r.Started === hire.started)
      : rows
          .filter(
            (r) =>
              Date.parse(r.Started) <= Date.parse(hire.time) &&
              (!r.Ended || Date.parse(hire.time) <= Date.parse(r.Ended)),
          )
          .sort((a, b) => Date.parse(b.Started) - Date.parse(a.Started))[0]) ||
    {}
  );
}
export function learningRows(s: RecordData): RecordData[] {
  return [
    ...list(s.items?.all || s.items?.recent).map((i) => ({
      ...i,
      state:
        i.status || (i.state === "accepted" ? "active" : i.state) || "active",
      node: i.provenance?.node || i.source || "",
      specialist: "",
    })),
    ...list(s.specialists).flatMap((sp) =>
      list(sp.lessons).map((l) => ({
        id: sp.id + ":" + l.time,
        title: l.text,
        kind: "Specialist lesson",
        state: sp.status || "active",
        node: l.from,
        specialist: sp.node + "/" + sp.id,
        provenance: {
          node: l.from,
          time: l.time,
          ref: "specialists/" + sp.id + "/lessons.md",
        },
      })),
    ),
  ].sort(
    (a: RecordData, b: RecordData) =>
      Number(b.state === "proposed") - Number(a.state === "proposed") ||
      (Date.parse(b.provenance?.time) || 0) -
        (Date.parse(a.provenance?.time) || 0),
  );
}
export const usageStopPercent = (row: RecordData, policy: RecordData) =>
  Number.isFinite(row.stop_percent) ? row.stop_percent : policy.stop_percent;

export function windowState(
  w: RecordData | undefined,
  row: RecordData,
  policy: RecordData,
  now = Date.now() / 1000,
) {
  const stop = usageStopPercent(row, policy);
  const stamp = w?.read_at || row.read_at,
    stale = !stamp || now - stamp > (policy.max_age_seconds ?? 120);
  const expired = w?.expired || !!(w?.resets_at && w.resets_at <= now);
  const known = w && !w.absent && !expired && Number.isFinite(w.used_percent);
  const state = !known
    ? w?.absent
      ? "No window"
      : expired
        ? "Expired · unknown"
        : "Unknown"
    : stale
      ? "Stale · unverified"
      : Number.isFinite(stop) &&
          w.used_percent >= stop
        ? "At stop line"
        : Number.isFinite(stop) &&
            w.used_percent >= stop - 10
          ? "Near stop line"
          : "Normal";
  return {
    known,
    stale,
    expired,
    state,
    percent: known ? Math.min(100, Math.max(0, w.used_percent)) : 0,
    age: stamp ? Math.max(0, Math.floor((now - stamp) / 60)) : null,
  };
}
export function routing(s: RecordData) {
  const p = { ...s.usage_routing_policy, ...s.usage_policy },
    now = Date.now() / 1000;
  if (p.autodetected) return null;
  return list(s.usage)
    .map((row) => {
      const stop = usageStopPercent(row, p);
      if (!Number.isFinite(stop)) return null;
      const windows = Object.entries(row.windows || {}).filter(
        ([k, w]: any) =>
          ["five_hour", "seven_day"].includes(k) &&
          !w.absent &&
          !w.expired &&
          (!w.resets_at || w.resets_at > now),
      ) as [string, RecordData][];
      if (windows.some(([, w]) => w.used_percent >= stop))
        return null;
      const unknown =
        !windows.length ||
        windows.some(
          ([, w]) =>
            !Number.isFinite(w.used_percent) ||
            (w.source || row.source) === "unknown" ||
            windowState(w, row, p, now).stale,
        );
      const ahead = windows.some(
        ([k, w]) =>
          k === "seven_day" &&
          w.used_percent >
            (w.resets_at
              ? Math.max(
                  0,
                  Math.min(100, 100 * (1 - (w.resets_at - now) / 604800)),
                )
              : 100) +
              (p.pacing_margin ?? 10),
      );
      const fallback = list(s.brain?.fallback),
        rank = fallback.indexOf(row.host_provider_instance_id);
      return {
        row,
        score: [
          +unknown,
          +ahead,
          Math.max(0, ...windows.map(([, w]) => w.used_percent || 0)),
          rank < 0 ? fallback.length : rank,
        ],
      };
    })
    .filter(Boolean)
    .sort((a: any, b: any) => {
      for (let i = 0; i < a.score.length; i++)
        if (a.score[i] !== b.score[i]) return a.score[i] - b.score[i];
      return String(a.row.id).localeCompare(String(b.row.id));
    })[0];
}

export function treeRows(nodes: RecordData[]) {
  const active = nodes.filter((n) => !n.archived),
    seen = new Set<string>(),
    out: { node: RecordData; depth: number }[] = [];
  function visit(n: RecordData, depth: number) {
    if (seen.has(n.id)) return;
    seen.add(n.id);
    out.push({ node: n, depth });
    for (const child of active.filter((c) => c.parent === n.id))
      visit(child, depth + 1);
  }
  for (const n of active.filter(
    (n) => !n.parent || !active.some((p) => p.id === n.parent),
  ))
    visit(n, 0);
  for (const n of active) visit(n, 0);
  return out;
}

export function hasMeasuredUsage(row: RecordData) {
  return (
    !!row.Usage &&
    ["Fresh in", "Cache read", "Cache write", "Output", "Active h"].some(
      (key) =>
        row[key] !== "" &&
        row[key] != null &&
        Number.isFinite(Number(row[key])),
    )
  );
}

export function machineHealth(machine: RecordData) {
  const out: RecordData[] = [];
  const add = (name: string, used: number, state: string) =>
    out.push({ name, percent: Math.max(0, Math.min(100, used * 100)), state });
  if (Number.isFinite(machine.load?.[0]) && machine.cpus > 0) {
    const used = machine.load[0] / machine.cpus;
    add(
      "Processor load",
      used,
      used > 0.9
        ? "Critical pressure"
        : used > 0.7
          ? "Near capacity"
          : "Normal",
    );
  }
  if (Number.isFinite(machine.mem_free_gb) && machine.mem_total_gb > 0) {
    const used = 1 - machine.mem_free_gb / machine.mem_total_gb;
    add(
      "Memory used",
      used,
      machine.mem_free_gb < 8
        ? "Critical pressure"
        : used > 0.8
          ? "Near capacity"
          : "Normal",
    );
  }
  if (Number.isFinite(machine.disk_free_gb) && machine.disk_total_gb > 0) {
    const used = 1 - machine.disk_free_gb / machine.disk_total_gb;
    add(
      "Disk used",
      used,
      machine.disk_free_gb < 40
        ? "Critical pressure"
        : used > 0.85
          ? "Near capacity"
          : "Normal",
    );
  }
  return out;
}

// A job's human name: no "job/" prefix, no machine hash (an import job reads "Import").
export const jobTitle = (value: any) => (String(value || "Job").replace(/^job\//, "").replace(/[:/_-]?\b[0-9a-f]{12,}\b/gi, "").replace(/[-_/:]+/g, " ").trim() || "Job").replace(/^./, (c) => c.toUpperCase());

// Dashboard shaping: derived only from fields the CLI already serves.
export const stamp = (v: any) => (typeof v === "number" ? v * 1000 : Date.parse(String(v || "")));
export const nowOf = (s: RecordData) => stamp(s.brain?.now) || Date.now();
export const within = (v: any, now: number, hours: number) => {
  const t = stamp(v);
  return Number.isFinite(t) && t <= now + 60_000 && now - t <= hours * 3600_000;
};
export function threads(nodes: RecordData[]) {
  return flight(nodes).filter((f) => f.job.state !== "Worktree only");
}
// Every open job gets one tone: ready, review, working, blocked (needs you) or idle.
export function tone(f: { job: RecordData; wt?: RecordData }, due: RecordData[] = []) {
  const kind = jobState(f.job.state).kind.toLowerCase();
  if (f.wt?.ready?.ready || due.some((d) => d.ready && d.branch === f.job.branch) || kind === "ready to merge") return "ready";
  if (/blocked|needs you|paused/.test(kind)) return "blocked";
  if (/review/.test(kind)) return "review";
  if (f.job.state === "Worktree only") return "idle";
  return "working";
}
export const dueKinds: Record<string, string> = {
  handback: "Hand back", "stale-status": "Update status", "lesson-outcome": "Record lessons", inbox: "Question",
  "merged-job": "Close job", "missing-worktree": "Missing working copy", "unrecorded-worktree": "Reconcile records",
  "blocked-overdue": "Overdue", "overdue-question": "Overdue question", "missing-preview": "Record preview",
  "unsettled-threads": "Settle threads", "overlapping-inputs": "Overlapping jobs", "invalid-handback": "Unreadable hand-back",
  "core-changes": "Read changes", machine: "Machine", ledger: "Reconcile ledger",
};
// A Due card names its job from the record's branch, then drops the branch prefix from the text.
export function dueParts(item: RecordData) {
  const text = String(item.text || "");
  const branch = item.branch || "";
  const body = branch && text.startsWith(branch + ":") ? text.slice(branch.length + 1).trim() : text;
  return { title: branch ? jobTitle(branch) : "", label: item.ready ? "Ready to merge" : dueKinds[item.kind] || label(item.kind || "Next step"), body };
}
// The sender comes from the record (`from`); the kind names the source when nobody wrote it.
export const sources: Record<string, string> = {
  blocked: "Project status", "blocked-overdue": "Project status", job: "Job", "import-review": "Import review",
  "core-update": "Core update", inbox: "Project inbox", stale: "Project status",
};
export const sender = (item: RecordData) => (typeof item.from === "string" && item.from.trim() ? item.from.trim() : sources[item.kind] || "Workspace");
export function hourly(s: RecordData) {
  const counts = Object.values(s.activity?.counts || {}) as number[][];
  const hours = Math.max(0, ...counts.map((c) => c.length));
  return Array.from({ length: hours }, (_, i) => counts.reduce((a, c) => a + (c[i] || 0), 0));
}
export function allJobs(nodes: RecordData[]): RecordData[] {
  return nodes.flatMap((n) => list(n.jobs?.rows).map((r) => ({ ...r, _node: n, _open: list(n.jobs?.open).find((o) => o.Job === r.Job && o.Started === r.Started) })));
}
