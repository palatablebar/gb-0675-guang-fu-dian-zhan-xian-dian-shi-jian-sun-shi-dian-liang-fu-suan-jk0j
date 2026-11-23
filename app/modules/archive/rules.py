import hashlib,json

def archive_decision(state):
    b=state['balance'];ready=(b['row_balanced'] and b['time_balanced'] and b['energy_balanced'] and b.get('allocation_balanced',False)
           and not b['unknown_segments'] and not b.get('pending_segments',0) and not state['quarantine'])
    return {'status':'可归档' if ready else '待复核','seal':hashlib.sha256(json.dumps({'energy_rows':state['energy_rows'],'settlement':state.get('settlement')},sort_keys=True,default=str).encode()).hexdigest(),'segments':len(state['energy_rows']),'unknown':b['unknown_segments'],'pending_segments':b.get('pending_segments',0),'claimable_kwh':b.get('claimable_kwh')}
def apply(state):
    state['archive']=archive_decision(state);state['views']['archive']={'title':'运行凭证','rows':state['event_unions'],'metrics':{'切片':state['archive']['segments'],'未定':state['archive']['unknown'],'待查片段':state['archive']['pending_segments'],'状态':state['archive']['status']}}
