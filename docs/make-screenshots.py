#!/usr/bin/env python3
"""Builds a throwaway demo brain with made-up projects, then takes the README's screenshots of `brain ui`
and the terminal dashboard. Nothing here is real data. Needs Google Chrome (headless) on macOS or Linux.

    python3 docs/make-screenshots.py            # writes docs/img/*.png and docs/img/dashboard.txt
"""
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent
OUT = CORE / "docs" / "img"
CHROME = next((p for p in ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                           shutil.which("google-chrome") or "", shutil.which("chromium") or ""] if p and Path(p).exists()),
              None)
random.seed(7)
NOW = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
ENV = {**os.environ, "BRAIN_NO_REEXEC": "1", "GIT_AUTHOR_NAME": "sam", "GIT_AUTHOR_EMAIL": "sam@example.com",
       "GIT_COMMITTER_NAME": "sam", "GIT_COMMITTER_EMAIL": "sam@example.com", "GIT_CONFIG_GLOBAL": "/dev/null"}


def run(*args, cwd, env=None, check=True):
    r = subprocess.run([str(a) for a in args], cwd=cwd, env={**ENV, **(env or {})}, capture_output=True, text=True)
    if check and r.returncode:
        sys.exit(f"{' '.join(map(str, args))}: {r.stderr or r.stdout}")
    return r.stdout


def at(hours_ago):
    return (NOW - dt.timedelta(hours=hours_ago)).isoformat()


def commit(repo, msg, hours_ago, path=None, text=None):
    if path:
        p = Path(repo) / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((p.read_text() if p.exists() else "") + (text or msg) + "\n")
    run("git", "add", "-A", cwd=repo)
    when = at(hours_ago)
    run("git", "commit", "-qm", msg, "--allow-empty", cwd=repo, env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})


def home_repo(path, files):
    path.mkdir(parents=True)
    run("git", "init", "-q", "-b", "main", cwd=path)
    for k, v in files.items():
        (path / k).parent.mkdir(parents=True, exist_ok=True)
        (path / k).write_text(v)
    commit(path, "init", 60)
    return path


def section(f, heading, lines):
    text = f.read_text()
    marker = f"## {heading}\n"
    body = "\n".join(lines) + "\n"
    f.write_text(text.replace(marker, marker + "\n" + body, 1) if marker in text else text + f"\n{marker}\n{body}")


