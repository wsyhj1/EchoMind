"""Bounded per-user operational aggregates; never returns questions or source text."""
from collections import Counter
import json
import math
from statistics import median

from fastapi import APIRouter,Query,Request
from auth import require_matching_user
from runtime_limits import ChatLimits

router=APIRouter(tags=['operations'])


def summarize_runs(runs):
    statuses=Counter()
    durations=[]
    model_calls=tokens=failed_spans=0
    for run in runs:
        statuses[run.get('status','unknown')]+=1
        duration=run.get('timings',{}).get('server_total_ms')
        if isinstance(duration,(float,int)) and math.isfinite(duration) and duration>=0:
            durations.append(duration)
        for span in run.get('spans',[]):
            failed_spans+=span.get('status')=='failed'
            if span.get('kind')=='model':
                model_calls+=1
                usage=span.get('attributes',{}).get('usage') or {}
                total=usage.get('total_tokens')
                if isinstance(total,int) and total>=0:
                    tokens+=total
    durations.sort()
    alerts=[]
    if statuses['failed']:
        alerts.append({'kind':'failed_runs','count':statuses['failed'],'message':'存在失败回答，请查看逐轮详情。'})
    if failed_spans:
        alerts.append({'kind':'failed_stages','count':failed_spans,'message':'存在失败或回退阶段，请检查模型服务和索引状态。'})
    return {'runs':len(runs),'statuses':dict(statuses),'p50_ms':median(durations) if durations else None,
            'p95_ms':durations[max(0,math.ceil(len(durations)*.95)-1)] if durations else None,
            'model_calls':model_calls,'reported_total_tokens':tokens,'token_scope':'provider-reported model usage; not billing',
            'alerts':alerts}


@router.get('/operations/summary')
async def summary(request:Request,user_id:int=Query(ge=1),hours:int=Query(default=24,ge=1,le=168)):
    require_matching_user(request,user_id)
    rows=await request.app.state.postgresql_client.pool.fetch('''SELECT snapshot FROM evaluation_runs
        WHERE user_id=$1 AND created_at>now()-($2::int*interval '1 hour') ORDER BY created_at DESC LIMIT 501''',user_id,hours)
    runs=[json.loads(row['snapshot']) if isinstance(row['snapshot'],str) else row['snapshot'] for row in rows[:500]]
    limits=ChatLimits.from_env()
    return {**summarize_runs(runs),'hours':hours,'sample_capped':len(rows)>500,
            'limits':{'total_seconds':limits.total_seconds,'model_calls':limits.model_calls,'output_tokens':limits.output_tokens}}
