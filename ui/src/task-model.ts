import type { RecordData } from "./model";

export const taskKey = (task: RecordData) => `${task.node}:${task.id}`;
export const dependencyKey = (task: RecordData, ref: string) => ref.includes(":") ? ref : `${task.node}:${ref}`;
export const taskColumns = ["Backlog", "Ready", "In progress", "Parked", "Done"] as const;

export function taskColumn(task: RecordData): string {
  if (task.delivery_state === "unproven") return "Parked";
  switch (task.status) {
    case "ready": return "Ready";
    case "in-progress": case "in-review": case "testing": return "In progress";
    case "blocked": case "parked": return "Parked";
    case "merged": case "done": return "Done";
    case "dropped": return "Dropped";
    default: return "Backlog";
  }
}

export function filterTasks(rows: RecordData[], node: string, owner: string, query: string, dropped: boolean) {
  const needle = query.trim().toLocaleLowerCase();
  return rows.filter(t => (!node || t.node === node) && (!owner || t.owner === owner)
    && (dropped || t.status !== "dropped")
    && (!needle || [t.title, t.project, t.id, t.branch].join(" ").toLocaleLowerCase().includes(needle)));
}

export function taskGraph(rows: RecordData[]) {
  const byKey = new Map(rows.map(t => [taskKey(t), t]));
  const outgoing = new Map(rows.map(t => [taskKey(t), new Set<string>()]));
  const incoming = new Map(rows.map(t => [taskKey(t), new Set<string>()]));
  for (const t of rows) for (const ref of (t.depends_on || []) as string[]) {
    const from = dependencyKey(t, ref), to = taskKey(t);
    if (!byKey.has(from)) continue;
    outgoing.get(from)!.add(to);
    incoming.get(to)!.add(from);
  }
  const edges = [...outgoing].flatMap(([from, targets]) => [...targets].map(to => ({ from, to })));
  return { byKey, outgoing, incoming, edges };
}

export type TaskGraph = ReturnType<typeof taskGraph>;

export function downstream(graph: TaskGraph, key: string): Set<string> {
  const seen = new Set([key]), result = new Set<string>(), queue = [key];
  for (let i = 0; i < queue.length; i++) for (const next of graph.outgoing.get(queue[i]) || []) {
    if (seen.has(next)) continue;
    seen.add(next); result.add(next); queue.push(next);
  }
  return result;
}

// Layer the connected tasks in dependency order; pack independent tasks into a compact grid.
// Kahn's traversal is bounded even for malformed provider cycles, which remain visible.
export function taskLayout(graph: TaskGraph) {
  const degree = new Map([...graph.incoming].map(([key, refs]) => [key, refs.size]));
  const level = new Map([...graph.byKey.keys()].map(key => [key, 0]));
  const queue = [...degree].filter(([, n]) => n === 0).map(([key]) => key);
  for (let i = 0; i < queue.length; i++) for (const next of graph.outgoing.get(queue[i]) || []) {
    level.set(next, Math.max(level.get(next)!, level.get(queue[i])! + 1));
    degree.set(next, degree.get(next)! - 1);
    if (degree.get(next) === 0) queue.push(next);
  }
  const positions = new Map<string, { x: number; y: number }>(), lanes = new Map<number, number>();
  const independent: string[] = [];
  let height = 0, width = 0;
  for (const key of graph.byKey.keys()) {
    if (!graph.incoming.get(key)!.size && !graph.outgoing.get(key)!.size) { independent.push(key); continue; }
    const depth = level.get(key)!, row = lanes.get(depth) || 0;
    const x = 32 + depth * 296, y = 32 + row * 112;
    positions.set(key, { x, y }); lanes.set(depth, row + 1);
    height = Math.max(height, y + 88); width = Math.max(width, x + 248);
  }
  const start = height ? height + 56 : 32;
  independent.forEach((key, i) => {
    const x = 32 + (i % 5) * 296, y = start + Math.floor(i / 5) * 112;
    positions.set(key, { x, y }); height = Math.max(height, y + 88); width = Math.max(width, x + 248);
  });
  return { positions, width: Math.max(800, width + 32), height: Math.max(480, height + 32) };
}
