export type GNode = { key: string; kind: "node" | "specialist" | "learning"; id: string; title: string; r: number; recent: boolean; x: number; y: number; vx: number; vy: number };
export type GEdge = { a: number; b: number; kind: string };

// A small deterministic force layout: repulsion, springs, gravity and collision. No dependency.
export function layout(nodes: GNode[], edges: GEdge[], iterations = 260) {
  const n = nodes.length;
  nodes.forEach((p, i) => { const a = i * 2.399963, d = 30 * Math.sqrt(i + 1); p.x = Math.cos(a) * d; p.y = Math.sin(a) * d; p.vx = p.vy = 0; });
  for (let it = 0; it < iterations; it++) {
    const alpha = 1 - it / iterations;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y, d2 = dx * dx + dy * dy;
      if (d2 < 0.01) { dx = 0.1 * (j - i); dy = 0.1; d2 = dx * dx + dy * dy; }
      const d = Math.sqrt(d2), min = a.r + b.r + 6;
      // Big nodes (projects) push harder, so small ones settle around them and labels find room.
      let f = (520 * alpha * Math.max(1, (a.r * b.r) / 30)) / d2;
      if (d < min) f += (min - d) * 0.5;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx -= fx; a.vy -= fy; b.vx += fx; b.vy += fy;
    }
    for (const e of edges) {
      const a = nodes[e.a], b = nodes[e.b], dx = b.x - a.x, dy = b.y - a.y, d = Math.max(1, Math.hypot(dx, dy));
      const rest = e.kind === "parent" ? 110 : e.kind === "source" ? 46 : 70, f = (d - rest) * 0.04 * alpha;
      a.vx += (dx / d) * f; a.vy += (dy / d) * f; b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
    }
    for (const p of nodes) { p.vx -= p.x * 0.012 * alpha; p.vy -= p.y * 0.012 * alpha; p.x += p.vx * 0.5; p.y += p.vy * 0.5; p.vx *= 0.6; p.vy *= 0.6; }
  }
  return nodes;
}

// Labels never overlap nodes or each other: try below, above, right, then left of each node by
// priority (matches, then projects, then size); the rest appear only on hover, selection or match.
export type Label = { x: number; y: number; anchor: "middle" | "start" | "end" };
export const labelWidth = (title: string) => Math.min(28, title.length) * 6.2 + 4;
export function labelBox(p: GNode, at: Label) {
  const w = labelWidth(p.title), x0 = at.anchor === "middle" ? at.x - w / 2 : at.anchor === "start" ? at.x : at.x - w;
  return { x0, x1: x0 + w, y0: at.y - 11, y1: at.y + 3 };
}
export function placeLabels(nodes: GNode[], wanted: Set<string>) {
  const boxes: { x0: number; y0: number; x1: number; y1: number }[] = [], placed = new Map<string, Label>();
  const hits = (b: (typeof boxes)[number]) => boxes.some((o) => b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0)
    || nodes.some((p) => p.x + p.r > b.x0 && p.x - p.r < b.x1 && p.y + p.r > b.y0 && p.y - p.r < b.y1);
  const order = [...nodes].sort((a, b) => Number(wanted.has(b.key)) - Number(wanted.has(a.key)) || (a.kind === "node" ? 0 : 1) - (b.kind === "node" ? 0 : 1) || b.r - a.r);
  for (const p of order) {
    if (!wanted.has(p.key) && p.kind !== "node") continue;
    const options: Label[] = [
      { x: p.x, y: p.y + p.r + 14, anchor: "middle" }, { x: p.x, y: p.y - p.r - 5, anchor: "middle" },
      { x: p.x + p.r + 5, y: p.y + 4, anchor: "start" }, { x: p.x - p.r - 5, y: p.y + 4, anchor: "end" },
    ];
    const at = options.find((o) => !hits(labelBox(p, o)));
    if (at) { boxes.push(labelBox(p, at)); placed.set(p.key, at); }
  }
  return placed;
}
