"""把生效结算投影回 views/balance/archive，保证页面与接口同源。

更正依据后只替换 settlement，再由此函数重建损失、对账与归档视图；原始
records/quarantine/counts 不参与重建，因此不会被误删或改写。
"""
from app.attribution import settlement_rows
from app.modules.reconcile.rules import reconcile
from app.modules.archive.rules import archive_decision


def refresh(result):
    settlement=result.get('settlement')
    if not settlement:
        return result
    result['views']['loss']={
        'title':'损失电量与归因结算',
        'rows':settlement_rows(settlement),
        'metrics':{
            '可申报_kWh':settlement['claimable_kwh'],
            '电网支撑_kWh':settlement['grid_support_kwh'],
            '设备故障_kWh':settlement['fault_kwh'],
            '待查_kWh':settlement['pending_kwh'],
            '确定损失_kWh':settlement['known_loss_kwh'],
            '待查片段':settlement['pending_segments'],
        },
    }
    result['balance']=reconcile(result)
    b=result['balance']
    result['views']['reconcile']={
        'title':'时间与能量四账','rows':[b],
        'metrics':{'可申报_kWh':b['claimable_kwh'],'待查_kWh':b['pending_kwh'],
                   '未定分钟':b['unknown_seconds']//60,'并集分钟':b['union_seconds']//60,
                   '总量守恒':b['allocation_balanced']},
    }
    result['archive']=archive_decision(result)
    a=result['archive']
    result['views']['archive']={
        'title':'运行凭证','rows':result['event_unions'],
        'metrics':{'切片':a['segments'],'未定':a['unknown'],'待查片段':a['pending_segments'],'状态':a['status']},
    }
    return result
