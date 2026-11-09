"""Оновлення даних вкладки «Планування» дашборду IT-команд.
Використання: python3 -I refresh_plan.py <output/dash> <output/managers> <поточні docs data/*.json> <папка для нових docs>
Поточні docs — вивантаження ArtifactData list collection «data» (out_dir) з файлами projects.json, people.json, tasks.json, tasks_1.json.
Вихід: projects.json, people.json, tasks.json, tasks_1.json — повні документи для ArtifactData set. data/learn не змінюється."""
import json,collections,re,sys,datetime
D=sys.argv[1].rstrip('/')+'/'; M=sys.argv[2].rstrip('/')+'/'; O=sys.argv[3].rstrip('/')+'/'; OUT=sys.argv[4].rstrip('/')+'/'
import os; os.makedirs(OUT,exist_ok=True)
ld=lambda p: json.load(open(p))
ex=ld(M+'tasks.json'); TODAY=ex['date']; CUR=ex['cur']; tasks=ex['tasks']
en=ld(M+'entries.json'); et=ld(D+'emp_tasks.json'); emp=ld(D+'employees.json')
at=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
tp={str(t['id']):str(t['pid']) for t in tasks}
pp=collections.defaultdict(float); pr=collections.defaultdict(float); tnov=collections.defaultdict(float)
for tid,rows in en.items():
  for d,e,h,txt,pd in rows:
    m=d[:7]; pid=tp.get(tid)
    if pid: pp[(pid,e,m)]+=h; pr[(pid,m)]+=h
    if m==CUR: tnov[int(tid)]+=h
r2=lambda x: round(x,2)
# projects
P=ld(O+'projects.json'); MS=P['months']
for r in P['rows']:
  for k,m in MS.items():
    v=pr.get((r['id'],m)); r[k]=r2(v) if v else (None if r.get(k) is None else 0.0)
P['date']=TODAY; P['updated']={'at':at,'by':'Claude'}
# people
Pe=ld(O+'people.json')
keep_m={r['m'] for r in Pe['emps']}
names={r['name'] for r in Pe['emps']}
E={ (x['employee'],x['month']):x for x in emp}
for r in Pe['emps']:
  x=E.get((r['name'],r['m']))
  if x: r.update(fact=x['counted_mtd'],paid=x['paid_mtd'],wd=x['wd_elapsed'],wdt=x['wd_total'])
mons=sorted({r['m'] for r in Pe['pp']})
okp={r['e'] for r in Pe['pp']}|names; okid={r['id'] for r in P['rows']}
Pe['pp']=[{'id':k[0],'e':k[1],'m':k[2],'h':r2(v)} for k,v in sorted(pp.items()) if k[2] in mons and v>0 and k[1] in okp and k[0] in okid]
Pe['date']=TODAY; Pe['updated']={'at':at,'by':'Claude'}
# tasks
old=ld(O+'tasks.json')['tasks']+ld(O+'tasks_1.json')['tasks']; oid={t['id']:t for t in old}
dl={}
for x in et:
  i=int(re.search(r'/task/(\d+)',x['url']).group(1))
  if x.get('deadline'): dl[i]=x['deadline']
ids={t['id'] for t in tasks if t['id'] in oid or ((t['closed'] is None or t['closed']>=CUR) and 'Definitively' not in t['st'])}
byid={t['id']:t for t in tasks}
for i in list(ids):
  p=byid[i]['par']
  while p and p in byid and p not in ids: ids.add(p); p=byid[p]['par']
out=[]
for t in tasks:
  if t['id'] not in ids: continue
  o=oid.get(t['id'],{})
  out.append({'id':t['id'],'pid':str(t['pid']),'t':t['t'][:70],'s':t['s'],'pl':t['pl'],'f':t['f'],'e':t['e'],'dl':dl.get(t['id'],o.get('dl')),'par':t['par'],'nov':r2(tnov.get(t['id'],0.0))})
# split by size
def size(x): return len(json.dumps(x,ensure_ascii=False).encode())
parts=[[]]
for t in out:
  parts[-1].append(t)
  if size(parts[-1])>200000: parts.append([])
assert len(parts)<=2, len(parts)
while len(parts)<2: parts.append([])
for n,pt in enumerate(parts):
  json.dump({'tasks':pt,'date':TODAY,'month':CUR,'part':n,'parts':2},open(OUT+('tasks.json' if n==0 else 'tasks_1.json'),'w'),ensure_ascii=False)
json.dump(P,open(OUT+'projects.json','w'),ensure_ascii=False); json.dump(Pe,open(OUT+'people.json','w'),ensure_ascii=False)
print('date',TODAY,'tasks',len(out),'new',sum(t['id'] not in oid for t in out),'parts',[len(p) for p in parts],'pp',len(Pe['pp']),'emps',len(Pe['emps']))
for f in os.listdir(OUT): print(f, os.path.getsize(OUT+f))
