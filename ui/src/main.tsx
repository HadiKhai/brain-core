import { ReviewProvider } from "./review";
import { watchLive } from "./live";
import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Bell, Moon, RefreshCw, Sun } from "lucide-react";
import { SidebarProvider, SidebarInset, SidebarTrigger } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { TimeZoneContext, PageHeader, Status, Tip, Empty, type Crumb } from "./shared";
import { Navigation } from "./nav";
import { InboxView } from "./inbox";
import { Overview, greeting, overviewSummary } from "./overview";
import { Learnings } from "./learnings";
import { Specialists, Jobs, Threads } from "./agents";
import { Usage } from "./usage";
import { AddProjects } from "./imports";
import { Project, Projects, ancestors, projectState } from "./project";
import { Graph } from "./graph";
import { RecordPicker } from "./picker";
import { readRoute, writeRoute, type Route } from "./routes";
import { list, count, threads, learningRows, type RecordData } from "./model";
import "./index.css";
declare global {
  interface Window {
    __STATE__?: RecordData;
  }
}
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
const agentsCrumb = (go: (r: Partial<Route> & { view: string }) => void): Crumb[] => [{ label: "Agents", onClick: () => go({ view: "specialists" }) }];
// One title per page, with a subtitle of facts that never restates it.
function heading(s: RecordData | null, route: Route, go: (r: Partial<Route> & { view: string }) => void): { title: string; subtitle?: string; crumbs?: Crumb[] } {
  const nodes = list(s?.nodes), open = s ? threads(nodes) : [];
  switch (route.view) {
    case "inbox": return { title: "Inbox", subtitle: s ? `${count(list(s.inbox).length, "request")} · ${count(list(s.due).length, "coordinator step")}` : undefined };
    case "projects": return { title: "Projects", subtitle: s ? `${nodes.filter((n) => !n.archived).length} in this brain · ${count(open.length, "thread")} in flight` : undefined };
    case "project": {
      const n = nodes.find((x) => x.id === route.node);
      return { title: n?.title || "Project", crumbs: ancestors(nodes, n).map((p) => ({ label: p.title || "Project", onClick: () => go({ view: "project", node: p.id }) })),
        subtitle: n ? [projectState(n), n.runtime, n.owners?.length ? n.owners.join(", ") : "", n.updated_ago ? "updated " + n.updated_ago : ""].filter(Boolean).join(" · ") : undefined };
    }
    case "specialists": return { title: "Specialists", crumbs: agentsCrumb(go), subtitle: s ? `${count(list(s.specialists).length, "role")} · ${count(list(s.specialists).reduce((a, sp) => a + list(sp.jobs).length, 0), "hire")}` : undefined };
    case "jobs": return { title: "Jobs", crumbs: agentsCrumb(go), subtitle: s ? `${nodes.reduce((a, n) => a + list(n.jobs?.rows).length, 0)} recorded · ${nodes.reduce((a, n) => a + list(n.jobs?.open).length, 0)} open` : undefined };
    case "threads": return { title: "Threads", crumbs: agentsCrumb(go), subtitle: s ? `${open.length} open across ${count(new Set(open.map((f) => f.node.id)).size, "project")}` : undefined };
    case "usage": return { title: "Usage", crumbs: agentsCrumb(go), subtitle: s ? `${count(list(s.usage).length, "subscription")} · stop line ${s.usage_policy?.stop_percent ?? "unknown"}%` : undefined };
    case "learnings": { const rows = s ? learningRows(s) : []; return { title: "Learnings", subtitle: s ? `${count(rows.length, "item")} · ${rows.filter((r) => r.state === "proposed").length} proposed for review` : undefined }; }
    case "graph": return { title: "Graph", subtitle: s ? `${count(nodes.filter((n) => !n.archived).length, "project")} · ${count(list(s.specialists).length, "specialist")} · ${count(list(s.items?.all).length, "learning")}` : undefined };
    default: return { title: s ? greeting(s) : "Overview", subtitle: s ? overviewSummary(s) : undefined };
  }
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
    activeView = useRef(route.view + ":" + (route.node || ""));
  useLayoutEffect(() => {
    // Overlay scrollbars need no reserved space. CSS stable alone can still
    // allocate a classic-width strip on systems whose scrollbars overlay content.
    const measure = () => {
      const probe = document.createElement("div");
      probe.style.cssText = "position:fixed;top:-9999px;width:100px;height:100px;overflow:scroll;visibility:hidden;pointer-events:none";
      document.body.append(probe);
      const classic = probe.offsetWidth - probe.clientWidth > 0;
      probe.remove();
      if (scroll.current) scroll.current.dataset.classicScrollbars = String(classic);
    };
    measure();
    addEventListener("resize", measure);
    return () => removeEventListener("resize", measure);
  }, []);
  const pageKey = route.view + ":" + (route.node || "");
  useLayoutEffect(() => {
    if (scroll.current)
      scroll.current.scrollTop = positions.current[pageKey] || 0;
    activeView.current = pageKey;
  }, [pageKey]);
  useEffect(() => {
    const fn = () => {
      const next = readRoute();
      if (next.view + ":" + (next.node || "") !== activeView.current && scroll.current)
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
    return watchLive(key, setState, setConnection, notify);
  }, [retry, notify]);
  useEffect(() => {
    const t = setInterval(
      () => { if (!document.hidden) setState((prev) => (prev ? { ...prev } : prev)); },
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
  const go = (r: Partial<Route> & { view: string }) => {
    location.hash = writeRoute(r);
  };
  const setAdd = (open: boolean) => {
    const hash = writeRoute({ ...route, add: open });
    if (open) location.hash = hash;
    else { history.replaceState(null, "", hash); setRoute({ ...route, add: false }); }
  };
  const head = heading(s, route, go);
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
  const refresh = () => {
    if (window.__STATE__) setNotice("This is a saved snapshot. Open your live workspace to see updates.");
    else setRetry(retry + 1);
  };
  const v = route.view;
  return (
    <TimeZoneContext.Provider value={s?.brain?.tz}>
      <ReviewProvider enabled={!!s?.review?.enabled} catalogKey={JSON.stringify([list(s?.items?.all).filter(i => i.status === "proposed").map(i => [i.id, i.scope]), list(s?.specialists).filter(i => i.status === "proposed").map(i => [i.id, i.node, i.scope])])}><TooltipProvider>
        <SidebarProvider>
          <Navigation s={s} route={route} go={go} onAdd={() => setAdd(true)} onNotice={setNotice} />
          <SidebarInset className="app-inset">
            <div className="topbar">
              <div className="topbar-left">
                <SidebarTrigger />
                {s && <div className="global-search"><RecordPicker s={s} kind="all" label="Search the brain" hideLabel value={null} allLabel="Search the brain" placeholder="Projects, specialists, learnings, jobs"
                  onChange={(f) => {
                    if (!f) return;
                    if (f.kind === "node") go({ view: "project", node: f.id });
                    else if (f.kind === "job") go({ view: "project", node: f.id.split("/")[0], tab: "jobs" });
                    else if (f.kind === "specialist") go({ view: "specialists", item: f.id });
                    else go({ view: "learnings", item: f.id });
                  }} /></div>}
              </div>
              <div className="topbar-actions">
                <Status>{connection}</Status>
                <Tip text="Refresh the reading"><Button variant="ghost" size="icon" aria-label="Refresh" onClick={refresh}><RefreshCw size={16} /></Button></Tip>
                <Tip text="Switch light and dark appearance">
                  <Button variant="ghost" size="icon" aria-label="Toggle theme" onClick={() => setTheme(document.documentElement.classList.contains("dark") ? "light" : "dark")}>
                    {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
                  </Button>
                </Tip>
                <Tip text={notify ? "Turn off notifications" : "Notify me about new inbox items"}>
                  <Button variant="ghost" size="icon" aria-label={notify ? "Turn off notifications" : "Enable notifications"} onClick={enableNotifications}>
                    <Bell size={17} />
                    {notify && <span className="notification-dot" />}
                  </Button>
                </Tip>
              </div>
            </div>
            <div ref={scroll} className="inset-scroll" id="workspace-content" data-fill-page={["inbox", "specialists", "learnings", "graph", "threads", "jobs"].includes(v) || undefined}>
              <PageHeader title={head.title} subtitle={head.subtitle} crumbs={head.crumbs} actions={<div id="screen-action-host" />} />
              {notice && (
                <Alert className="notice" role="status">
                  <AlertTitle>Workspace notice</AlertTitle>
                  <AlertDescription>
                    {notice}
                    <Button variant="ghost" size="sm" onClick={() => setNotice("")}>Dismiss</Button>
                  </AlertDescription>
                </Alert>
              )}
              {["Permission required", "Connection failed", "Updates interrupted"].includes(connection) && (
                <Alert variant={connection === "Updates interrupted" ? "default" : "destructive"}>
                  <AlertTitle>{connection === "Permission required" ? "Open your private workspace link" : connection === "Updates interrupted" ? "Live updates interrupted" : "Could not reach your brain"}</AlertTitle>
                  <AlertDescription>
                    {connection === "Permission required" ? "Open the complete private address from the terminal where you started this dashboard."
                      : connection === "Updates interrupted" ? "You are viewing the last successful reading. Updates will reconnect automatically, or you can refresh now."
                      : "Check that your local dashboard is still running, then refresh this page."}
                  </AlertDescription>
                </Alert>
              )}
              {!s ? (
                connection === "Connecting" ? (
                  <div className="page-stack skeleton-page" aria-label="Loading workspace">
                    <Skeleton className="h-36 rounded-2xl" />
                    <div className="overview-grid"><Skeleton className="h-80 rounded-2xl" /><Skeleton className="h-80 rounded-2xl" /></div>
                  </div>
                ) : (
                  <Empty text="Workspace unavailable." action="Open the complete private address from your terminal." />
                )
              ) : (
                <>
                  <div data-page="overview" hidden={v !== "overview"}><Overview s={s} go={go} onAdd={() => setAdd(true)} active={v === "overview"} /></div>
                  <div data-page="inbox" hidden={v !== "inbox"}><InboxView s={s} go={go} tab={route.tab} active={v === "inbox"} /></div>
                  <div data-page="projects" hidden={v !== "projects"}><Projects s={s} go={go} onAdd={() => setAdd(true)} /></div>
                  {v === "project" && <div data-page="project"><Project key={route.node || ""} s={s} route={route} go={go} active /></div>}
                  <div data-page="specialists" hidden={v !== "specialists"}><Specialists s={s} active={v === "specialists"} item={v === "specialists" ? route.item : null} /></div>
                  <div data-page="jobs" hidden={v !== "jobs"}><Jobs s={s} go={go} active={v === "jobs"} /></div>
                  <div data-page="threads" hidden={v !== "threads"}><Threads s={s} go={go} active={v === "threads"} /></div>
                  <div data-page="usage" hidden={v !== "usage"}><Usage s={s} active={v === "usage"} /></div>
                  <div data-page="learnings" hidden={v !== "learnings"}><Learnings s={s} item={v === "learnings" ? route.item : null} active={v === "learnings"} initialState={route.view === "learnings" ? route.state : null} /></div>
                  {v === "graph" && <div data-page="graph"><Graph s={s} go={go} /></div>}
                  <AddProjects s={s} open={route.add} onOpenChange={setAdd} />
                </>
              )}
              <footer className="workspace-footer">
                <span>{s?.review?.enabled ? "Local proposal reviews enabled · " : "Read-only view of "}{s?.brain?.title || "your brain"} on this machine</span>
                <span>
                  {s?.brain?.now ? "Read " + new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: s.brain.tz || undefined }).format(new Date(s.brain.now)) : "Waiting for a reading"} ·{" "}
                  <a href="/assets/THIRD_PARTY_NOTICES.txt" target="_blank" rel="noreferrer">Third-party notices</a>
                </span>
              </footer>
            </div>
          </SidebarInset>
        </SidebarProvider>
      </TooltipProvider></ReviewProvider>
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
