// Evaluate this expression in the browser at ui/tests/fixture.py's /?k=demo.
// Uses public DOM interactions only; returns evidence or throws on the first regression.
(async () => {
  const checks = [];
  const check = (condition, message) => {
    if (!condition) throw new Error(message);
    checks.push(message);
  };
  const wait = async (predicate, message) => {
    for (let i = 0; i < 100; i++) {
      if (predicate()) return;
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    throw new Error("Timed out: " + message);
  };
  const visible = (el) =>
    !!el?.getClientRects().length &&
    getComputedStyle(el).visibility !== "hidden";
  const button = (text, root = document) =>
    [...root.querySelectorAll("button")].find(
      (e) =>
        visible(e) &&
        (e.getAttribute("aria-label") === text ||
          e.textContent.trim() === text),
    );
  const go = async (view) => {
    location.hash = "view=" + view;
    await wait(
      () => !document.querySelector(`[data-page="${view}"]`)?.hidden,
      "navigate " + view,
    );
    document.querySelector("#workspace-content").scrollTop = 0;
  };
  const setInput = (el, value) => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    ).set.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const choose = async (name, text) => {
    [...document.querySelectorAll("[role=combobox]")]
      .find((e) => visible(e) && e.getAttribute("aria-label") === name)
      .click();
    await wait(
      () =>
        [...document.querySelectorAll("[role=option]")].some(
          (e) => e.textContent.trim() === text,
        ),
      "options " + name,
    );
    [...document.querySelectorAll("[role=option]")]
      .find((e) => e.textContent.trim() === text)
      .click();
    await new Promise((r) => setTimeout(r, 50));
  };
  const scroller = document.querySelector("#workspace-content");
  await wait(() => document.querySelector("[data-page=overview]"), "state");
  check(
    !document.querySelector(
      "select,details,summary,input[type=date],input[type=time],input[type=color],input[type=file]",
    ),
    "no stock controls or disclosures",
  );
  check(!document.querySelector("[title]"), "no native title tooltips");
  check(
    getComputedStyle(document.body).overflow === "hidden" &&
      getComputedStyle(document.documentElement).overflow === "hidden",
    "body and html never scroll",
  );
  for (const view of ["overview", "learnings", "agents", "usage", "imports"]) {
    await go(view);
    check(
      document.querySelector("h1").textContent ===
        {
          overview: "Overview",
          learnings: "Learnings",
          agents: "Agents",
          usage: "Usage",
          imports: "Import checklist",
        }[view],
      "view " + view + " deep links",
    );
    check(
      document.documentElement.scrollWidth <= innerWidth,
      "no document overflow: " + view,
    );
    check(
      scroller.scrollWidth <= scroller.clientWidth + 1,
      "no inset overflow: " + view,
    );
    const stray = [...document.querySelectorAll("*")].filter(
      (e) =>
        visible(e) &&
        e !== scroller &&
        e.scrollHeight > e.clientHeight + 2 &&
        ["auto", "scroll"].includes(getComputedStyle(e).overflowY),
    );
    check(stray.length === 0, "one active page scroller: " + view);
    const header = document.querySelector(".topbar").getBoundingClientRect(),
      page = document.querySelector(".page-header").getBoundingClientRect();
    check(
      Math.abs(
        page.left -
          header.left -
          parseFloat(
            getComputedStyle(document.querySelector(".topbar")).paddingLeft,
          ),
      ) < 2,
      "matching topbar/content gutter: " + view,
    );
  }
  await go("overview");
  check(
    document
      .querySelector(".flight-list")
      .textContent.includes("narrow-screen navigation"),
    "long job state is readable",
  );
  check(
    !document
      .querySelector(".flight-list")
      .textContent.includes("job/complete-1"),
    "completed rows and worktrees excluded from in-flight",
  );
  const projectButton = button("Field Guide");
  projectButton.click();
  await wait(() => document.querySelector(".node-sheet"), "node detail");
  check(
    document
      .querySelector(".node-sheet")
      .textContent.includes("Owners: Morgan"),
    "node detail retains ownership",
  );
  check(location.hash.includes("node=field-guide"), "node details deep link");
  document.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
  );
  await wait(
    () => !visible(document.querySelector(".node-sheet")),
    "close node detail",
  );
  await go("learnings");
  check(
    document
      .querySelector(".learning-entry")
      .textContent.includes("awaiting review"),
    "proposals sort first",
  );
  button("Read learning").click();
  await wait(
    () => document.querySelector(".learning-entry .markdown"),
    "learning reader",
  );
  check(
    document
      .querySelector(".learning-entry .markdown")
      .textContent.includes("<script>literal, untrusted content</script>"),
    "untrusted code stays literal",
  );
  button("Review and decide").click();
  await wait(
    () => document.querySelector(".learning-entry .command input"),
    "review commands",
  );
  check(
    document.querySelector(".learning-entry .command input").value ===
      "brain item accept --reviewed-by 'REVIEWER' -- 'lesson-0'",
    "accept command preserved",
  );
  check(
    [...document.querySelectorAll(".learning-entry .command input")].some(
      (e) => e.value === "brain item reject -- 'lesson-0'",
    ),
    "reject command preserved",
  );
  await choose("State", "Accepted (5)");
  check(
    !document
      .querySelector(".learning-results")
      .textContent.includes("awaiting review"),
    "custom select filters knowledge",
  );
  button("Clear filters").click();
  const search = document.querySelector(
    'input[placeholder="Search learnings"]',
  );
  setInput(search, "filters");
  await wait(
    () => document.querySelector(".results-meta").textContent === "1 learning",
    "search",
  );
  await go("usage");
  await go("learnings");
  check(search.value === "filters", "search survives navigation");
  check(
    document.querySelector(".results-meta").textContent === "1 learning",
    "filtered results survive navigation",
  );
  button("Clear filters").click();
  await go("agents");
  check(
    document
      .querySelector("[data-page=agents]")
      .textContent.includes("Review navigation and focus order"),
    "specialist hires retain tasks",
  );
  check(
    document
      .querySelector("[data-page=agents]")
      .textContent.includes("Median 1.6 h"),
    "job medians present",
  );
  const table = document.querySelector(".ledger-entry .data-table");
  check(
    table.querySelectorAll("tbody tr").length === 10,
    "ledger defaults to ten rows",
  );
  button("Next", table).click();
  await wait(
    () => table.querySelectorAll("tbody tr").length === 4,
    "ledger paging",
  );
  check(
    table.querySelector(".table-footer").textContent.includes("11–14 of 14"),
    "ledger pages the complete history",
  );
  await go("usage");
  const usage = document.querySelector("[data-page=usage]");
  check(
    usage.textContent.includes("Stale") &&
      usage.textContent.includes("No window"),
    "stale and absent subscription readings distinct",
  );
  check(
    usage.textContent.includes("Design model"),
    "per-model weekly window present",
  );
  check(
    usage.querySelectorAll(".window-meter").length === 5,
    "all subscription windows present",
  );
  check(
    usage.textContent.includes("1 other subscription hidden"),
    "only count of hidden subscriptions shown",
  );
  check(
    usage.querySelectorAll(".runtime-spend").length === 2,
    "spend per runtime",
  );
  check(
    usage.textContent.includes("Jobs: time and tokens"),
    "measured job usage preserved",
  );
  check(
    usage.querySelector(".stop-line").style.left === "85%",
    "configured stop marker",
  );
  await go("imports");
  const boxes = [
    ...document.querySelectorAll("[data-page=imports] [role=checkbox]"),
  ];
  check(
    boxes.length === 3 && boxes[1].getAttribute("aria-disabled") === "true" && boxes[2].getAttribute("aria-disabled") === "true",
    "owned repositories cannot be selected",
  );
  boxes[0].click();
  await wait(
    () => document.querySelector(".action-dock .command input"),
    "import selection",
  );
  check(
    document.querySelector(".action-dock .command input").value ===
      "brain adopt --parent 'root' -- '/demo/pocket-atlas'",
    "adopt command preserves exact arguments",
  );
  check(
    !scroller.contains(document.querySelector(".action-dock")),
    "action dock sits outside scroller",
  );
  const dockTop = document
    .querySelector(".action-dock-host")
    .getBoundingClientRect().top;
  for (const fraction of [0, 0.5, 1]) {
    scroller.scrollTop =
      (scroller.scrollHeight - scroller.clientHeight) * fraction;
    await new Promise((r) => requestAnimationFrame(r));
    check(
      Math.abs(
        document.querySelector(".action-dock-host").getBoundingClientRect()
          .top - dockTop,
      ) < 1,
      "dock stable at scroll " + fraction,
    );
  }
  await go("overview");
  await go("imports");
  check(
    document
      .querySelector(".action-dock .command input")
      .value.includes("pocket-atlas"),
    "selection survives navigation",
  );
  button("Clear", document.querySelector(".action-dock")).click();
  await wait(()=>!document.querySelector(".action-dock .command input"),"clear import selection");
  check(!document.querySelector(".action-dock .command input"),"clear import selection");
  check(
    !document.querySelector("[data-ui-attack]") && !window.uiAttack,
    "source data cannot create executable markup",
  );
  const remote = performance
    .getEntriesByType("resource")
    .filter((e) => new URL(e.name).origin !== location.origin);
  check(remote.length === 0, "fonts scripts and styles stay local");
  await go("overview");
  const geometry = {
    viewport: [innerWidth, innerHeight],
    scrollers: 1,
    gutter: parseFloat(getComputedStyle(scroller).paddingLeft),
    controls: [
      ...new Set(
        [
          ...document.querySelectorAll(
            "button,input,[role=combobox],[role=checkbox],a",
          ),
        ]
          .filter(visible)
          .map((e) => e.getAttribute("role") || e.tagName.toLowerCase()),
      ),
    ],
  };
  return { passed: checks.length, checks, geometry };
})();