def build(base):
    os.environ["BRAIN_CONFIG"] = str(base / "brains.json")
    ENV["BRAIN_CONFIG"] = os.environ["BRAIN_CONFIG"]
    root = base / "Brain"
    run(sys.executable, CORE / "bin" / "brain", "init", root, "--operator", "sam", "--title", "Sam's Brain",
        "--tz", "Europe/Lisbon", "--name", "sam", cwd=base)
    b = lambda *a: run(sys.executable, root / "core" / "bin" / "brain", *a, cwd=root)  # noqa: E731
    code = base / "code"
    web = home_repo(code / "website", {"README.md": "# Studio website\n", "package.json": "{}\n"})
    app = home_repo(code / "mobile-app", {"README.md": "# Field app\n"})
    nw = home_repo(code / "northwind-portal", {"README.md": "# Portal\n"})
    b("principal", "add", "northwind")
    b("new", "website", "--title", "Studio website", "--home", web, "--labels", "kind:code,area:studio", "--runtime", "claude")
    b("new", "mobile-app", "--title", "Field app", "--home", app, "--labels", "kind:code,area:studio", "--runtime", "codex")
    b("new", "northwind", "--title", "Northwind portal", "--owners", "sam,northwind", "--home", nw,
      "--labels", "kind:code,area:clients")
    b("new", "research", "--title", "Research", "--labels", "kind:research")
    b("new", "research/pricing", "--title", "Pricing study", "--labels", "kind:research")
    b("new", "newsletter", "--title", "Newsletter", "--labels", "kind:writing")
    b("set", "website", "ports=3")
    for nid, tid in [("root", "demo-root"), ("website", "demo-web"), ("mobile-app", "demo-app"),
                     ("northwind", "demo-nw"), ("research", "demo-research"), ("research/pricing", "demo-pricing"),
                     ("newsletter", "demo-news")]:
        b("set", nid, f"coordinator={tid}")

    nodes = {n: next(p.parent for p in root.rglob("node.md") if f"id: {n}\n" in p.read_text())
             for n in ["website", "mobile-app", "northwind", "research", "research/pricing", "newsletter"]}
    stamp = lambda h: (NOW - dt.timedelta(hours=h)).astimezone(dt.timezone(dt.timedelta(hours=1))).strftime(  # noqa
        "%Y-%m-%d %H:%M +01:00")

    def status(n, updated_h, now, nxt, work, blocked, changes):
        f = nodes[n] / "status.md" if n != "root" else root / "status.md"
        lines = f.read_text().splitlines()
        lines[0] = f"Updated: {stamp(updated_h)} by {n} coordinator (claude)"
        f.write_text("\n".join(lines) + "\n")
        section(f, "Now", [f"- {x}" for x in now])
        section(f, "Next", [f"- {x}" for x in nxt])
        if work:
            t = f.read_text().replace("| --- | --- | --- | --- |\n", "| --- | --- | --- | --- |\n" + "".join(
                f"| {br} | [{th}](t3-thread://v1/demo-{th}) ({rt}) | {pr} | {st} |\n" for br, th, rt, pr, st in work), 1)
            f.write_text(t)
        section(f, "Blocked / needs the operator", [f"- {x}" for x in blocked])
        section(f, "Recent changes (newest first)", [f"- {stamp(h)}: {x}" for h, x in changes])

    status("website", 0.2, ["Hero redesign in review; blog search building; translations started."],
           ["Ship the redesign behind a flag, then measure sign-ups for a week."],
           [("job/hero-redesign", "hero-redesign", "Claude", "#41", "in review (round 2)"),
            ("job/blog-search", "blog-search", "Codex", "-", "building; index done, ranking next"),
            ("job/i18n", "i18n", "Claude", "-", "building; 3 of 7 locales")],
           ["Approve the new pricing copy on the landing page."],
           [(0.3, "hero redesign: review round 2 sent"), (2, "blog search: index built"), (5, "i18n job started")])
    status("mobile-app", 1.5, ["Offline sync done; push notifications next."], ["Beta to 20 field testers."],
           [("job/push", "push", "Codex", "-", "building; Android done, iOS next")],
           [], [(1.5, "offline sync merged"), (9, "push job started")])
    status("northwind", 3, ["Portal v2: invoices page in progress."], ["Demo to Northwind on Friday."],
           [("job/invoices", "invoices", "Claude", "-", "building")],
           ["Northwind's API key for the staging billing service."], [(3, "invoices job started")])
    status("research", 20, ["Pricing study under way in research/pricing."], [], [], [], [(20, "research node set up")])
    status("research/pricing", 6, ["12 competitors compared; 3 tiers proposed."], ["Interview 5 customers."], [],
           ["Pick the interview list (shortlist in sources/)."], [(6, "tier proposal drafted")])
    status("newsletter", 30, ["Issue 14 drafted."], ["Send Tuesday 09:00."], [], [], [(30, "issue 14 drafted")])
    status("root", 0.5, ["6 projects; 5 jobs running across 3 homes."], ["Daily brief at 09:00."], [], [],
           [(0.5, "daily brief")])
    with open(root / "nodes" / "website" / "inbox.md", "a") as fh:
        fh.write(f"- {stamp(0.4)} from worker: question: keep the old hero as a fallback for Safari 15? (job/hero-redesign)\n")

    dec = nodes["website"] / "decisions.md"
    dec.write_text(dec.read_text() + (
        f"\n## {stamp(26)}: Ship the redesign behind a feature flag\nSaid: \"flag it, I want to compare sign-ups\"\n"
        "Why: compare sign-ups before and after.\nAlternatives considered: a hard switch.\n"
        f"\n## {stamp(50)}: Search runs on a static index, no server\nWhy: the site is static; no extra hosting.\n"
        "Alternatives considered: a hosted search service.\n"))
    dec2 = nodes["mobile-app"] / "decisions.md"
    dec2.write_text(dec2.read_text() + (
        f"\n## {stamp(14)}: Offline first: the app works with no signal\nSaid: \"the field teams are in basements\"\n"
        "Why: most use is underground.\nAlternatives considered: online-only with retries.\n"))
    brief = nodes["website"] / "brief.md"
    brief.write_text(brief.read_text().replace("## Sources of truth", (
        "- At most 3 worktrees at once. Why: each runs a dev server and a browser. Lift when: the CI runners take "
        "the browser tests.\n- No deploys without Sam. Why: the site takes payments. Lift when: never.\n\n"
        "## Sources of truth")))
    (nodes["northwind"] / "never-name.txt").write_text("# other clients\nAcme\n")

    for nid, h in [("website", web), ("mobile-app", app)]:
        for i in range(random.randint(8, 14)):
            commit(h, random.choice(["fix: spacing on small screens", "feat: search results page", "test: hero variants",
                                     "chore: bump deps", "feat: sync queue", "docs: README"]),
                   random.uniform(1, 47), "log.md", f"change {i}")
    for wt in ["job/hero-redesign", "job/blog-search", "job/i18n"]:
        b("wt", "add", "website", wt)
    b("wt", "add", "mobile-app", "job/push")
    b("wt", "add", "northwind", "job/invoices")
    for nid, wt, n in [("website", "job/hero-redesign", 9), ("website", "job/blog-search", 6), ("website", "job/i18n", 3),
                       ("mobile-app", "job/push", 5), ("northwind", "job/invoices", 4)]:
        w = json.loads((root / ".brain" / "local.json").read_text())["nodes"][nid]["worktrees"][wt]["path"]
        for i in range(n):
            commit(w, f"{wt.split('/')[1]}: step {i + 1}", random.uniform(0.2, 20), "work.md", f"{wt} {i}")
        (Path(w) / "wip.md").write_text("in progress\n")
    b("review", "website", "--branch", "job/hero-redesign", "--built-by", "claude")
    b("review", "website", "--branch", "job/hero-redesign", "--built-by", "claude")
    led = nodes["website"] / "job-ledger.md"
    rows = led.read_text().rstrip("\n").split("\n")
    past = [("job/nav", 70, 3.2, 11, 2), ("job/footer", 66, 1.1, 4, 1), ("job/contact-form", 58, 5.6, 17, 3),
            ("job/a11y-pass", 40, 2.4, 9, 1), ("job/og-images", 30, 1.8, 6, 1), ("job/blog-layout", 22, 4.1, 13, 2)]
    hdr = rows[:rows.index(next(r for r in rows if r.startswith("| ---"))) + 1]
    body = [r for r in rows[len(hdr):]]
    old = [f"| {j} | {(NOW - dt.timedelta(hours=s)).astimezone(dt.timezone(dt.timedelta(hours=1))).isoformat(timespec='minutes')}"
           f" | {(NOW - dt.timedelta(hours=s - h)).astimezone(dt.timezone(dt.timedelta(hours=1))).isoformat(timespec='minutes')}"
           f" | {h} | merged | {c} | {r} | claude |  |" for j, s, h, c, r in past]
    old.append(f"| job/dark-mode | {(NOW - dt.timedelta(hours=34)).isoformat(timespec='minutes')} | "
               f"{(NOW - dt.timedelta(hours=31)).isoformat(timespec='minutes')} | 3.0 | abandoned | 5 | 1 | codex | "
               "superseded by the redesign |")
    led.write_text("\n".join(hdr + old + body) + "\n")
    lisbon = dt.timezone(dt.timedelta(hours=1))
    for nid, job, hours in [("website", "job/hero-redesign", 7.5), ("website", "job/blog-search", 3.1),
                            ("website", "job/i18n", 1.2), ("mobile-app", "job/push", 9.4), ("northwind", "job/invoices", 3.3)]:
        f = nodes[nid] / "job-ledger.md"  # these jobs "started" hours ago, not when this script ran
        lines = f.read_text().split("\n")
        for i, l in enumerate(lines):
            if l.startswith(f"| {job} |"):
                cells = l.split("|")
                cells[2] = " " + (NOW - dt.timedelta(hours=hours)).astimezone(lisbon).isoformat(timespec="minutes") + " "
                lines[i] = "|".join(cells)
        f.write_text("\n".join(lines))
    for i, (iid, title, src) in enumerate([("flag-before-ship", "Ship UI changes behind a flag, measure, then switch", "website"),
                                           ("bun-trusted-deps", "List trustedDependencies explicitly; empty means unset", "mobile-app"),
                                           ("review-two-runtimes", "A second runtime reviews every change before you see it", "website")]):
        b("item", "add", iid, "--from", src, "--title", title, "--kind", "lesson", "--ref", "job report", "--body", title)
    b("schedule", "add", "daily-brief", "--when", "daily 09:00", "--node", "root", "--brief", "Run the node-brief skill.")
    b("schedule", "bind", "daily-brief", "demo-schedule")
    for h, m in [(0.5, "root: daily brief"), (2, "website: blog search index built"), (6, "research/pricing: tiers"),
                 (14, "mobile-app: decision: offline first"), (26, "website: decision: ship behind a flag"),
                 (30, "newsletter: issue 14 drafted"), (40, "northwind: portal v2 kickoff")]:
        commit(root, m, h)
    run("git", "add", "-A", cwd=root)
    run("git", "commit", "-qm", "demo: notes", cwd=root, check=False)
    return root, b


