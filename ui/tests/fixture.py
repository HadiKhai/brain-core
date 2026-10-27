"""Synthetic dashboard state. No files, names, or readings from a real brain."""
import copy
import json
import time
from pathlib import Path
NOW = int(time.time())
def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
def fixture():
    def job(name, hours=None, outcome="", age=2, ended=0):
        end = NOW - int(ended * 3600)
        return {"Job": name, "Started": iso(end - int((hours or age) * 3600)), "Ended": iso(end) if hours else "",
                "Hours": str(hours or ""), "Outcome": outcome, "Reviews": "2", "Runtime": "Codex", "Subscription": "Studio plan",
                "Lessons": "Check keyboard navigation" if hours else "", "age_h": age,
                "Usage": "measured" if hours else "", "Fresh in": 42000, "Cache read": 100000, "Cache write": 8000, "Output": 12000,
                "Active h": 1.2, "Compactions": 1, "Sub-agents": 0, "Models": "Design model", "Budget": "300k tokens",
                "usage": None if hours else {"processed": 162000, "fresh": 42000, "output": 12000, "active_h": 1.2,
                "compactions": 1, "subagents": 0, "sub_share": 0, "models": {"Design model": 162000}}, "budget": {"tokens":300000,"hours":4}}
    def node(nid, title, parent="root"):
        live = job("job/navigation")
        finished = [job("job/complete-"+str(i), 1+i/10, "merged", ended=3+i*14) for i in range(13)]
        return {"id":nid,"title":title,"parent":parent,"owners":["Demo reviewer"],"labels":["Product"],"runtime":"Codex",
            "sealed":False,"archived":False,"paused":False,"home":"~/demo/"+nid,"coordinator":"https://example.com/conversation",
            "updated":"2026-10-20T10:00:00Z","updated_ago":"12 min ago","status":{"Now":"A calmer navigation system is in review.\n\n- Preserve keyboard focus.\n- Keep long project names readable.","Next":"Review the narrow-screen tour before the next job."},
            "inbox":["Should the compact navigation stay open on tablets?"],"blocked":[],"decisions":[{"title":"Keep project notes close to the overview","said":"The next decision should have its context.","t":NOW-900}],"limits":["Use synthetic data in public examples."],"never_name":2,
            "work":[{"branch":"job/navigation","thread":"made-up-conversation","link":"https://example.com/conversation","runtime":"Codex","state":"Reviewing the narrow-screen navigation and keyboard focus order"},
                    {"branch":"job/complete-1","state":"merged","thread":""}],
            "worktrees":[{"branch":"job/navigation","port":4200,"ports":4,"listening":[4200],"dirty":3,"ahead":2,"last":NOW-600,"wrote":NOW-120,"ready":{"ready":False,"reasons":["Review still running"]}},
                         {"branch":"job/complete-1","port":4204,"ports":4,"listening":[],"dirty":0,"ahead":0}],
            "jobs":{"rows":[*finished,live],"open":[live],"merged":13,"abandoned":0,"median_hours":1.6,"median_reviews":2,"median_tokens":160000,"median_active_h":1.2,"measured":13},
            "reports":[{"name":"navigation-review","title":"Navigation review","path":"~/demo/reports/navigation/index.md"}],"spend":None}
    root=node("root","Meadow workspace",None);root["work"]=[];root["worktrees"]=[];root["jobs"]={"rows":[],"open":[],"merged":0,"abandoned":0}
    app=node("field-guide","Field Guide"); site=node("observatory","Observatory website");site["work"]=[];site["worktrees"]=[];site["jobs"]={"rows":[],"open":[],"merged":0,"abandoned":0};site["paused"]=True
    items=[{"id":"lesson-"+str(i),"title":t,"kind":"Lesson","status":"proposed" if i<2 else "active","scope":["Product"],"body":"## What changed\n\nKeep the action near the context it affects.\n\n- Check narrow layouts.\n- Test keyboard focus.\n\n```text\n<script>literal, untrusted content</script>\n```",
        "provenance":{"node":"field-guide","ref":"notes/review.md","repo":"demo/field-guide","commit":"abcdef1234567890abcdef1234567890abcdef12","time":"2026-10-20T09:00:00Z","owners":["Demo reviewer"]}}
        for i,t in enumerate(["Keep filters next to the work","Explain every status","Preserve drafts between views","Review mobile navigation with taps","Measure the shared gutters","Make empty states useful"])]
    days={f"Oct {d}":{"tokens":(d*12537)%200000+30000,"fresh_out":18000,"active_h":1.4} for d in range(7,21)}
    weeks={f"Week {d}":{"tokens":d*110000,"active_h":8,"fresh_out":200000} for d in range(1,9)}
    return {"brain":{"name":"meadow-demo","title":"Meadow workspace","root":"~/demo/meadow","machine":"Studio laptop","tz":"UTC","now":iso(NOW),"fallback":["codex","claudeAgent"],"others":["Sandbox"]},
        "nodes":[root,app,site],"attention":[{"node":"field-guide","kind":"inbox","text":"Navigation review needs a decision"}],
        "inbox":[{"id":"synthetic-question-one","node":"field-guide","kind":"inbox","from":"worker","recipient":"operator","options":["Keep open", "Collapse"],"recommendation":"Keep open","text":"2026-10-20 10:00 +00:00 from worker: question: Keep the compact navigation open on tablets?","first_seen":iso(NOW-3600)},
                 {"id":"synthetic-blocked-one","node":"observatory","kind":"blocked","from":"","recipient":"operator","options":["Read now", "Defer"],"recommendation":"Read now","text":"question: Should the launch note get a final read from Demo reviewer?","first_seen":iso(NOW-30*3600)}],
        "due":[{"node":"field-guide","kind":"handback","branch":"job/navigation","job":"a1b2c3d4e5f6a7b8c9d0e1f2","text":"job/navigation: ready to merge; review the report and record the next step","ready":True},{"node":"observatory","kind":"stale-status","text":"facts differ from current files; update judgement sections, then `brain status observatory --refresh`"}],
        "commits":[{"sha":str(i),"subject":t,"node":"field-guide","where":"home","t":NOW-i*1400} for i,t in enumerate(["Refine the narrow navigation","Preserve the active project context","Add keyboard navigation checks","Improve the review hand-back"])],
        "commits_24h":18,"commits_total":42,"hours":72,"activity":{"start":NOW-72*3600,"counts":{"field-guide":[i%7 if i%4==0 else 0 for i in range(72)],"observatory":[i%5 if i%3==0 else 0 for i in range(72)]}},
        "items":{"count":len(items),"all":items,"recent":items[:3]},"specialists":[{"id":"product-reviewer","node":"root","title":"Product reviewer","runtime":"Claude","scope":["Product"],"status":"active","imported":False,
            "role_summary":"Review product structure and guide the next clear decision.","role_body":"## Purpose\n\nReview product structure and guide the next clear decision.\n\n## When to hire\n\nBefore a navigation change or a release.","hire_command":"brain hire product-reviewer field-guide --task 'Review navigation'",
            "lessons":[{"text":"Treat touch activation as a separate check","from":"field-guide","time":"2026-10-20T09:00:00Z"}],"jobs":[{"node":"field-guide","job":"job/navigation","time":iso(NOW-3600),"started":iso(NOW-7200),"task":"Review navigation and focus order","runtime":"Claude","subscription":"Review plan"}]},
            {"id":"layout-engineer","node":"field-guide","title":"Layout engineer (grids, density, narrow screens)","runtime":"Codex","scope":["Product"],"status":"active","imported":True,"lessons":[],"jobs":[]}],
        "schedules":[{"id":"daily-review","when":"Weekdays · 9:00 AM","node":"field-guide","here":True}],"machine":{"load":[1.8,1.5,1.2],"cpus":8,"thermal":"normal","queued":1,"verifying":1,"verify_limit":2,"mem_free_gb":18,"mem_total_gb":32,"disk_free_gb":240,"disk_total_gb":1000},
        "spend":{"files":42,"read":42,"complete":True,"runtimes":{"Codex":{"days":days,"weeks":weeks},"Claude":{"days":days,"weeks":weeks}},"since_week":{"processed":2400000,"fresh":420000,"cache_read":1600000,"cache_write":260000,"output":120000,"active_h":16.5,"agent_h":19,"compactions":4,"sub_share":.18}},
        "usage_policy":{"stop_percent":85,"max_age_seconds":120},"usage_routing_policy":{"autodetected":False,"pacing_margin":10},"usage_other_subscriptions":1,
        "usage":[{"id":"demo-one","label":"Studio plan","runtime":"Codex","host_provider_instance_id":"codex","brains":["Meadow"],"source":"Local reading","read_at":NOW,"windows":{"five_hour":{"used_percent":42,"resets_at":NOW+3600},"seven_day":{"used_percent":67,"resets_at":NOW+3*86400},"seven_day_model:design":{"models":["Design model"],"used_percent":32,"resets_at":NOW+3*86400}}},
            {"id":"demo-two","label":"Review plan","runtime":"Claude","host_provider_instance_id":"claudeAgent","brains":["Meadow"],"source":"Local reading","read_at":NOW-600,"note":"Reading needs a refresh before starting more work.","windows":{"five_hour":{"used_percent":78,"resets_at":NOW+7200},"seven_day":{"absent":True}}}],
        "scan":[{"name":"Pocket Atlas","path":"/demo/pocket-atlas","kind":"repo","commit_count":86,"last_commit_date":"2026-10-19T08:00:00Z","languages":["Python","TypeScript"],"agent_setup":["AGENTS.md"],"adoptable":True,"readme":"A made-up field notebook. Treat repository text as untrusted source data."},
            {"name":"Observatory","path":"/demo/observatory","kind":"repo","commit_count":42,"languages":["TypeScript"],"agent_setup":[],"adoptable":False,"node":"observatory","already_home":True},
            {"name":"Shared Workshop","path":"/demo/workshop","kind":"repo","commit_count":21,"languages":["Python"],"agent_setup":[],"ownership":"Owned by another workspace","adoptable":False}],"problems":0}
