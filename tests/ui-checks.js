// Evaluate in a throwaway `brain ui` browser page. No packages or native notifications; test storage is isolated and removed.
(() => {
  const saved = { state: S, selected, url: location.href, notifyOn, firstPaint, notification: window.Notification };
  const checks = [];
  const originalFilter = {...learningFilter}, originalLimit = learningLimit, originalView = currentView, originalScan = [...scanSelected], originalParent = scanParent;
  const storageKeys = [];
  function check(condition, name) {
    if (!condition) throw new Error(name);
    checks.push(name);
  }
  function expected(date, zone, withDate = false) {
    return new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit", minute: "2-digit", hourCycle:"h23", timeZone: zone,
      ...(withDate ? { year: "numeric", month: "short", day: "numeric" } : {}),
    }).format(date);
  }
  try {
    S = structuredClone(S);
    notifyOn = false;
    for (const zone of ["UTC", "Asia/Beirut", "America/New_York"]) {
      S.brain.tz = zone;
      for (const stamp of ["2026-10-19T06:00:00Z", "2026-01-19T06:00:00Z"]) {
        check(brainTime(stamp) === expected(new Date(stamp), zone), `clock ${zone} ${stamp}`);
      }
      renderHeat();
      const tick = document.querySelector("#heat .tick");
      check(tick && tick.textContent === expected(new Date(S.activity.start * 1000), zone), `chart ${zone}`);
      const node = S.nodes[0], instant = new Date("2026-10-19T06:00:00Z");
      node.decisions = [{ title: "Timezone decision", time: "wrong raw timezone", t: instant.getTime() / 1000 }];
      node.jobs.rows = [{ Job: "timezone-job", Started: "2026-10-19T09:00:00+03:00", Hours: "1", Outcome: "merged", Reviews: "1" }];
      openDetail(node.id);
      const detail = document.getElementById("detail");
      check(Array.from(detail.querySelectorAll(".when")).some(e => e.textContent === moment(instant.getTime()/1000)), `decision ${zone}`);
      const row = Array.from(detail.querySelectorAll("tr")).find(e => e.textContent.includes("timezone-job"));
      check(row && row.children[1].textContent === moment(instant.getTime()/1000), `job ${zone}`);
    }
    closeDetail();
    check(location.search === new URL(saved.url).search, "closing notes preserves the API key");
    const poison = '<img data-ui-attack src=x onerror="window.uiAttack=1">';
    S.items.all = [{id:"proposal", title:poison, status:"proposed", kind:poison, body:poison,
      provenance:{node:"test", ref:poison, repo:poison, commit:poison, time:"2026-10-19T06:00:00Z"}},
      ...Array.from({length:500}, (_, i) => ({id:"large-" + i, title:"Learning " + i, status:i % 2 ? "quarantined" : "active", provenance:{node:"bulk"}}))];
    S.specialists = [{id:"reviewer", node:"test", title:poison, status:"active", runtime:poison,
      lessons:[{from:"test", time:"2026-10-19T06:00:00Z", text:poison}], jobs:[{node:S.nodes[0].id, job:"timezone-job", time:"2026-10-19T06:00:00Z", task:poison, subscription:poison}]}];
    Object.assign(learningFilter, {node:"", specialist:"", state:""}); learningLimit = 80;
    renderLearnings();
    check($("learning-results").querySelectorAll("article").length === 80, "large library renders a bounded first page");
    check($("learning-results").textContent.includes("awaiting review"), "proposals await review");
    check($("learning-results").querySelector("input").value === "brain item accept --reviewed-by 'REVIEWER' -- 'proposal'", "proposal accept command");
    check([...$("learning-results").querySelectorAll("input")].some(e => e.value === "brain item reject -- 'proposal'"), "proposal reject command");
    $("filter-state").value = "quarantined"; $("filter-state").dispatchEvent(new Event("change"));
    check($("learning-results").textContent.includes("250 learnings"), "state filter includes quarantined records");
    learningFilter.state = ""; $("filter-state").value = "";
    $("filter-specialist").value = "test/reviewer"; $("filter-specialist").dispatchEvent(new Event("change"));
    check($("learning-results").querySelectorAll("article").length === 1, "specialist lesson filter");
    check($("learning-results").textContent.includes("specialists/reviewer/lessons.md"), "lesson provenance");
    learningFilter.specialist = ""; $("filter-specialist").value = "";
    $("filter-node").value = "test"; $("filter-node").dispatchEvent(new Event("change"));
    check($("learning-results").querySelectorAll("article").length === 2, "node filter combines library and specialist lessons");
    renderAgents();
    check($("agent-results").textContent.includes("timezone-job"), "specialist hire joins the node ledger");
    check($("agent-results").textContent.includes(poison), "agent metadata remains literal text");
    check($("ledger").textContent.includes("median"), "node ledger shows medians");
    S.nodes[0].jobs.rows.push({Job:"timezone-job", Started:"2026-10-20T06:00:00Z", Ended:"2026-10-20T07:00:00Z", Outcome:"abandoned", Reviews:"9"});
    renderAgents();
    check(!$("agent-results").textContent.includes("abandoned"), "reused branch names do not reassign historical hire outcomes");
    const now = Date.now() / 1000;
    S.usage_policy = {stop_percent:73, max_age_seconds:120}; S.usage_other_subscriptions = 4;
    S.usage = [{id:"one", label:poison, runtime:"codex", brains:[poison], read_at:now - 600, source:poison,
      windows:{five_hour:{used_percent:80, resets_at:now+3600}, seven_day:{used_percent:50, resets_at:now+86400},
        "seven_day_model:test":{models:[poison], used_percent:40, resets_at:now+86400}}},
      {id:"two", label:"Fresh", runtime:"claude", brains:["test"], read_at:now, source:"lookup", windows:{five_hour:{absent:true}}}];
    renderUsage();
    check($("usage-results").querySelectorAll("article").length === 2, "one usage row per subscription");
    check($("usage-hidden").textContent === "4 other subscriptions hidden", "other subscriptions remain a count");
    check($("usage-results").textContent.includes("Stale"), "stale usage visibly stale");
    check($("usage-results").textContent.includes("5-hour · none"), "absent usage differs from unknown");
    check($("usage-results").textContent.includes("Weekly · unknown"), "missing usage is unknown");
    check($("usage-results").querySelectorAll(".window").length === 5, "per-model weekly window rendered");
    check($("usage-results").querySelector(".stop").style.left === "73%", "configured stop line");
    check($("usage-results").querySelector(".stale-meter"), "stale bars have a distinct state");
    S.usage[0].read_at = now; renderUsage();
    check($("usage-results").querySelector(".critical"), "fresh usage beyond the stop line is critical");
    S.usage[0].windows.five_hour.used_percent = 65; renderUsage();
    check($("usage-results").querySelector(".warning") && $("usage-results").textContent.includes("Near stop line"), "near stop bars include a text state");
    check($("usage-results").querySelector(".normal"), "normal bars have a distinct state");
    S.usage[0].windows.five_hour.used_percent = 63; renderUsage();
    check($("usage-results").querySelector(".window .meter").classList.contains("warning"), "exactly ten points before stop is near");
    S.usage[0].windows.five_hour.used_percent = 62.9; renderUsage();
    check($("usage-results").querySelector(".window .meter").classList.contains("normal"), "more than ten points before stop is normal");
    S.usage[0].windows.five_hour.used_percent = 73; renderUsage();
    check($("usage-results").querySelector(".window .meter").classList.contains("critical"), "exact stop line is critical");
    check($("usage-results").querySelector(".usage-grid .usage-model"), "model weekly windows align inside subscription grid");
    check($("usage-results").textContent.includes("resets in 1 h 0 m"), "reset countdown accompanies absolute time");
    S.usage_other_subscriptions = 0; renderUsage();
    check($("usage-hidden").textContent === "", "zero hidden subscriptions produce no noise");
    S.usage[0].windows.five_hour.used_percent = 80; renderUsage();
    check($("usage-results").textContent.includes("Skipped for new jobs") && $("usage-routing").textContent.includes("new jobs go to Fresh"), "stop line explains routing to alternative");
    S.due = [{node:poison, kind:"handback", ready:true, text:"ready to merge"}, {node:"test", kind:"handback", text:poison, reasons:[poison]},
      {node:"test", kind:"stale-status", text:"status is stale"}, {node:"test", kind:"lesson-outcome", text:"missing lesson outcome"}];
    renderDue();
    check($("due").querySelectorAll("details").length === 2, "due grouped per node");
    check($("due").textContent.includes("Ready to merge") && $("due").textContent.includes("missing lesson outcome"), "due includes readiness and lesson actions");
    S.scan = [{name:poison, path:"/demo/a 'quoted'; $(touch nope)", kind:"repo", commit_count:2, last_commit_date:"2026-10-19T06:00:00Z", languages:[poison], agent_setup:[poison], readme:poison, adoptable:true},
      {name:"Existing", path:"/demo/existing", kind:"repo", commit_count:1, node:"test", already_home:true, adoptable:false},
      {name:"Other", path:"/demo/other", kind:"repo", commit_count:1, ownership:"another brain: workshop", adoptable:false}];
    scanSelected.clear(); renderScan();
    const boxes = $("scan-results").querySelectorAll('input[type="checkbox"]');
    check(boxes[1].disabled && boxes[2].disabled, "already owned repositories cannot be selected");
    boxes[0].click();
    check($("adopt-command").querySelector("input").value === "brain adopt --parent 'root' -- " + shellQuote(S.scan[0].path), "adopt command preserves and shell-quotes the exact path");
    const parent = S.nodes.find(n => n.id !== "root" && !n.archived).id;
    $("adopt-parent").value = parent; $("adopt-parent").dispatchEvent(new Event("change"));
    check($("adopt-command").querySelector("input").value.includes("--parent " + shellQuote(parent)), "adopt parent selector updates command");
    check(getComputedStyle($("adopt-command")).position === "sticky", "selected import command remains sticky");
    check(shellQuote("a'b") === "'a'\\''b'", "shell quoting protects apostrophes");
    check(!document.querySelector("img[data-ui-attack]") && !window.uiAttack, "all new data surfaces escape markup");
    for (const view of ["learnings", "agents", "usage", "imports", "overview"]) {
      document.querySelector(`[data-view="${view}"]`).click();
      check(!document.querySelector(`[data-page="${view}"]`).hidden, `view ${view} accessible via native button`);
    }
    document.querySelector('[data-view="imports"]').click(); render();
    const checkbox = $("scan-results").querySelector("input");
    checkbox.focus(); render();
    check(document.activeElement === checkbox, "unchanged scan snapshots preserve keyboard focus");
    S.scan[0].commit_count++; render();
    check(document.activeElement.dataset.focus === checkbox.dataset.focus, "changed import snapshot restores checkbox focus");
    S.scan[0].path = "/demo/[…]-hidden"; renderScan();
    check($("scan-results").querySelector("input").disabled, "redacted paths never become adopt commands");
    S.usage[0].windows.five_hour.resets_at = now - 1; renderUsage();
    check($("usage-results").textContent.includes("expired · unknown"), "expired windows age out without a server refresh");
    // Changed snapshots preserve user intent, not just byte-identical data.
    S = structuredClone(saved.state); notifyOn = false; renderedData.clear();
    Object.assign(learningFilter, {node:"", specialist:"", state:""}); learningLimit = 80;
    scanSelected.clear(); scanParent = "root"; render();
    const proposal = S.items.all.find(i => i.status === "proposed");
    proposal.provenance.commit = "abcdef1" + "0".repeat(33);
    proposal.provenance.time = "2020-01-01T00:00:00Z";
    proposal.body = "First paragraph.\nContinued line.\n\n- One\n- Two\n\n1. First\n2. Second\n\n```js\n" + poison + "\n```\n\n" + poison;
    render();
    check($("learning-results").querySelector("article h3").textContent === proposal.title, "old proposed items still sort before accepted items");
    check($("learning-results").textContent.includes("abcdef1") && !$("learning-results").textContent.includes("abcdef10000"), "provenance shortens only commit hashes");
    check($("filter-state").querySelector('option[value="proposed"]').textContent === "proposed (1)", "state filters count awaiting proposals");
    const body = $("learning-results").querySelector("details"); body.open = true;
    check(body.querySelectorAll("p").length === 2 && body.querySelector("ul") && body.querySelector("ol") && body.querySelector("pre code").textContent === poison, "item markdown renders paragraphs lists and literal code safely");
    const due = $("due").querySelector("details"); due.open = false;
    const historyGroup = $("ledger").querySelector("details"); historyGroup.open = false;
    const candidate = S.scan.find(c => c.adoptable);
    const candidateBox = [...$("scan-results").querySelectorAll("input")].find(e => e.dataset.focus === "scan:" + candidate.path);
    candidateBox.click();
    proposal.body += "\n\n" + Array.from({length:20}, () => "Additional context for reviewing the source and its assumptions.").join("\n\n");
    learningFilter.state = "proposed"; renderLearnings();
    document.querySelector('[data-view="learnings"]').click();
    window.scrollTo(0, 200); const priorY = scrollY;
    proposal.title += " updated"; S.due[0].text += " updated"; S.nodes.find(n => n.jobs.rows.length).jobs.rows[0].Reviews = "8"; candidate.commit_count++;
    render();
    check($("learning-results").querySelector("details").open, "open learning body survives changed snapshot");
    check(!$("due").querySelector("details").open, "collapsed due survives changed snapshot");
    check(!$("ledger").querySelector("details").open, "collapsed ledger survives changed snapshot");
    check(learningFilter.state === "proposed" && $("filter-state").value === "proposed", "filters survive changed snapshot");
    check([...$("scan-results").querySelectorAll("input")].some(e => e.checked && e.dataset.focus === "scan:" + candidate.path), "selected imports survive changed snapshot");
    check(priorY > 0 && scrollY === priorY, "scroll position survives changed snapshot");
    check($("ledger").querySelector(".late-flag") && $("ledger").querySelector(".spark"), "ledger flags long open jobs and charts durations");
    check(!$("agent-results").textContent.includes("1 lessons") && $("ledger").querySelector('[title="Subscription not recorded"]').textContent === "—", "singular lessons and quiet missing subscriptions");
    check(document.querySelector("#needs").closest('[data-page="overview"]') && document.querySelector("#due").closest('[data-page="overview"]'), "overview contains Inbox and Due");
    S.inbox = []; S.due = []; renderNeeds(); renderDue();
    check($("needs").textContent.includes("Nothing needs you") && $("due").textContent.includes("Coordinators are idle: nothing due"), "overview empty states explain what belongs here");
    const tabs = [...document.querySelectorAll('[role="tab"]')]; tabs[0].click(); tabs[0].focus();
    tabs[0].dispatchEvent(new KeyboardEvent("keydown", {key:"ArrowRight", bubbles:true}));
    check(document.activeElement === tabs[1] && location.hash === "#view=learnings" && tabs[1].getAttribute("aria-selected") === "true", "arrow keys activate and link tabs");
    tabs[1].dispatchEvent(new KeyboardEvent("keydown", {key:"End", bubbles:true}));
    check(document.activeElement === tabs[4] && currentView === "imports", "End reaches last tab");
    history.replaceState(null,"", "#view=usage"); showView();
    check(currentView === "usage" && !document.querySelector('[data-page="usage"]').hidden, "hash restores a linked tab");
    const long = "LongUnbrokenNodeAndItemName".repeat(12);
    S.items.all = Array.from({length:500}, (_,i) => ({id:"bulk-"+i,title:long,status:"active",provenance:{node:long,ref:long,time:new Date(Date.now()-i*3600000).toISOString()}}));
    Object.assign(learningFilter,{node:"",specialist:"",state:""}); learningLimit=80;
    document.querySelector('[data-view="learnings"]').click(); render();
    check($("learning-results").querySelectorAll("article").length === 80 && document.documentElement.scrollWidth <= innerWidth, "500 long items stay bounded without document overflow");
    for (const n of S.nodes) n.title = long;
    for (const sp of S.specialists) sp.title = long;
    for (const row of S.usage) row.label = long;
    for (const candidate of S.scan) candidate.name = long;
    for (const view of ["overview","agents","usage","imports"]) {
      document.querySelector(`[data-view="${view}"]`).click(); render();
      check(document.documentElement.scrollWidth <= innerWidth, `long node names stay contained in ${view}`);
    }
    check(displayText("1 commit(s), 2 lesson(s)") === "1 commit, 2 lessons", "CLI plural placeholders read naturally");
    check(!document.querySelector('script[src], link[href], img[src], iframe[src]'), "page has no automatic external asset requests");
    const events = [];
    window.Notification = class { constructor(title, options) { events.push({ title, options }); } };
    notifyOn = true; firstPaint = true;
    S.brain.name = "ui-notification-test-" + crypto.randomUUID();
    storageKeys.push(notificationKey());
    const question = {id:"stable-first", node:"test", kind:"inbox", text:poison, first_seen:"2026-10-19T06:00:00Z"};
    S.inbox = [question]; renderNeeds(); notifyNew();
    check($("needs").textContent.includes("stable-f") && $("needs").textContent.includes("first seen"), "inbox shows short id and first-seen time");
    check(events.length === 1, "unseen inbox id notifies on load when opted in");
    firstPaint = false; notifyNew();
    check(events.length === 1, "same id never repeats on refresh");
    firstPaint = true; notifyNew();
    check(events.length === 1, "same id never repeats across a reload");
    S.inbox = []; notifyNew(); S.inbox = [{...question, text:"edited text"}]; notifyNew();
    check(events.length === 1, "resolved or edited id never repeats");
    S.inbox.push({...question, id:"stable-second"}); notifyNew();
    check(events.length === 2, "new stable id notifies once");
    notifyOn = false; S.inbox.push({...question, id:"stable-third"}); notifyNew();
    check(events.length === 2, "notifications remain opt-in");
    notifyOn = true; notifyNew();
    check(events.length === 3, "opting in can report an unseen id");
    S.brain.name += "-other"; storageKeys.push(notificationKey()); notifyNew();
    check(events.length === 6, "notification history is isolated by brain");
    return { passed: checks.length, checks };
  } finally {
    S = saved.state;
    selected = saved.selected;
    firstPaint = saved.firstPaint;
    Object.assign(learningFilter, originalFilter); learningLimit = originalLimit; currentView = originalView;
    for (const key of storageKeys) localStorage.removeItem(key);
    scanParent = originalParent; scanSelected.clear(); for (const path of originalScan) scanSelected.add(path);
    $("learning-filters").replaceChildren();
    window.Notification = saved.notification;
    notifyOn = false;
    history.replaceState(null, "", saved.url);
    render();
    if (!selected) closeDetail();
    notifyOn = saved.notifyOn;
    notifyLabel();
  }
})()