def snapshot(root, b, theme, out, hash_="", size=(1440, 1000)):
    st = json.loads(b("ui", "--json"))
    st["brain"].update(root="~/Brain", machine="studio-mac", others=[])
    st["machine"].update(cpus=12, load=[4.2, 3.9, 3.1], mem_free_gb=21.4, mem_total_gb=36, disk_free_gb=312,
                         disk_total_gb=994)
    for n in st["nodes"]:
        for w in n["worktrees"]:
            w["path"] = w["path"].replace(str(root.parent), "~")
            w["listening"] = list(range(w["port"], w["port"] + (2 if w["branch"] != "job/i18n" else 0)))
            w["wrote"] = int(NOW.timestamp()) - (40 if w["branch"] in ("job/blog-search", "job/push") else 1500)
        n["home"] = n["home"].replace(str(root.parent), "~")
    html = (CORE / "ui" / "index.html").read_text().replace(".2s ease", "0s")
    html = html.replace('<script>\n"use strict";', "<script>window.__STATE__=" + json.dumps(st).replace("</", "<\\/")
                        + ';</script>\n<script>\n"use strict";', 1)
    if theme == "light":
        html = html.replace("<html lang=\"en\">", "<html lang=\"en\" data-theme=\"light\">", 1)
    else:
        html = html.replace("<html lang=\"en\">", "<html lang=\"en\" data-theme=\"dark\">", 1)
    page = root.parent / f"snap-{theme}.html"
    page.write_text(html)
    prof = tempfile.mkdtemp()
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", f"--user-data-dir={prof}", "--hide-scrollbars",
                    f"--window-size={size[0]},{size[1]}", "--virtual-time-budget=3000", "--force-device-scale-factor=1",
                    f"--screenshot={out}", f"file://{page}{hash_}"], capture_output=True, timeout=120)
    shutil.rmtree(prof, ignore_errors=True)
    print("wrote", out.relative_to(CORE))


def main():
    if not CHROME:
        sys.exit("needs Google Chrome or Chromium for the screenshots")
    OUT.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="brain-demo-")).resolve()
    try:
        root, b = build(base)
        snapshot(root, b, "dark", OUT / "ui-dark.png", size=(1440, 1270))
        snapshot(root, b, "light", OUT / "ui-light.png", size=(1440, 1270))
        snapshot(root, b, "dark", OUT / "ui-project.png", "#node=website", size=(1440, 1000))
        dash = b().replace(str(root.parent), "~").replace("machine Mac", "machine studio-mac")
        (OUT / "dashboard.txt").write_text(dash)
        print("wrote", (OUT / "dashboard.txt").relative_to(CORE))
        (OUT / "jobs.txt").write_text(b("jobs"))
        (OUT / "brief.txt").write_text(b("brief", "website", "--branch", "job/blog-search", "--task",
                                         "Rank results by title match first, then recency. Done when the search page "
                                         "shows the 10 best results in under 100 ms.").replace(str(root.parent), "~"))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
