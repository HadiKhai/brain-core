import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Brain,
  Bell,
  BookOpen,
  Bot,
  ChartNoAxesCombined,
  ChevronDown,
  FolderInput,
  LayoutDashboard,
  Monitor,
  Moon,
  RefreshCw,
  Sun,
  ShieldCheck,
  X,
  ArrowUpRight,
} from "lucide-react";
import {
  SidebarProvider,
  Sidebar,
  SidebarHeader,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuItem,
  SidebarMenuButton,
  SidebarInset,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import {
  TimeZoneContext,
  PageHeader,
  PageSection,
  Status,
  Markdown,
  Text,
  Tip,
  DataTable,
  Command,
  Empty,
} from "./shared";
import { Overview } from "./overview";
import { Learnings } from "./learnings";
import { Agents } from "./agents";
import { Usage } from "./usage";
import { Imports } from "./imports";
import { list, time, safeHref, count, quote, type RecordData } from "./model";
import "./index.css";
declare global {
  interface Window {
    __STATE__?: RecordData;
  }
}
const destinations = [
  {
    id: "overview",
    title: "Overview",
    icon: LayoutDashboard,
    subtitle: "Your projects, priorities, and the work moving them forward.",
  },
  {
    id: "learnings",
    title: "Learnings",
    icon: BookOpen,
    subtitle: "What your projects have learned, ready for the next job.",
  },
  {
    id: "agents",
    title: "Agents",
    icon: Bot,
    subtitle: "Specialists, their hires, and the complete job ledger.",
  },
  {
    id: "usage",
    title: "Usage",
    icon: ChartNoAxesCombined,
    subtitle: "Subscription capacity, measured spend, and the cost of a job.",
  },
  {
    id: "imports",
    title: "Import checklist",
    icon: FolderInput,
    subtitle: "Discover repositories and choose what joins your workspace.",
  },
];
const getStorage = (key: string) => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};
const setStorage = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* Storage may be disabled. The current session remains usable. */
  }
};
function readRoute() {
  const p = new URLSearchParams(location.hash.slice(1));
  return {
    view: destinations.some((d) => d.id === p.get("view"))
      ? p.get("view")!
      : "overview",
    node: p.get("node"),
  };
}
function Navigation({
  s,
  view,
  onView,
  onNotice,
}: {
  s: RecordData | null;
  view: string;
  onView: (id: string) => void;
  onNotice: (text: string) => void;
}) {
  const { setOpenMobile, isMobile } = useSidebar();
  return (
    <Sidebar variant="inset" collapsible="icon" className="app-sidebar">
      <SidebarHeader>
        {isMobile && (
          <div className="mobile-nav-heading">
            <strong>Workspace</strong>
            <Button
              variant="ghost"
              size="icon"
              aria-label="Close navigation"
              onClick={() => setOpenMobile(false)}
            >
              <X size={18} />
            </Button>
          </div>
        )}
        <DropdownMenu>
          <DropdownMenuTrigger
            render={<Button variant="outline" className="workspace-switcher" />}
          >
            <span className="brand-tile">
              <Brain size={20} />
            </span>
            <span className="workspace-name">
              <strong>{s?.brain?.title || "Brain"}</strong>
              <small>Personal workspace</small>
            </span>
            <ChevronDown size={14} />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuItem disabled>
              Current workspace: {s?.brain?.title || "Brain"}
            </DropdownMenuItem>
            {(s?.brain?.others || []).map((name: string) => (
              <DropdownMenuItem
                key={name}
                onClick={async () => {
                  const command = "brain -b " + quote(name) + " ui";
                  try {
                    await navigator.clipboard.writeText(command);
                    onNotice(
                      "Workspace launch command copied. Run it in your terminal.",
                    );
                  } catch {
                    onNotice("Run this command in your terminal: " + command);
                  }
                }}
              >
                Copy launch command · {name}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Workspace</SidebarGroupLabel>
          <nav aria-label="Workspace navigation">
            <SidebarMenu
              onKeyDown={(e) => {
                const keys = [
                  "ArrowDown",
                  "ArrowRight",
                  "ArrowUp",
                  "ArrowLeft",
                  "Home",
                  "End",
                ];
                if (!keys.includes(e.key)) return;
                const buttons = Array.from(
                    e.currentTarget.querySelectorAll<HTMLButtonElement>(
                      "button",
                    ),
                  ),
                  index = buttons.indexOf(
                    document.activeElement as HTMLButtonElement,
                  );
                if (index < 0) return;
                e.preventDefault();
                const next =
                  e.key === "Home"
                    ? 0
                    : e.key === "End"
                      ? buttons.length - 1
                      : (index +
                          (["ArrowDown", "ArrowRight"].includes(e.key)
                            ? 1
                            : -1) +
                          buttons.length) %
                        buttons.length;
                buttons[next].click();
                buttons[next].focus();
              }}
            >
              {destinations.map((d) => (
                <SidebarMenuItem key={d.id}>
                  <SidebarMenuButton
                    isActive={view === d.id}
                    tooltip={d.title}
                    aria-label={d.title}
                    aria-current={view === d.id ? "page" : undefined}
                    onClick={() => {
                      onView(d.id);
                      if (isMobile) setOpenMobile(false);
                    }}
                  >
                    <d.icon />
                    <span>{d.title}</span>
                    {d.id === "overview" && list(s?.inbox).length > 0 && (
                      <span className="nav-count">{s!.inbox.length}</span>
                    )}
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </nav>
        </SidebarGroup>
      </SidebarContent>
      <SidebarFooter>
        <div className="sidebar-note">
          <ShieldCheck size={16} />
          <div>
            <strong>Local & read-only</strong>
            <small>Your brain stays on this machine.</small>
          </div>
        </div>
        <SidebarMenu>
          <SidebarMenuItem>
            <div className="workspace-user">
              <span className="avatar">
                <Brain size={16} />
              </span>
              <span>
                <strong>{s?.brain?.title || "Workspace"}</strong>
                <small>{s?.brain?.machine || "Local machine"}</small>
              </span>
            </div>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>
    </Sidebar>
  );
}
function NodeDetail({
  s,
  node,
  onClose,
}: {
  s: RecordData;
  node: string | null;
  onClose: () => void;
}) {
  const n = list(s.nodes).find((n) => n.id === node);
  return (
    <Sheet
      open={!!n}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent
        className="node-sheet"
        side="right"
        initialFocus={() =>
          document.querySelector<HTMLElement>(".node-sheet [data-slot=sheet-close]")
        }
      >
        <SheetHeader>
          <SheetTitle>{n?.title || "Project details"}</SheetTitle>
          <SheetDescription>
            Project notes, decisions, and local work
          </SheetDescription>
        </SheetHeader>
        {n && (
          <div className="detail-scroll">
            <div className="detail-meta">
              <Status>
                {n.sealed
                  ? "Sealed"
                  : n.paused
                    ? "Paused"
                    : n.archived
                      ? "Archived"
                      : "Active"}
              </Status>
              {n.runtime && <Status>{n.runtime}</Status>}
              <dl className="detail-facts">
                <dt>Owners</dt>
                <dd>{(n.owners || []).join(", ") || "Not recorded"}</dd>
                <dt>Labels</dt>
                <dd>{(n.labels || []).join(" · ") || "None"}</dd>
                <dt>Updated</dt>
                <dd>{time(n.updated, s.brain?.tz)}</dd>
                <dt>Home</dt>
                <dd className="break-all">{n.home || "Not recorded"}</dd>
              </dl>
              {safeHref(n.coordinator) ? (
                <a href={safeHref(n.coordinator)}>
                  Open coordinator <ArrowUpRight size={14} />
                </a>
              ) : (
                <p>Coordinator not available on this machine</p>
              )}
              {safeHref(n.preview_url) && (
                <a href={safeHref(n.preview_url)}>
                  Open preview <ArrowUpRight size={14} />
                </a>
              )}
              {n.preview && (
                <p>
                  Preview: <Text>{n.preview}</Text>
                </p>
              )}
              {n.preview_tour && (
                <p>
                  Preview tour: <Text>{n.preview_tour}</Text>
                </p>
              )}
            </div>
            {list(n.blocked).length > 0 && (
              <PageSection title="Needs you">
                {n.blocked.map((t: string, i: number) => (
                  <p className="note-row" key={i}>
                    <Text>{t}</Text>
                  </p>
                ))}
              </PageSection>
            )}
            {list(n.inbox).length > 0 && (
              <PageSection title="Inbox">
                {n.inbox.map((t: string, i: number) => (
                  <p className="note-row" key={i}>
                    <Text zone={s.brain?.tz}>{t}</Text>
                  </p>
                ))}
              </PageSection>
            )}
            {Object.entries(n.status || {})
              .filter(([k]) => !/^blocked/i.test(k))
              .map(([k, v]) => (
                <PageSection key={k} title={k}>
                  <Markdown text={v} />
                </PageSection>
              ))}
            {list(n.limits).length > 0 && (
              <PageSection title="Limits">
                {n.limits.map((t: string, i: number) => (
                  <p className="note-row" key={i}>
                    <Text>{t}</Text>
                  </p>
                ))}
              </PageSection>
            )}
            {list(n.decisions).length > 0 && (
              <PageSection title="Latest decisions">
                {n.decisions.map((d: RecordData, i: number) => (
                  <div className="note-row" key={i}>
                    <strong>
                      <Text>{d.title}</Text>
                    </strong>
                    {d.said && (
                      <p>
                        <Text>{d.said}</Text>
                      </p>
                    )}
                    <small>{time(d.t || d.time, s.brain?.tz)}</small>
                  </div>
                ))}
              </PageSection>
            )}
            {list(n.jobs?.rows).length > 0 && (
              <PageSection
                title="Jobs"
                description={`${n.jobs.merged || 0} merged · ${n.jobs.abandoned || 0} abandoned · median ${n.jobs.median_hours ?? "unmeasured"} h`}
              >
                <DataTable
                  rows={[...n.jobs.rows].reverse()}
                  name="jobs"
                  columns={[
                    { key: "Job", label: "Job" },
                    {
                      key: "Started",
                      label: "Started",
                      render: (r) => time(r.Started, s.brain?.tz),
                    },
                    { key: "Hours", label: "Hours", numeric: true },
                    {
                      key: "Outcome",
                      label: "Outcome",
                      render: (r) => (
                        <div>
                          <Status>{r.Outcome || "Open"}</Status>
                          <Text>{r.Cause}</Text>
                        </div>
                      ),
                    },
                    { key: "Reviews", label: "Reviews", numeric: true },
                  ]}
                />
              </PageSection>
            )}
            {list(n.reports).length > 0 && (
              <PageSection title="Reports">
                {n.reports.map((r: RecordData, i: number) => (
                  <div className="note-row" key={i}>
                    <strong>
                      <Text>{r.title}</Text>
                    </strong>
                    <small className="break-all">{r.path}</small>
                  </div>
                ))}
              </PageSection>
            )}
            {n.never_name > 0 && (
              <PageSection title="Privacy guard">
                <p>
                  {count(n.never_name, "restricted word")} must stay out of this
                  project’s home.
                </p>
                <Command text={"brain guard " + n.id} />
              </PageSection>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
function App() {
  const [s, setState] = useState<RecordData | null>(window.__STATE__ || null),
    [connection, setConnection] = useState(
      window.__STATE__ ? "Snapshot" : "Connecting",
    ),
    [retry, setRetry] = useState(0),
    [route, setRoute] = useState(readRoute),
    [theme, setTheme] = useState(getStorage("brain-ui-theme") || "system"),
    [notice, setNotice] = useState(""),
    [notify, setNotify] = useState(
      getStorage("brain-notify") === "1" &&
        "Notification" in window &&
        Notification.permission === "granted",
    );
  const scroll = useRef<HTMLDivElement>(null),
    positions = useRef<Record<string, number>>({}),
    activeView = useRef(route.view);
  useLayoutEffect(() => {
    if (scroll.current)
      scroll.current.scrollTop = positions.current[route.view] || 0;
    activeView.current = route.view;
  }, [route.view]);
  useEffect(() => {
    const fn = () => {
      const next = readRoute();
      if (next.view !== activeView.current && scroll.current)
        positions.current[activeView.current] = scroll.current.scrollTop;
      setRoute(next);
    };
    addEventListener("hashchange", fn);
    return () => removeEventListener("hashchange", fn);
  }, []);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)"),
      apply = () =>
        document.documentElement.classList.toggle(
          "dark",
          theme === "dark" || (theme === "system" && media.matches),
        );
    apply();
    setStorage("brain-ui-theme", theme);
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
  useEffect(() => {
    if (window.__STATE__) return;
    const key = new URLSearchParams(location.search).get("k");
    if (!key) {
      setConnection("Permission required");
      return;
    }
    let source: EventSource | undefined,
      disposed = false;
    const controller = new AbortController();
    fetch("/api/state?k=" + encodeURIComponent(key), {
      signal: controller.signal,
    })
      .then(async (r) => {
        if (r.status === 403) {
          setConnection("Permission required");
          return;
        }
        if (!r.ok) throw Error();
        const data = await r.json();
        if (disposed) return;
        setState(data);
        setConnection("Live");
        source = new EventSource("/api/events?k=" + encodeURIComponent(key));
        source.onmessage = (e) => {
          try {
            setState(JSON.parse(e.data));
            setConnection("Live");
          } catch {
            setConnection("Updates interrupted");
          }
        };
        source.onerror = () => setConnection("Updates interrupted");
      })
      .catch((e) => {
        if (e.name !== "AbortError") setConnection("Connection failed");
      });
    return () => {
      disposed = true;
      controller.abort();
      source?.close();
    };
  }, [retry]);
  useEffect(() => {
    const t = setInterval(
      () => setState((prev) => (prev ? { ...prev } : prev)),
      30000,
    );
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!s || !notify) return;
    const key =
      "brain-inbox-seen:" + JSON.stringify([s.brain?.name, s.brain?.root]);
    let ids: string[] = [];
    try {
      const saved = JSON.parse(getStorage(key) || "[]");
      ids = Array.isArray(saved)
        ? saved.filter((id) => typeof id === "string")
        : [];
    } catch {}
    const seen = new Set(ids);
    for (const item of list(s.inbox)) {
      if (!item.id || seen.has(item.id)) continue;
      try {
        new Notification(`${s.brain?.title}: a project needs you`, {
          body: String(item.text || "").slice(0, 200),
          tag: item.id,
        });
        seen.add(item.id);
      } catch {}
    }
    setStorage(key, JSON.stringify([...seen]));
  }, [s, notify]);
  const go = (view: string) => {
    location.hash = "view=" + view;
  };
  const node = (id: string | null) => {
    const hash =
      "view=" + route.view + (id ? "&node=" + encodeURIComponent(id) : "");
    history.replaceState(null, "", "#" + hash);
    setRoute({ ...route, node: id });
  };
  const current = destinations.find((d) => d.id === route.view)!;
  const enableNotifications = async () => {
    if (notify) {
      setNotify(false);
      setStorage("brain-notify", "0");
      return;
    }
    if (!("Notification" in window)) {
      setNotice(
        "Notifications are unavailable in this browser. Keep the Inbox open to see questions.",
      );
      return;
    }
    let permission: NotificationPermission;
    try {
      permission = await Notification.requestPermission();
    } catch {
      setNotice(
        "Notifications could not be enabled. Check browser settings and try again.",
      );
      return;
    }
    if (permission === "granted") {
      setNotify(true);
      setStorage("brain-notify", "1");
    } else
      setNotice(
        "Notifications are blocked. Allow them in browser settings or check the Inbox here.",
      );
  };
  return (
    <TimeZoneContext.Provider value={s?.brain?.tz}>
      <TooltipProvider>
        <SidebarProvider>
          <Navigation
            s={s}
            view={route.view}
            onView={go}
            onNotice={setNotice}
          />
          <SidebarInset className="app-inset">
            <div className="topbar">
              <div className="topbar-left">
                <SidebarTrigger />
                <span className="topbar-divider" />
                <span className="topbar-title">{current.title}</span>
              </div>
              <div className="topbar-actions">
                <Status>{connection}</Status>
                <Tip text="Switch light and dark appearance">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Toggle theme"
                    onClick={() =>
                      setTheme(
                        document.documentElement.classList.contains("dark")
                          ? "light"
                          : "dark",
                      )
                    }
                  >
                    {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
                  </Button>
                </Tip>
                <Tip
                  text={
                    notify
                      ? "Turn off notifications"
                      : "Notify me about new inbox items"
                  }
                >
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={
                      notify ? "Turn off notifications" : "Enable notifications"
                    }
                    onClick={enableNotifications}
                  >
                    <Bell size={17} />
                    {notify && <span className="notification-dot" />}
                  </Button>
                </Tip>
              </div>
            </div>
            <div ref={scroll} className="inset-scroll" id="workspace-content">
              <PageHeader
                title={current.title}
                subtitle={
                  route.view === "overview" && s
                    ? `${count(list(s.inbox).length, "item")} in your inbox · ${count(list(s.nodes).filter((n) => !n.archived).length, "project")} in your workspace.`
                    : current.subtitle
                }
                actions={
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      if (window.__STATE__)
                        setNotice(
                          "This is a saved snapshot. Open brain ui for live updates.",
                        );
                      else setRetry(retry + 1);
                    }}
                  >
                    <RefreshCw size={14} />
                    Refresh
                  </Button>
                }
              />
              {notice && (
                <Alert className="notice">
                  <AlertTitle>Workspace notice</AlertTitle>
                  <AlertDescription>
                    {notice}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setNotice("")}
                    >
                      Dismiss
                    </Button>
                  </AlertDescription>
                </Alert>
              )}
              {[
                "Permission required",
                "Connection failed",
                "Updates interrupted",
              ].includes(connection) && (
                <Alert
                  variant={
                    connection === "Updates interrupted"
                      ? "default"
                      : "destructive"
                  }
                >
                  <AlertTitle>
                    {connection === "Permission required"
                      ? "Open your private workspace link"
                      : connection === "Updates interrupted"
                        ? "Live updates interrupted"
                        : "Could not reach your brain"}
                  </AlertTitle>
                  <AlertDescription>
                    {connection === "Permission required"
                      ? "Run brain ui and open the complete address it prints. The key protects your workspace."
                      : connection === "Updates interrupted"
                        ? "You are viewing the last successful reading. Updates will reconnect automatically, or you can refresh now."
                        : "Check that brain ui is still running, then refresh this page."}
                  </AlertDescription>
                </Alert>
              )}
              {!s ? (
                connection === "Connecting" ? (
                  <div
                    className="page-stack skeleton-page"
                    aria-label="Loading workspace"
                  >
                    <div className="metric-grid">
                      {[0, 1, 2, 3].map((i) => (
                        <Skeleton key={i} className="h-32 rounded-xl" />
                      ))}
                    </div>
                    <Skeleton className="h-64 rounded-2xl" />
                    <Skeleton className="h-80 rounded-2xl" />
                  </div>
                ) : (
                  <Empty
                    text="Workspace unavailable."
                    action="Open the address printed by brain ui."
                  />
                )
              ) : (
                <>
                  <div data-page="overview" hidden={route.view !== "overview"}>
                    <Overview s={s} onNode={node} onView={go} />
                  </div>
                  <div
                    data-page="learnings"
                    hidden={route.view !== "learnings"}
                  >
                    <Learnings s={s} />
                  </div>
                  <div data-page="agents" hidden={route.view !== "agents"}>
                    <Agents s={s} />
                  </div>
                  <div data-page="usage" hidden={route.view !== "usage"}>
                    <Usage s={s} />
                  </div>
                  <div data-page="imports" hidden={route.view !== "imports"}>
                    <Imports s={s} />
                  </div>
                </>
              )}
              <footer className="workspace-footer">
                <span>
                  <Monitor size={13} />
                  Local workspace · read-only
                </span>
                <span>
                  {s?.brain?.now
                    ? time(s.brain.now, s.brain.tz)
                    : "Waiting for a reading"}{" "}
                  ·{" "}
                  <a
                    href="/assets/THIRD_PARTY_NOTICES.txt"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Third-party notices
                  </a>
                </span>
              </footer>
            </div>
            <div
              id="action-dock-host"
              className="action-dock-host"
              hidden={route.view !== "imports" || !s}
            />
            {s && (
              <NodeDetail s={s} node={route.node} onClose={() => node(null)} />
            )}
          </SidebarInset>
        </SidebarProvider>
      </TooltipProvider>
    </TimeZoneContext.Provider>
  );
}
class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: boolean }
> {
  state = { error: false };
  static getDerivedStateFromError() {
    return { error: true };
  }
  render() {
    return this.state.error ? (
      <main className="fatal-error">
        <h1>This view could not be displayed.</h1>
        <p>Refresh to load the latest workspace reading.</p>
        <Button onClick={() => location.reload()}>Refresh workspace</Button>
      </main>
    ) : (
      this.props.children
    );
  }
}
createRoot(document.getElementById("root")!).render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>,
);
