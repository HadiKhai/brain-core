// ui-designer §15 "lean structure" rules as mechanical checks. Each function runs inside the
// page (it is serialised by Playwright), so it must stay self-contained: no imports, no closures.
// The checks read only rendered geometry and computed style, never class names of one build.
export type Violation = { rule: string; detail: string };

export function audit(rule: string): Violation[] {
  const out: Violation[] = [];
  const add = (detail: string) => out.push({ rule, detail });
  const scroller = document.querySelector<HTMLElement>("#workspace-content")!;
  const inSidebar = (e: Element) => !!e.closest("[data-slot=sidebar], [data-sidebar=sidebar]");
  const inModal = (e: Element) => !!e.closest("[role=dialog], [role=alertdialog], [role=listbox], [role=menu], [data-slot=tooltip-content], [data-slot=popover-content]");
  const visible = (e: Element) => {
    const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none" && !e.closest("[hidden]");
  };
  const page = () => [...document.querySelectorAll<HTMLElement>("[data-page]")].find(visible) || null;
  const name = (e: Element | null | undefined) => (e?.textContent || "").replace(/\s+/g, " ").trim();
  const canvas = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!;
  const rgba = (color: string) => {
    canvas.clearRect(0, 0, 1, 1);
    canvas.fillStyle = "rgba(0,0,0,0)";
    canvas.fillStyle = color;
    canvas.fillRect(0, 0, 1, 1);
    return [...canvas.getImageData(0, 0, 1, 1).data];
  };
  const painted = (e: Element) => {
    const cs = getComputedStyle(e);
    return rgba(cs.backgroundColor)[3] > 10 || cs.backgroundImage !== "none";
  };
  const differs = (a: number[], b: number[]) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]) + Math.abs(a[2] - b[2]) > 6;
  const control = (e: Element) => e.matches("button, a, input, textarea, select, [role=combobox], [role=checkbox], [role=tab], [role=progressbar], [data-slot=badge], code, kbd, pre");
  const scrollable = (e: Element) => {
    const cs = getComputedStyle(e);
    return /(auto|scroll)/.test(cs.overflowY + " " + cs.overflowX);
  };
  const overflowing = (e: Element) => scrollable(e) && (e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1);
  const itemSelector = "article, li, tbody > tr, [role=row]:not(:has([role=columnheader])), [role=listitem], [data-item]";
  const items = (root: Element) => [...root.querySelectorAll(itemSelector)].filter((e) => visible(e) && !inSidebar(e) && !inModal(e) && !e.parentElement!.closest(itemSelector));
  // A surface is a painted box big enough to hold content whose tone differs from what it sits on.
  const surfaces = (root: Element) => {
    const found: { el: Element; depth: number }[] = [];
    const walk = (e: Element, tone: number[], depth: number) => {
      for (const child of e.children) {
        if (!visible(child) || inModal(child)) continue;
        let nextTone = tone, nextDepth = depth;
        const r = child.getBoundingClientRect();
        if (!control(child) && r.width >= 120 && r.height >= 40 && painted(child)) {
          const own = rgba(getComputedStyle(child).backgroundColor);
          if (own[3] > 10 && differs(own, tone)) {
            nextTone = own;
            nextDepth = depth + 1;
            found.push({ el: child, depth: nextDepth });
          }
        }
        walk(child, nextTone, nextDepth);
      }
    };
    walk(root, rgba(getComputedStyle(scroller).backgroundColor), 0);
    return found;
  };
  const describe = (e: Element) => `${e.tagName.toLowerCase()}${e.id ? "#" + e.id : ""}${e.getAttribute("data-slot") ? "[" + e.getAttribute("data-slot") + "]" : ""} “${name(e).slice(0, 48)}”`;
  const p = page();
  if (!p && rule !== "scrollbars") return [{ rule, detail: "no visible [data-page] for the current route" }];

  if (rule === "one-title") {
    const h1s = [...document.querySelectorAll("h1")].filter((e) => visible(e) && !inSidebar(e) && !inModal(e));
    if (h1s.length !== 1) add(`${h1s.length} visible h1 elements; a page has exactly one title`);
    const title = name(h1s[0]).toLowerCase();
    if (title) {
      const inset = document.querySelector("[data-slot=sidebar-inset]") || document.body;
      const repeats = [...inset.querySelectorAll("*")].filter((e) => {
        if (!visible(e) || inModal(e)) return false;
        const r = e.getBoundingClientRect();
        if (r.top >= innerHeight || r.bottom <= 0) return false;
        // Adjacent text nodes form one run (React splits "{n} learnings"); an element child ends a run.
        const runs: string[] = [""];
        for (const n of e.childNodes) n.nodeType === 3 ? (runs[runs.length - 1] += n.textContent) : runs.push("");
        return runs.some((t) => t.replace(/\s+/g, " ").trim().toLowerCase() === title);
      });
      if (repeats.length > 1) add(`page name “${title}” is visible ${repeats.length} times above the fold: ${repeats.map(describe).join(", ")}`);
      const header = h1s[0].closest("header") || h1s[0].parentElement!;
      const subtitle = [...header.querySelectorAll("p")].find(visible);
      const words = title.split(/\W+/).filter((w) => w.length > 2);
      if (subtitle && words.length && words.every((w) => name(subtitle).toLowerCase().includes(w)))
        add(`subtitle “${name(subtitle)}” restates the title`);
    }
  }

  if (rule === "options") {
    const lists = [...document.querySelectorAll("[role=listbox], [role=menu]")].filter(visible);
    for (const list of lists) {
      if (list.scrollWidth > list.clientWidth + 1) add(`menu ${describe(list)} scrolls sideways`);
      const box = list.getBoundingClientRect();
      for (const option of list.querySelectorAll("[role=option], [role=menuitem]")) {
        const r = option.getBoundingClientRect();
        if (r.right > box.right + 1 || r.left < box.left - 1) add(`option ${describe(option)} is wider than its menu`);
        for (const e of [option, ...option.querySelectorAll("*")]) {
          if (e.scrollWidth > e.clientWidth + 1 && getComputedStyle(e).textOverflow !== "ellipsis" && e.clientWidth > 0)
            add(`option text ${describe(e)} overflows without an ellipsis (${e.scrollWidth} > ${e.clientWidth})`);
        }
      }
    }
  }

  if (rule === "scrollbars") {
    const all = [document.documentElement, document.body, ...document.querySelectorAll("*")];
    for (const e of all) {
      if (!visible(e) || !scrollable(e)) continue;
      if (e !== document.documentElement && e !== document.body && !overflowing(e) && getComputedStyle(e).overflowY !== "scroll") continue;
      const cs = getComputedStyle(e) as CSSStyleDeclaration & { scrollbarColor: string; scrollbarWidth: string };
      if (cs.scrollbarWidth !== "thin" || !cs.scrollbarColor || cs.scrollbarColor === "auto")
        add(`${describe(e)} uses the platform scrollbar (width ${cs.scrollbarWidth}, colour ${cs.scrollbarColor})`);
    }
  }

  if (rule === "hierarchy") {
    const top = [...p!.querySelectorAll("section, [data-section]")].filter((e) => visible(e) && !inModal(e) && !e.parentElement!.closest("section, [data-section]"));
    if (top.length > 3) add(`${top.length} top-level sections on one page: ${top.map(describe).join(", ")}`);
    // A full list has its own pager; a preview ("Showing 5 of 31 · View all") does not count.
    const paged = top.filter((e) => [...e.querySelectorAll("button")].some((b) => visible(b) && /^(next|previous)$/i.test(name(b))));
    if (paged.length > 1) add(`${paged.length} independent full lists share one page; they are sub-areas that need their own sidebar items`);
  }

  if (rule === "job") {
    const main = [...p!.children].filter((e) => !e.matches("header") && !e.querySelector("h1"));
    const content = "table, [role=table], [role=grid], li, [role=listitem], article, [data-item], input:not([type=hidden]), textarea, [role=combobox], [role=checkbox], [role=tab], svg[role=img], canvas, [data-kpi]";
    const empty = main.flatMap((e) => [...e.querySelectorAll(".empty, [data-empty]")]).filter(visible);
    const data = main.flatMap((e) => [...e.querySelectorAll(content)]).filter((e) => visible(e) && !e.closest(".empty, [data-empty]") && !inModal(e));
    if (empty.length && !data.length) add(`the main content is only an empty state (${empty.map(describe).join(", ")}); fold it into its parent page`);
    if (!empty.length && !data.length) add("the main content has no list, data or form");
  }

  if (rule === "surfaces") {
    for (const { el, depth } of surfaces(p!)) {
      if (depth >= 3) add(`surface depth ${depth}: ${describe(el)}`);
      let only: Element = el;
      while (only.children.length === 1 && !only.matches(itemSelector) && !(only !== el && painted(only))) only = only.children[0];
      if (only !== el && (only.matches(itemSelector) || painted(only)) && name(only) === name(el))
        add(`box around a single item: ${describe(el)}`);
    }
    for (const e of p!.querySelectorAll("*")) {
      if (e === scroller || !visible(e) || inModal(e) || !overflowing(e)) continue;
      if (e.scrollHeight <= innerHeight * 1.5 && e.scrollHeight > e.clientHeight + 1)
        add(`inner scroll for short content (${e.scrollHeight}px ≤ 1.5 viewports): ${describe(e)}`);
    }
  }

  if (rule === "lean") {
    const heights = items(p!).map((e) => e.getBoundingClientRect().height).sort((a, b) => a - b);
    if (heights.length >= 2) {
      const median = heights[Math.floor(heights.length / 2)];
      if (median > innerHeight * 0.08) add(`median list item is ${Math.round(median)}px, over 8% of a ${innerHeight}px viewport`);
    }
  }

  if (rule === "chaining") {
    for (const e of [scroller, ...p!.querySelectorAll("*")]) {
      if (!visible(e) || inModal(e)) continue;
      const cs = getComputedStyle(e);
      if (/contain|none/.test(cs.overscrollBehaviorY) && scrollable(e)) add(`overscroll-behavior ${cs.overscrollBehaviorY} traps scrolling: ${describe(e)}`);
      if (e !== scroller && overflowing(e)) {
        let depth = 0;
        for (let a = e.parentElement; a && a !== scroller; a = a.parentElement) if (overflowing(a)) depth++;
        if (depth >= 1) add(`scroll area nested ${depth + 1} deep: ${describe(e)}`);
      }
    }
  }

  if (rule === "overview") {
    if (scroller.scrollHeight > scroller.clientHeight * 2 + 1) add(`overview needs ${(scroller.scrollHeight / scroller.clientHeight).toFixed(1)} viewports`);
    const parents = new Set(items(p!).map((e) => e.parentElement!));
    const lists = [...parents].filter((l) => items(l).length >= 2 && ![...parents].some((o) => o !== l && o.contains(l)));
    const bare = lists.filter((l) => {
      const section = l.closest("section, [data-section]") || l;
      return ![...section.querySelectorAll("a, button")].some((b) => /\b(view|see|show) all\b|^all\b/i.test(name(b)));
    });
    if (bare.length > 3) add(`${bare.length} inline lists without a “View all”`);
    const kpis = [...p!.querySelectorAll("[data-kpi]")].filter(visible);
    if (kpis.length < 3) add(`${kpis.length} KPIs; a decision dashboard leads with at least three`);
    if (!kpis.some((k) => k.querySelector("[data-trend]"))) add("no KPI shows a trend");
    if (![...p!.querySelectorAll("[data-segmented]")].some(visible)) add("no segmented state bar");
    const actionable = [...p!.querySelectorAll("[data-needs-action] " + "[data-item]")].filter(visible);
    if (!actionable.length || actionable.some((i) => !i.querySelector("a, button"))) add("the items needing action are missing, or one has no action");
  }
  return out;
}

