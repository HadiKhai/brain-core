import { useRef, useState, type CSSProperties, type KeyboardEvent, type ReactElement, type ReactNode } from "react";
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
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuGroup, DropdownMenuLabel } from "@/components/ui/dropdown-menu";
import { list, needsView, quote, safeHref, treeRows, threads, tone, jobTitle, learningRows, type RecordData } from "./model";
import { fold } from "./search";
import { visibleTaskCount } from "./task-model";
import { writeRoute, type Route } from "./routes";

// The sidebar is the product's hierarchy: Overview and Inbox, the Brain tree (projects → threads),
// Agents (Specialists, Jobs, Threads, Usage) and Knowledge (Learnings, Graph).
export function Navigation({ s, route, go, onAdd, onNotice }: {
  s: RecordData | null; route: Route; go: (r: Partial<Route> & { view: string }) => void; onAdd: () => void; onNotice: (t: string) => void;
}) {
  const { setOpenMobile, isMobile } = useSidebar();
  const [filter, setFilter] = useState(""), [closed, setClosed] = useState<Record<string, boolean>>({});
  const nodes = list(s?.nodes), open = threads(nodes);
  const { inbox, due } = needsView(s || {});
  const needs = (id: string) => inbox.filter((a) => a.node === id).length;
  const rows = treeRows(nodes), long = rows.length > 8;
  const shown = filter ? rows.filter(({ node }) => fold(node.title).includes(fold(filter))) : rows;
  const hidden = new Set<string>();
  if (!filter) for (const { node } of rows) if (closed[node.id] || hidden.has(node.id)) list(nodes).filter((c) => c.parent === node.id).forEach((c) => hidden.add(c.id));
  const copyThread = async (link: string) => {
    try { await navigator.clipboard.writeText(link); onNotice("Thread link copied. Paste it into your conversation host."); }
    catch { onNotice("Copy this thread link: " + link); }
  };
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
  const collapsed = useSidebar().state === "collapsed" && !isMobile;
  const agentAreas = [["specialists", "Specialists", list(s?.specialists).length, Users], ["jobs", "Jobs", nodes.reduce((a, n) => a + list(n.jobs?.open).length, 0), Workflow], ["threads", "Threads", open.length, MessagesSquare], ["usage", "Usage", 0, ChartNoAxesCombined]] as const;
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
              {item("inbox", "Inbox", InboxIcon, inbox.length + due.length, true)}
            </SidebarMenu>
          </SidebarGroup>
          <SidebarGroup>
            <SidebarGroupLabel>Brain</SidebarGroupLabel>
            <SidebarGroupAction aria-label="Add projects" onClick={() => { if (isMobile) setOpenMobile(false); onAdd(); }}><Plus /></SidebarGroupAction>
            {long && <div className="tree-filter"><Search size={14} /><SidebarInput aria-label="Filter projects" placeholder="Filter projects" value={filter} onChange={(e) => setFilter(e.target.value)} /></div>}
            <SidebarMenu className="brain-tree">
              {item("projects", "All projects", FolderTree, rows.length)}
              {item("tasks", "Tasks", Workflow, nodes.reduce((total, n) => total + visibleTaskCount(list(n.tasks?.rows)), 0))}
              {shown.filter(({ node }) => !hidden.has(node.id)).map(({ node: n, depth }) => {
                const work = open.filter((f) => f.node.id === n.id);
                const children = nodes.some((c) => c.parent === n.id && !c.archived);
                const active = route.view === "project" && route.node === n.id;
                return (
                  <SidebarMenuItem key={n.id} className="tree-item" style={{ "--depth": filter ? 0 : Math.min(depth, 4) } as CSSProperties}>
                    <RailFlyout label={n.title || "Project"} items={[
                      { key: "page", label: "Project page", icon: <FolderGit2 />, active: active && !route.thread, href: writeRoute({ view: "project", node: n.id }) },
                      ...(safeHref(n.coordinator) ? [{ key: "coordinator", label: "Coordinator", icon: <MessagesSquare />, href: safeHref(n.coordinator), external: true }] : []),
                      ...(n.coordinator ? [{ key: "copy-coordinator", label: "Copy thread link", icon: <MessagesSquare />, onSelect: () => { void copyThread(n.coordinator); } }] : []),
                      ...work.map((f) => ({ key: f.job.branch, label: jobTitle(f.job.branch), icon: <i className={"dot tone-" + tone(f, due)} />, active: active && route.thread === f.job.branch, href: writeRoute({ view: "project", node: n.id, thread: f.job.branch, tab: "threads" }) })),
                    ].filter((i) => i.key !== "page" || work.length || n.coordinator)} button={
                    <SidebarMenuButton isActive={active && !route.thread} tooltip={collapsed && (work.length || n.coordinator) ? undefined : n.title || "Project"} aria-current={active ? "page" : undefined} onClick={() => goto({ view: "project", node: n.id })}>
                      <span className="tree-glyph"><FolderGit2 /><i className={"dot live-dot tone-" + (work.some((f) => tone(f, due) === "working" || tone(f, due) === "review") ? "working" : needs(n.id) ? "blocked" : "idle")} aria-hidden /></span>
                      <span className="truncate" dir="auto">{n.title || "Untitled project"}</span>
                    </SidebarMenuButton>} />
                    {children && !filter ? (
                      <SidebarMenuAction aria-label={(closed[n.id] ? "Expand " : "Collapse ") + (n.title || "project")} onClick={() => setClosed({ ...closed, [n.id]: !closed[n.id] })}>
                        {closed[n.id] ? <ChevronRight /> : <ChevronDown />}
                      </SidebarMenuAction>
                    ) : needs(n.id) ? <SidebarMenuBadge className="badge-alert">{needs(n.id)}</SidebarMenuBadge> : null}
                    {(work.length > 0 || n.coordinator) && (
                      <SidebarMenuSub>
                        {safeHref(n.coordinator) && (
                          <SidebarMenuSubItem>
                            <SidebarMenuSubButton data-external render={<a href={safeHref(n.coordinator)} target="_blank" rel="noreferrer" />}>
                              <MessagesSquare /><span className="truncate">Coordinator</span><ArrowUpRight className="ext" />
                            </SidebarMenuSubButton>
                          </SidebarMenuSubItem>
                        )}
                        {n.coordinator && <SidebarMenuSubItem><SidebarMenuSubButton aria-label="Copy thread link" onClick={() => { void copyThread(n.coordinator); }}><MessagesSquare /><span className="truncate">{safeHref(n.coordinator) ? "Copy thread link" : "Copy coordinator link"}</span></SidebarMenuSubButton></SidebarMenuSubItem>}
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
                <RailFlyout label="Agents" items={agentAreas.map(([view, title, count, Icon]) => ({ key: view, label: title, count, icon: <Icon />, active: route.view === view, href: writeRoute({ view }) }))}
                  button={<SidebarMenuButton isActive={false} tooltip={collapsed ? undefined : "Agents"} onClick={() => goto({ view: agentsActive ? route.view : "specialists" })}>
                    <Bot /><span>Agents</span>
                  </SidebarMenuButton>} />
                <SidebarMenuSub>
                  {agentAreas.map(([view, title, count, Icon]) => (
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

// The collapsed rail keeps every sub-item reachable: hover, focus or tap on the parent opens a flyout
// beside it. Choosing a destination navigates; the rail stays collapsed.
type FlyItem = { key: string; label: string; icon: ReactNode; count?: number; active?: boolean; href?: string; external?: boolean; onSelect?: () => void };
function RailFlyout({ label, items, button }: { label: string; items: FlyItem[]; button: ReactElement }) {
  const { state, isMobile } = useSidebar();
  const [open, setOpen] = useState(false), closedAt = useRef(0), trigger = useRef<HTMLButtonElement>(null);
  if (state !== "collapsed" || isMobile || !items.length) return button;
  return (
    <DropdownMenu open={open} onOpenChange={(o) => { if (!o) closedAt.current = Date.now(); setOpen(o); }}>
      <DropdownMenuTrigger ref={trigger} openOnHover delay={80} closeDelay={160} render={button} onFocus={() => {
        // Focus arriving from the rail opens it; focus returning as the flyout closes (Escape) does not.
        if (Date.now() - closedAt.current > 200) setOpen(true);
      }} />
      <DropdownMenuContent side="right" align="start" sideOffset={8} className="rail-flyout" finalFocus={() => {
        // A hover-opened menu may have another rail item as its previous focus.
        // Return to this trigger, suppressing its focus-open handler for this return.
        closedAt.current = Date.now();
        return trigger.current;
      }}>
        <DropdownMenuGroup>
          <DropdownMenuLabel>{label}</DropdownMenuLabel>
          {items.map((i) => (
            <DropdownMenuItem key={i.key} data-active={i.active || undefined}
              onClick={i.onSelect} render={i.href ? <a href={i.href} {...(i.external ? { target: "_blank", rel: "noreferrer" } : {})} /> : <Button variant="ghost" />}>
              {i.icon}<span className="truncate">{i.label}</span>{i.count ? <span className="sub-count">{i.count}</span> : null}
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
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