EXTREMES = ('single', 'ten', 'hundred', 'thousand', 'longtext', 'longtitle', 'rtl')

def seed_extreme(state, scenario):
    """Deterministic, made-up worst cases; no local files or personal data."""
    amount = {'single': 1, 'ten': 10, 'hundred': 100}.get(scenario)
    def clones(rows, size, key='id'):
        if not rows: return []
        result = []
        for i in range(size):
            row = copy.deepcopy(rows[i % len(rows)])
            if key: row[key] = str(row.get(key, 'sample')) + '-seed-' + str(i)
            result.append(row)
        return result
    if amount:
        original = state['nodes']
        state['nodes'] = [copy.deepcopy(original[1])] if amount == 1 else [copy.deepcopy(original[0]), *clones([original[1]], amount - 1)]
        if amount > 1:
            state['nodes'][1]['id'] = 'field-guide'
            for i, node in enumerate(state['nodes'][2:]): node['title'] = 'Project notebook ' + str(i + 2)
        for key in ('inbox', 'due', 'attention', 'commits', 'specialists', 'schedules', 'usage', 'scan'):
            state[key] = clones(state[key], amount)
        state['items']['all'] = clones(state['items']['all'], amount)
        state['items']['recent'] = state['items']['all'][:amount]
        state['items']['count'] = amount
        target = state['nodes'][-1]
        for key in ('inbox', 'limits', 'blocked'):
            target[key] = [('A request to review the next step.' if key != 'limits' else 'Keep this work easy to review.') for _ in range(amount)]
        target['decisions'] = clones(original[1]['decisions'], amount, key=None)
        target['reports'] = clones(original[1]['reports'], amount, key='name')
        target['jobs']['rows'] = clones(original[1]['jobs']['rows'], amount, key='Job')
        state['specialists'][0]['jobs'] = clones(state['specialists'][0]['jobs'], amount, key='job')
    if scenario == 'thousand': state['inbox'] = clones(state['inbox'], 1000)
    if scenario in ('longtext', 'longtitle', 'rtl'):
        words = 'Review the proposed navigation with the project team and keep the decision near its context for the next person reading'.split()
        prose = '\n\n'.join(' '.join((words * 10)[0:100]) for _ in range(20))
        assert len(prose.split()) == 2000
        title = ('A thoughtful project notebook with a deliberately extended title for checking narrow layouts and preserving reading context ' * 2)[:120]
        rtl = 'هل نراجع الخطوة التالية مع الفريق؟ نحتاج إلى قرار واضح يحافظ على سياق المشروع ويسهّل متابعة العمل. '
        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('title', 'label') and isinstance(item, str):
                        value[key] = rtl if scenario == 'rtl' else title if scenario == 'longtitle' else item
                    elif key in ('text', 'task', 'body', 'said', 'readme', 'note', 'Cause', 'Lessons') and isinstance(item, str) and item:
                        # Replacing prose must retain the request's question marker.
                        value[key] = ('question: ' if 'question:' in item else '') + rtl * 12 if scenario == 'rtl' else prose if scenario == 'longtext' else item
                    else: walk(item)
            elif isinstance(value, list):
                for item in value: walk(item)
        walk(state)
        if scenario == 'longtitle': state['items']['all'][0]['title'] = 'W' * 120
    return state

