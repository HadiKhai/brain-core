// Evaluate this expression in the browser at ui/tests/fixture.py's /?k=demo.
// Uses public DOM interactions only; returns evidence or throws on the first regression.
(async () => {
  const checks = [];
  const check = (condition, message) => {
    if (!condition) throw new Error(message);
    checks.push(message);
  };
  const wait = async (predicate, message) => {
    for (let i = 0; i < 150; i++) {
      if (predicate()) return;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error("Timed out: " + message);
  };
  const visible = (el) => !!el?.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
  const button = (text, root = document) =>
    [...root.querySelectorAll("button, a")].find((e) => visible(e) && (e.getAttribute("aria-label") === text || e.textContent.trim() === text));
  const page = (view) => document.querySelector(`[data-page="${view}"]`);
  const go = async (hash, view = hash) => {
    location.hash = "view=" + hash;
    await wait(() => page(view) && !page(view).hidden, "navigate " + hash);
    document.querySelector("#workspace-content").scrollTop = 0;
  };
  const setInput = (el, value) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const tab = async (root, label) => {
    const t = [...root.querySelectorAll("[role=tab]")].find((e) => e.textContent.trim().startsWith(label));
    t.click();
    await wait(() => t.getAttribute("aria-selected") === "true", "tab " + label);
  };
  const choose = async (name, text) => {
    [...document.querySelectorAll("[role=combobox]")].find((e) => visible(e) && e.getAttribute("aria-label") === name).click();
    await wait(() => [...document.querySelectorAll("[role=option]")].some((e) => e.textContent.trim().startsWith(text)), "options " + name);
    [...document.querySelectorAll("[role=option]")].find((e) => e.textContent.trim().startsWith(text)).click();
    await new Promise((r) => setTimeout(r, 80));
  };
  const scroller = document.querySelector("#workspace-content");
  await wait(() => page("overview"), "state");
  check(!document.querySelector("select,details,summary,input[type=date],input[type=time],input[type=color],input[type=file]"), "no stock controls or disclosures");
  check(!document.querySelector("[title]"), "no native title tooltips");
  check(getComputedStyle(document.body).overflow === "hidden" && getComputedStyle(document.documentElement).overflow === "hidden", "body and html never scroll");
  const workspaceGutter = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--workspace-gutter"));
  const toolbarGap = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--toolbar-gap"));
  // Mobile navigation is a sheet. Measure its actual menu inset once while open, then return
  // to the page before checking every top bar; do not infer geometry from a hidden sidebar.
  let sidebar = [...document.querySelectorAll("[data-slot=sidebar]")].find(visible);
  const openedSidebar = !sidebar;
  if (openedSidebar) {
    button("Toggle Sidebar").click();
    await wait(() => [...document.querySelectorAll("[data-slot=sidebar]")].some(visible), "open navigation for gutter measurement");
    sidebar = [...document.querySelectorAll("[data-slot=sidebar]")].find(visible);
  }
  const group = sidebar.querySelector("[data-slot=sidebar-group]");
  const menu = group.querySelector("[data-slot=sidebar-menu]");
  const sidebarGutter = menu.getBoundingClientRect().left - group.getBoundingClientRect().left;
  check(Number.isFinite(workspaceGutter) && Math.abs(sidebarGutter - workspaceGutter) <= 1, "sidebar menu inset follows the workspace gutter token");
  if (openedSidebar) {
    button("Close navigation", sidebar).click();
    await wait(() => !visible(sidebar), "close measured navigation");
  }
  const titles = { inbox: "Inbox", projects: "Projects", specialists: "Specialists", jobs: "Jobs", threads: "Threads", usage: "Usage", learnings: "Learnings", graph: "Graph" };
  for (const view of ["overview", ...Object.keys(titles)]) {
    await go(view);
    const h1 = [...document.querySelectorAll("h1")].filter(visible);
    check(h1.length === 1 && (view === "overview" ? /^Good (morning|afternoon|evening)$/.test(h1[0].textContent) : h1[0].textContent === titles[view]), "view " + view + " deep links with one title");
    check(document.documentElement.scrollWidth <= innerWidth, "no document overflow: " + view);
    check(scroller.scrollWidth <= scroller.clientWidth + 1, "no inset overflow: " + view);
    const stray = [...document.querySelectorAll("*")].filter((e) => visible(e) && e !== scroller && !e.closest("[role=dialog]") && e.scrollHeight > e.clientHeight + 2 && ["auto", "scroll"].includes(getComputedStyle(e).overflowY));
    const unexpected = stray.filter(e => !e.matches("[data-list-frame] > .list-scroll"));
    check(unexpected.length === 0, "only the page and shared table body scroll: " + view);
    check(stray.filter(e => e.matches(".list-scroll")).every(e => !/contain|none/.test(getComputedStyle(e).overscrollBehaviorY)), "shared table scroll chains to the page: " + view);
    const bar = document.querySelector(".topbar"), header = document.querySelector(".page-header").getBoundingClientRect();
    const toggle = bar.querySelector("[data-slot=sidebar-trigger]").getBoundingClientRect();
    const searchField = bar.querySelector(".global-search").getBoundingClientRect();
    const topbarGutter = toggle.left - bar.getBoundingClientRect().left;
    check(Math.abs(topbarGutter - workspaceGutter) <= 1 && Math.abs(topbarGutter - sidebarGutter) <= 1, "matching sidebar/topbar gutter token: " + view);
    check(Math.abs(searchField.left - toggle.right - toolbarGap) <= 1, "toggle-to-search gap follows its token: " + view);
    check(Math.abs(header.left - scroller.getBoundingClientRect().left - scroller.clientLeft - parseFloat(getComputedStyle(scroller).paddingLeft)) <= 1, "page title follows its content gutter: " + view);
  }
  await go("overview");
  const moving = page("overview").querySelector("[aria-label='Moving now']");
  check(moving.textContent.includes("Navigation"), "a running thread is listed under Moving now");
  check(!/complete 1/i.test(moving.textContent), "completed rows and worktrees excluded from in-flight");
  check(page("overview").querySelectorAll("[data-kpi]").length >= 5 && page("overview").querySelector("[data-segmented]") && page("overview").querySelector(".pulse-strip"), "overview leads with KPIs, segmented state and the pulse strip");
  const tree = document.querySelector("[data-slot=sidebar]");
  if (tree) button("Field Guide", tree).click();
  else location.hash = "view=project&node=field-guide";
  await wait(() => location.hash.includes("node=field-guide") && page("project"), "project page");
  check(location.hash.includes("view=project") && location.hash.includes("node=field-guide"), "project deep link");
  await tab(page("project"), "About");
  check(page("project").querySelector(".detail-facts").textContent.includes("Demo reviewer"), "project page retains ownership");
  await tab(page("project"), "Threads");
  check(page("project").textContent.includes("narrow-screen navigation"), "long job state is readable");
  await go("learnings");
  check(page("learnings").querySelector("[data-item]").textContent.includes("Proposed"), "proposals sort first");
  button("Review proposal", page("learnings").querySelector(".learning-results")).click();
  await wait(() => document.querySelector(".reading-sheet .markdown"), "learning reader");
  check(document.querySelector(".reading-sheet .markdown").textContent.includes("<script>literal, untrusted content</script>"), "untrusted code stays literal");
  button("View command", document.querySelector(".reading-sheet")).click();
  await wait(() => document.querySelector(".reading-sheet pre.verbatim"), "review commands");
  check(document.querySelector(".reading-sheet pre.verbatim").textContent === "brain item accept --reviewed-by 'REVIEWER' -- 'lesson-0'", "accept command preserved");
  await choose("Decision", "Reject");
  check(document.querySelector(".reading-sheet pre.verbatim").textContent === "brain item reject -- 'lesson-0'", "reject command preserved");
  button("Close", document.querySelector(".reading-sheet")).click();
  await wait(() => !visible(document.querySelector(".reading-sheet")), "close learning reader");
  await tab(page("learnings"), "Accepted");
  check(!page("learnings").querySelector(".learning-results .rows").textContent.includes("Proposed"), "state tabs filter knowledge");
  button("Clear filters").click();
  const search = document.querySelector('input[placeholder="Search learnings"]');
  setInput(search, "filters");
  await wait(() => page("learnings").querySelector(".list-header .list-count").textContent === "1–1 of 1 learning", "search");
  await go("usage");
  await go("learnings");
  check(search.value === "filters", "search survives navigation");
  check(page("learnings").querySelector(".list-header .list-count").textContent === "1–1 of 1 learning", "filtered results survive navigation");
  button("Clear filters").click();
  await go("specialists");
  button("Open specialist", page("specialists")).click();
  await wait(() => document.querySelector(".reading-sheet [data-slot=table]"), "specialist reader");
  check(document.querySelector(".reading-sheet").textContent.includes("Review navigation and focus order"), "specialist hires retain tasks");
  button("Close", document.querySelector(".reading-sheet")).click();
  await wait(() => !visible(document.querySelector(".reading-sheet")), "close specialist");
  await go("jobs");
  check(page("jobs").textContent.includes("1.6 h"), "job medians present");
  check(page("jobs").textContent.includes("162K"), "measured job tokens present");
  const table = page("jobs").querySelector(".data-table");
  check(table.querySelectorAll("tbody tr").length === 10, "ledger defaults to ten rows");
  button("Next", table).click();
  await wait(() => table.querySelectorAll("tbody tr").length === 4, "ledger paging");
  check(table.querySelector(".table-footer").textContent.includes("11–14 of 14"), "ledger pages the complete history");
  await go("usage");
  const usage = page("usage");
  check(usage.textContent.includes("Stale") && usage.textContent.includes("No window"), "stale and absent subscription readings distinct");
  check(usage.textContent.includes("Design model"), "per-model weekly window present");
  check(usage.querySelectorAll(".window-meter").length === 5, "all subscription windows present");
  check(usage.textContent.includes("1 other subscription hidden"), "only count of hidden subscriptions shown");
  check(usage.querySelectorAll(".runtime-spend").length === 2, "spend per runtime");
  check(usage.querySelector(".meter .tip-trigger").style.left === "85%", "configured stop marker");
  await go("overview");
  const addProjects = () => tree ? button("Add projects", tree).click() : (location.hash = "view=overview&add=1");
  addProjects();
  await wait(() => document.querySelector(".add-sheet [role=checkbox]"), "add projects sheet");
  const boxes = [...document.querySelectorAll(".add-sheet [role=checkbox]")];
  check(boxes.length === 3 && boxes[1].getAttribute("aria-disabled") === "true" && boxes[2].getAttribute("aria-disabled") === "true", "owned repositories cannot be selected");
  boxes[0].click();
  const dock = () => document.querySelector(".add-sheet .sheet-dock");
  await wait(() => button("Review import", dock()), "review import action");
  check(!scroller.contains(dock()) && !document.querySelector(".add-sheet .sheet-body").contains(dock()), "action dock sits outside the scrolling list");
  button("Review import", dock()).click();
  await wait(() => document.querySelector(".reading-sheet .command input"), "import selection");
  check(document.querySelector(".reading-sheet .command input").value === "brain adopt --parent 'root' -- '/demo/pocket-atlas'", "adopt command preserves exact arguments");
  button("Close", document.querySelector(".reading-sheet")).click();
  await wait(() => !visible(document.querySelector(".reading-sheet")), "close import reader");
  button("Close", document.querySelector(".add-sheet")).click();
  await wait(() => !visible(document.querySelector(".add-sheet")), "close add sheet");
  addProjects();
  await wait(() => dock(), "reopen add sheet");
  check(dock().textContent.includes("1 repository selected"), "selection survives closing the sheet");
  button("Clear", dock()).click();
  await wait(() => !button("Review import", dock()), "clear import selection");
  button("Close", document.querySelector(".add-sheet")).click();
  await wait(() => !visible(document.querySelector(".add-sheet")), "close add sheet again");
  check(!document.querySelector("[data-ui-attack]") && !window.uiAttack, "source data cannot create executable markup");
  const remote = performance.getEntriesByType("resource").filter((e) => new URL(e.name).origin !== location.origin);
  check(remote.length === 0, "fonts scripts and styles stay local");
  await go("overview");
  const geometry = {
    viewport: [innerWidth, innerHeight],
    scrollOwners: "page and shared table bodies",
    sidebarGutter,
    topbarGutter: workspaceGutter,
    gutter: parseFloat(getComputedStyle(scroller).paddingLeft),
    controls: [...new Set([...document.querySelectorAll("button,input,[role=combobox],[role=checkbox],a")].filter(visible).map((e) => e.getAttribute("role") || e.tagName.toLowerCase()))],
  };
  return { passed: checks.length, checks, geometry };
})();
