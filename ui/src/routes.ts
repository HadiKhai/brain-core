// Hash routes. Old addresses keep working: Agents opens Jobs, Import checklist opens the add-projects
// sheet over Overview, and `node=` on any page opens that project's page.
export type Route = { view: string; node: string | null; thread: string | null; tab: string | null; state: string | null; add: boolean };
export const views = ["overview", "inbox", "projects", "project", "specialists", "jobs", "threads", "usage", "learnings", "graph"];
export function readRoute(): Route {
  const p = new URLSearchParams(location.hash.slice(1));
  let view = p.get("view") || "overview";
  const node = p.get("node");
  let add = p.get("add") === "1";
  if (view === "agents") view = "jobs";
  if (view === "imports") { view = "overview"; add = true; }
  if (node && view !== "project") view = "project";
  if (!views.includes(view)) view = "overview";
  if (view === "project" && !node) view = "projects";
  return { view, node, thread: p.get("thread"), tab: p.get("tab"), state: p.get("state"), add };
}
export function writeRoute(r: Partial<Route> & { view: string }) {
  const p = new URLSearchParams({ view: r.view });
  for (const k of ["node", "thread", "tab", "state"] as const) if (r[k]) p.set(k, r[k]!);
  if (r.add) p.set("add", "1");
  return "#" + p.toString();
}
