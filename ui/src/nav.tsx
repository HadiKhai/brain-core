import { useState, type CSSProperties, type KeyboardEvent } from "react";
import {
  Brain, Inbox as InboxIcon, BookOpen, Bot, ChartNoAxesCombined, ChevronDown, ChevronRight, FolderTree, FolderGit2,
  ArrowUpRight, LayoutDashboard, MessagesSquare, Plus, Search, Share2, ShieldCheck, Workflow, X, Users,
} from "lucide-react";
import {
  Sidebar, SidebarHeader, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupLabel, SidebarGroupAction, SidebarMenu,
  SidebarMenuItem, SidebarMenuButton, SidebarMenuBadge, SidebarMenuSub, SidebarMenuSubItem, SidebarMenuSubButton,
  SidebarMenuAction, SidebarInput, useSidebar,
} from "@/components/ui/sidebar";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { list, quote, safeHref, treeRows, threads, tone, jobTitle, learningRows, type RecordData } from "./model";
import { fold } from "./search";
import { writeRoute, type Route } from "./routes";

// The sidebar is the product's hierarchy: Overview and Inbox, the Brain tree (projects → threads),
// Agents (Specialists, Jobs, Threads, Usage) and Knowledge (Learnings, Graph).
export function Navigation({ s, route, go, onAdd, onNotice }: {
  s: RecordData | null; route: Route; go: (r: Partial<Route> & { view: string }) => void; onAdd: () => void; onNotice: (t: string) => void;
}) {
  const { setOpenMobile, isMobile } = useSidebar();
  const [filter, setFilter] = useState(""), [closed, setClosed] = useState<Record<string, boolean>>({});
  const nodes = list(s?.nodes), open = threads(nodes), due = list(s?.due);
  const needs = (id: string) => list(s?.inbox).filter((a) => a.node === id).length;
  const rows = treeRows(nodes), long = rows.length > 8;
  const shown = filter ? rows.filter(({ node }) => fold(node.title).includes(fold(filter))) : rows;
  const hidden = new Set<string>();
  if (!filter) for (const { node } of rows) if (closed[node.id] || hidden.has(node.id)) list(nodes).filter((c) => c.parent === node.id).forEach((c) => hidden.add(c.id));
  const goto = (r: Partial<Route> & { view: string }) => { go(r); if (isMobile) setOpenMobile(false); };
  const item = (view: string, title: string, Icon: any, badge?: number, alert = false) => (
    <SidebarMenuItem key={view}>
      <SidebarMenuButton isActive={route.view === view} tooltip={title} aria-current={route.view === view ? "page" : undefined} onClick={() => goto({ view })}>
        <Icon /><span>{title}</span>
      </SidebarMenuButton>
      {!!badge && <SidebarMenuBadge className={alert ? "badge-alert" : ""}>{badge}</SidebarMenuBadge>}
    </SidebarMenuItem>
  );
  const agentsActive = ["specialists", "jobs", "threads", "usage"].includes(route.view);
  const proposed = s ? learningRows(s).filter((r) => r.state === "proposed" && !r.specialist).length : 0;
  return (
    <Sidebar variant="inset" collapsible="icon" className="app-sidebar">
      <SidebarHeader>
        {isMobile && (
          <div className="mobile-nav-heading">
            <strong>Navigation</strong>
            <Button variant="ghost" size="icon" aria-label="Close navigation" onClick={() => setOpenMobile(false)}><X size={18} /></Button>
          </div>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger render={<Button variant="ghost" className="workspace-switcher" />}>
            <span className="brand-tile"><Brain size={18} /></span>
            <span className="workspace-name"><strong>{s?.brain?.title || "Brain"}</strong><small>{s?.brain?.machine || "This machine"}</small></span>
            <ChevronDown size={14} />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem disabled>Current workspace: {s?.brain?.title || "Brain"}</DropdownMenuItem>
            {list(s?.brain?.others).map((name: any) => (
              <DropdownMenuItem key={name} onClick={async () => {
                const command = "brain -b " + quote(name) + " ui";
                try { await navigator.clipboard.writeText(command); onNotice("Workspace launch command copied. Run it in your terminal."); }
                catch { onNotice("Run this command in your terminal: " + command); }
              }}>Copy launch command · {name}</DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarHeader>
      <SidebarContent>
        <nav aria-label="Workspace navigation" onKeyDown={arrows}>
          <SidebarGroup>
            <SidebarMenu>
              {item("overview", "Overview", LayoutDashboard)}
              {item("inbox", "Inbox", InboxIcon, list(s?.inbox).length + due.length, true)}
            </SidebarMenu>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel>Brain</SidebarGroupLabel>
            <SidebarGroupAction aria-label="Add projects" onClick={() => { if (isMobile) setOpenMobile(false); onAdd(); }}><Plus /></SidebarGroupAction>
            {long && <div className="tree-filter"><Search size={14} /><SidebarInput aria-label="Filter projects" placeholder="Filter projects" value={filter} onChange={(e) => setFilter(e.target.value)} /></div>}
            <SidebarMenu className="brain-tree">
              {item("projects", "All projects", FolderTree, rows.length)}
              {shown.filter(({ node }) => !hidden.has(node.id)).map(({ node: n, depth }) => {
                const work = open.filter((f) => f.node.id === n.id);
                const children = nodes.some((c) => c.parent === n.id && !c.archived);
                const active = route.view === "project" && route.node === n.id;
                return (
                  <SidebarMenuItem key={n.id} className="tree-item" style={{ "--depth": filter ? 0 : Math.min(depth, 4) } as CSSProperties}>
                    <SidebarMenuButton isActive={active && !route.thread} tooltip={n.title || "Project"} aria-current={active ? "page" : undefined} onClick={() => goto({ view: "project", node: n.id })}>
                      <span className="tree-glyph"><FolderGit2 /><i className={"dot live-dot tone-" + (work.some((f) => tone(f, due) === "working" || tone(f, due) === "review") ? "working" : needs(n.id) ? "blocked" : "idle")} aria-hidden /></span>
                      <span className="truncate" dir="auto">{n.title || "Untitled project"}</span>
                    </SidebarMenuButton>
                    {children && !filter ? (
                      <SidebarMenuAction aria-label={(closed[n.id] ? "Expand " : "Collapse ") + (n.title || "project")} onClick={() => setClosed({ ...closed, [n.id]: !closed[n.id] })}>
                        {closed[n.id] ? <ChevronRight /> : <ChevronDown />}
                      </SidebarMenuAction>
                    ) : needs(n.id) ? <SidebarMenuBadge className="badge-alert">{needs(n.id)}</SidebarMenuBadge> : null}
                    {(work.length > 0 || safeHref(n.coordinator)) && (
                      <SidebarMenuSub>
                        {safeHref(n.coordinator) && (
                          <SidebarMenuSubItem>
                            <SidebarMenuSubButton data-external render={<a href={safeHref(n.coordinator)} target="_blank" rel="noreferrer" />}>
                              <MessagesSquare /><span className="truncate">Coordinator</span><ArrowUpRight className="ext" />
                            </SidebarMenuSubButton>
                          </SidebarMenuSubItem>
                        )}
                        {work.map((f) => (
                          <SidebarMenuSubItem key={f.job.branch}>
                            <SidebarMenuSubButton isActive={active && route.thread === f.job.branch} href={writeRoute({ view: "project", node: n.id, thread: f.job.branch, tab: "threads" })} onClick={() => isMobile && setOpenMobile(false)}>
                              <i className={"dot tone-" + tone(f, due)} />
                              <span className="truncate">{jobTitle(f.job.branch)}</span>
                            </SidebarMenuSubButton>
                          </SidebarMenuSubItem>
                        ))}
                      </SidebarMenuSub>
                    )}
                  </SidebarMenuItem>
                );
              })}
              {filter && !shown.length && <li className="tree-empty">No project matches “{filter}”.</li>}
            </SidebarMenu>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel>Agents</SidebarGroupLabel>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton isActive={false} tooltip="Agents" onClick={() => goto({ view: agentsActive ? route.view : "specialists" })}>
                  <Bot /><span>Agents</span>
                </SidebarMenuButton>
                <SidebarMenuSub>
                  {([["specialists", "Specialists", list(s?.specialists).length, Users], ["jobs", "Jobs", nodes.reduce((a, n) => a + list(n.jobs?.open).length, 0), Workflow], ["threads", "Threads", open.length, MessagesSquare], ["usage", "Usage", 0, ChartNoAxesCombined]] as const).map(([view, title, count, Icon]) => (
                    <SidebarMenuSubItem key={view}>
                      <SidebarMenuSubButton isActive={route.view === view} aria-current={route.view === view ? "page" : undefined} href={writeRoute({ view })} onClick={() => isMobile && setOpenMobile(false)}>
                        <Icon /><span>{title}</span>{count ? <span className="sub-count">{count}</span> : null}
                      </SidebarMenuSubButton>
                    </SidebarMenuSubItem>
                  ))}
                </SidebarMenuSub>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel>Knowledge</SidebarGroupLabel>
            <SidebarMenu>
              {item("learnings", "Learnings", BookOpen, proposed, true)}
              {item("graph", "Graph", Share2)}
            </SidebarMenu>
          </SidebarGroup>
        </nav>
      </SidebarContent>
      <SidebarFooter>
        <div className="sidebar-foot"><ShieldCheck size={14} /><span>Local and read-only</span></div>
      </SidebarFooter>
    </Sidebar>
  );
}

// Arrow keys move between destinations and open them; Home and End jump to the ends.
function arrows(e: KeyboardEvent<HTMLElement>) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
  const buttons = [...e.currentTarget.querySelectorAll<HTMLButtonElement>("[data-slot=sidebar-menu-button], [data-slot=sidebar-menu-sub-button]")].filter((b) => b.offsetParent);
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
  if (index < 0) return;
  e.preventDefault();
  const next = e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 : (index + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[next].click();
  buttons[next].focus();
}