if __name__ == "__main__":
    import argparse
    import http.server
    import urllib.parse
    import threading
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=4109);args=parser.parse_args()
    import os
    import sys
    dist=Path(os.environ.get('UI_DIST') or Path(__file__).resolve().parents[1]/'dist')
    # The picker's search is the core's own (lib/brain/ui.py): same ranking, same paging, on this synthetic state.
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'lib'))
    from brain import ui as brain
    overrides={}
    revision=[0]

    def state_for(scenario):
        state=seed_extreme(fixture(), scenario)
        if scenario == 'tasks':
            from tasks_fixture import seed_tasks
            seed_tasks(state)
        for node in state['nodes']:
            node['coordinator'] = f'http://127.0.0.1:{args.port}/conversation'
            for job in node.get('work', []):
                if job.get('link'): job['link'] = node['coordinator']
        if scenario == 'empty':
            state.update(nodes=[],attention=[],inbox=[],due=[],commits=[],specialists=[],schedules=[],scan=[],usage=[],spend={},activity={"counts":{}},items={"count":0,"all":[],"recent":[]})
        elif scenario == 'long':
            long='A remarkably long project name without losing the context of the work '
            state['nodes'][1]['title']=long*3
            state['nodes'][1]['work'][0]['state']='Reviewing the complete narrow-screen navigation, every keyboard focus transition, and the import checklist while preserving the project context. '*3
            state['nodes'][1]['worktrees'][0].update(dirty=None,ahead=None,port=None,wrote=None,last=None)
            state['nodes'][1]['runtime']=''
            state['specialists'][0]['title']=long*2
            state['usage'][0]['label']=long*2
            state['items']['all'][0]['title']='LongUnbrokenKnowledgeTitle'*12
            state['items']['all'][0]['body']='Untrusted <img data-ui-attack src=x onerror="window.uiAttack=true"> and [unsafe](javascript:alert(1)).'
            state['scan'][0]['path']="/demo/a 'quoted'; $(touch nope)"
        elif scenario == 'partial':
            state['spend'].update(complete=False,read=14)
        current=int(time.time())
        for row in state['usage']:
            row['read_at']+=current-NOW
            for window in row.get('windows',{}).values():
                if window.get('resets_at'):window['resets_at']+=current-NOW
        if scenario=='demo':
            for key,value in overrides.items():state[key]=value
        return state

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self,*a,**kw):super().__init__(*a,directory=str(dist),**kw)
        # The browser runner sets FIXTURE_QUIET so access lines do not bury its report; log_error still prints.
        def log_request(self,*a):
            if not os.environ.get('FIXTURE_QUIET'):super().log_request(*a)
        def do_POST(self):
            if self.path!='/fixture/update':self.send_error(404);return
            data=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
            overrides.update(data);revision[0]+=1
            self.send_response(204);self.end_headers()

        def do_GET(self):
            parsed=urllib.parse.urlsplit(self.path)
            if parsed.path == '/conversation':
                body=b'<!doctype html><html lang="en"><title>Synthetic conversation</title><main><h1>Conversation opened</h1><p>This is a made-up handoff destination. Replying belongs to your connected conversation.</p></main></html>'
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(body);return
            if parsed.path in ('/snapshot','/before'):
                state=state_for(urllib.parse.parse_qs(parsed.query).get('scenario',['demo'])[0])
                page=(dist/'index.html').read_text() if parsed.path=='/snapshot' else Path('/tmp/brain-ui-before.html').read_text()
                params=urllib.parse.parse_qs(parsed.query)
                if params.get('inbox')==['second']:state['inbox'].append({**state['inbox'][0],'id':'synthetic-question-second'})
                if params.get('workspace')==['other']:state['brain']['name']='other-demo'
                mock="window.__notifications=[];window.Notification=class{static permission='granted';static async requestPermission(){return 'granted'}constructor(title,options){window.__notifications.push({title,options})}};" if params.get('notify')==['granted'] else ''
                injected='<script>'+mock+'window.__STATE__='+json.dumps(state).replace('<','\\u003c')+'</script>'
                page=page.replace('</head>',injected+'</head>').encode()
                self.send_response(200);self.send_header('Content-Type','text/html');self.send_header('Content-Length',str(len(page)));self.end_headers();self.wfile.write(page);return
            if parsed.path=='/api/search':
                q=urllib.parse.parse_qs(parsed.query);one=lambda k,d:(q.get(k) or [d])[0]
                key=one('k','')
                if key not in ('demo','empty','long','partial','slow','many','tasks',*EXTREMES):self.send_error(403);return
                if key=='slow':time.sleep(1.2)
                searched=state_for('demo' if key in ('slow','many') else key)
                if key=='many':  # 1,200 made-up projects for the picker's paging and ranking
                    modifiers=['Rill','Orchid','Lantern','Spruce','Moon','Kite','Nest','Harbor','Yonder','Zephyr']
                    nouns=['Notebook','Atlas','Workshop','Observatory','Fieldbook','Index','Studio','Archive']
                    searched['nodes']=searched['nodes']+[{'id':f'p{i:04}','title':f'{modifiers[i%10]} {nouns[(i//10)%8]} {i:04}','parent':'root','updated':''} for i in range(1200)]
                body=json.dumps(brain.ui_search(searched,one('kind','all'),one('q',''),int(one('offset','0')),int(one('limit','20')))).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body);return
            if parsed.path in ('/api/state','/api/events'):
                scenario=urllib.parse.parse_qs(parsed.query).get('k',[''])[0]
                if scenario not in ('demo','empty','long','partial','error','loading','slow','many','tasks', *EXTREMES):
                    self.send_error(403);return
                if scenario == 'error':self.send_error(503);return
                if scenario == 'loading':time.sleep(30)
                self.send_response(200);self.send_header('Content-Type','application/json' if parsed.path.endswith('state') else 'text/event-stream');self.end_headers()
                body=json.dumps(state_for('demo' if scenario in ('slow','many') else scenario)).encode()
                self.wfile.write(body if parsed.path.endswith('state') else b'data: '+body+b'\n\n')
                if parsed.path.endswith('events'):
                    seen=revision[0]
                    try:
                        while True:
                            time.sleep(.5)
                            if seen!=revision[0]:
                                self.wfile.write(b'data: '+json.dumps(state_for(scenario)).encode()+b'\n\n');seen=revision[0]
                            else:self.wfile.write(b': ping\n\n')
                            self.wfile.flush()
                    except (BrokenPipeError,ConnectionResetError):pass
                return
            return super().do_GET()
    print(f'Synthetic fixture at http://127.0.0.1:{args.port}/?k=demo',flush=True)
    http.server.ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