// Routes are discovered from the sidebar: every destination and every sub-item.
export function sidebarTargets(): string[] {
  const buttons = [...document.querySelectorAll<HTMLElement>("[data-slot=sidebar] [data-slot=sidebar-menu-button], [data-slot=sidebar] [data-slot=sidebar-menu-sub-button]")];
  return buttons.filter((b) => b.getBoundingClientRect().height > 0 && !b.hasAttribute("data-external")).map((b, i) => {
    b.setAttribute("data-fitness-target", String(i));
    return String(i);
  });
}

// Sidebar hierarchy expectations from the state the page reads.
export function sidebarShape(state: any): Violation[] {
  const out: Violation[] = [];
  const add = (detail: string) => out.push({ rule: "hierarchy", detail });
  const side = document.querySelector("[data-slot=sidebar]");
  if (!side) return [{ rule: "hierarchy", detail: "no sidebar" }];
  const label = (e: Element) => (e.textContent || "").replace(/\s+/g, " ").trim();
  const entry = (text: string) => [...side.querySelectorAll("[data-slot=sidebar-menu-item]")].find((li) => {
    const button = li.querySelector(":scope > [data-slot=sidebar-menu-button], :scope > * > [data-slot=sidebar-menu-button]");
    return button && label(button).startsWith(text);
  });
  const agents = entry("Agents");
  const subs = agents ? [...agents.querySelectorAll("[data-slot=sidebar-menu-sub-button]")].map(label) : [];
  for (const want of ["Specialists", "Jobs", "Threads"])
    if (!subs.some((s) => s.startsWith(want))) add(`Agents has no “${want}” sub-item`);
  for (const n of (state.nodes || []).filter((n: any) => !n.archived)) {
    const item = entry(n.title);
    if (!item) { add(`project “${n.title}” is missing from the brain tree`); continue; }
    const threads = (n.work || []).filter((w: any) => w.thread && !/^(closed|merged|done|abandoned)/i.test(w.state || ""));
    const nested = item.querySelectorAll("[data-slot=sidebar-menu-sub-button]").length;
    if (threads.length && nested < threads.length) add(`project “${n.title}” shows ${nested} of ${threads.length} threads`);
  }
  return out;
}
