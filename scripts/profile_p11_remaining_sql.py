"""Read-only EXPLAIN probes for matching and date-only tasks in a fresh fixture DB."""
import argparse,ast,asyncio,hashlib,json,sys
from datetime import datetime,timedelta,timezone,time
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.benchmark_p11 import Environment,OWNER
async def main(output):
    config=json.loads((ROOT/'evals/p11/load-contract.json').read_text())
    async with Environment(ROOT/output,config,'after') as env:
        source=(ROOT/'services/automatic.py').read_text();tree=ast.parse(source)
        clauses=next(ast.literal_eval(n.value) for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='clauses' for t in n.targets) and isinstance(n.value,ast.List))
        position=next(n.args[0].value for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='append' and n.args and isinstance(n.args[0],ast.Constant) and isinstance(n.args[0].value,str) and n.args[0].value.startswith('(position_name IS NULL'))
        matching='SELECT * FROM applications WHERE '+' AND '.join(clauses+[position,'(batch IS NULL OR recruitment_category(batch)=$4)'])
        recruitment=(ROOT/'services/recruitment.py').read_text();node=next(n for n in ast.walk(ast.parse(recruitment)) if isinstance(n,ast.AsyncFunctionDef) and n.name=='upcoming_tasks')
        where=next(ast.literal_eval(n.value) for n in ast.walk(node) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='where' for t in n.targets))
        upcoming="SELECT * FROM tasks WHERE "+where+" ORDER BY LEAST(starts_on,due_on,(starts_at AT TIME ZONE 'Asia/Shanghai')::date,(due_at AT TIME ZONE 'Asia/Shanghai')::date),LEAST(starts_at,due_at),id LIMIT $6 OFFSET $7"
        now=datetime.now(timezone.utc);today=now.astimezone(ZoneInfo('Asia/Shanghai')).date();end=today+timedelta(days=7)
        plans={}
        for name,sql,args in [('matching',(matching),(OWNER,'示例缺失公司','Agent开发工程师','27秋招')),('upcoming_dates',upcoming,(OWNER,now,datetime.combine(end,time.min,tzinfo=ZoneInfo('Asia/Shanghai')),today,end,20,0))]:
            plans[name]=[json.loads(await env.pool.fetchval('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+sql,*args))[0] for _ in range(3)]
        (env.out/'plans.json').write_text(json.dumps(plans,indent=2));(env.out/'sources.json').write_text(json.dumps({'automatic_sha256':hashlib.sha256(source.encode()).hexdigest(),'recruitment_sha256':hashlib.sha256(recruitment.encode()).hexdigest(),'as_of':now.isoformat(),'queries':{'matching':matching,'upcoming_dates':upcoming}},indent=2))
        print({name:[p['Execution Time'] for p in rows] for name,rows in plans.items()})
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True);a=p.parse_args();asyncio.run(main(a.output))
