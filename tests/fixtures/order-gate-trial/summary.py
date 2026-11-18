import json,sys,collections
d=sys.argv[1]; pre=sys.argv[2] if len(sys.argv)>2 else 'final-'
B=json.load(open(d+'/'+pre+'before.json')); A=json.load(open(d+'/'+pre+'after.json'))
err=lambda r:(r['note'] or '').startswith('ERROR')
good=lambda r:r['ok'] and not err(r)
def agg(rows):
    c=collections.OrderedDict()
    for r in rows: c.setdefault(r['id'],[]).append(r)
    return c
b,a=agg(B['rows']),agg(A['rows'])
print('case want before after (errors count as not passed)')
for k in b:
    print(k, b[k][0]['want'], sum(map(good,b[k])),'/',len(b[k]), sum(map(good,a[k])),'/',len(a[k]), 'errors b/a', sum(map(err,b[k])), sum(map(err,a[k])))
for arm,c in (('BEFORE',b),('AFTER',a)):
    for k in c:
        for r in c[k]:
            if not good(r): print(arm,'MISS',k,(r['note'] or '')[:60],(r['reason'] or '')[:260])
groups={'record (21 cases from the 20 incidents)':[k for k in b if k[0].isdigit()],'today (5)':[k for k in b if k[0]=='T'],'controls must block (3)':[k for k in b if k[0]=='C']}
for name,ks in groups.items():
    print(name,'before',sum(good(r) for k in ks for r in b[k]),'after',sum(good(r) for k in ks for r in a[k]),'of',sum(len(b[k]) for k in ks))
print('errors before',sum(map(err,B['rows'])),'after',sum(map(err,A['rows'])))
print('calls before',B['calls'],'after',A['calls'],'usd before %.5f after %.5f total %.5f'%(B['cost_usd'],A['cost_usd'],B['cost_usd']+A['cost_usd']),'unknown-cost calls',B['unknown_cost_calls']+A['unknown_cost_calls'])
