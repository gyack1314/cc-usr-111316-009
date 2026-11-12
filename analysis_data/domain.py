"""仅核对输入形状，不生成分析结果。"""
import json
from .io import asset,unique,integer
def summarize(v,root):
    rows=[json.loads(x) for x in asset(root,v['periods']).splitlines()];unique(rows,'id');integer(v['solver']['max_steps'],1)
    if v['battery']['capacity'].startswith('-'):raise ValueError('容量为负')
    return {'periods':len(rows),'emission_caps':len(v['emission_caps']),'billing_periods':len(v['demand_fees'])}
