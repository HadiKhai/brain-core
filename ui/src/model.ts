// The Python API owns this read-only snapshot. Optional fields stay optional across upgrades.
export type RecordData = Record<string, any>;
export const list = (v: any): RecordData[] => (Array.isArray(v) ? v : []);
export const label = (v: any) =>
  String(v ?? "")
    .replace(/[_-]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
export const count = (n: number, word: string) =>
  `${n} ${word}${n === 1 ? "" : "s"}`;
export const tokens = (n: any) =>
  Intl.NumberFormat("en", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(Number(n) || 0);
export const quote = (s: any) => "'" + String(s).replace(/'/g, "'\\''") + "'";
export const safeHref = (s: any) =>
  /^(https?:|t3-thread:)/i.test(String(s)) ? String(s) : undefined;
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
export function flight(
  nodes: RecordData[],
): {
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
export function windowState(
  w: RecordData | undefined,
  row: RecordData,
  policy: RecordData,
  now = Date.now() / 1000,
) {
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
      : Number.isFinite(policy.stop_percent) &&
          w.used_percent >= policy.stop_percent
        ? "At stop line"
        : Number.isFinite(policy.stop_percent) &&
            w.used_percent >= policy.stop_percent - 10
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
  if (p.autodetected || !Number.isFinite(p.stop_percent)) return null;
  return list(s.usage)
    .map((row) => {
      const windows = Object.entries(row.windows || {}).filter(
        ([k, w]: any) =>
          ["five_hour", "seven_day"].includes(k) &&
          !w.absent &&
          !w.expired &&
          (!w.resets_at || w.resets_at > now),
      ) as [string, RecordData][];
      if (windows.some(([, w]) => w.used_percent >= p.stop_percent))
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
