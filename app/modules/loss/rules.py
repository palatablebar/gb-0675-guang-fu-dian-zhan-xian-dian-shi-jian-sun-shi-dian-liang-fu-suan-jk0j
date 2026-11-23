from decimal import Decimal
from app.coerce import number
from app.attribution import settle

def integrate(estimate):
    if estimate['baseline_kw'] is None:return {**estimate,'loss_kwh':None,'actual_kwh':None,'baseline_kwh':None}
    hours=Decimal(estimate['seconds'])/3600;actual=number(estimate['actual_kw']);baseline=number(estimate['baseline_kw']);loss=max(Decimal(0),baseline-actual)*hours
    # 保留计算精度，汇总后再统一保留四位，避免逐段量化制造尾差。
    return {**estimate,'loss_kwh':str(loss),'actual_kwh':str(actual*hours),'baseline_kwh':str((actual+max(Decimal(0),baseline-actual))*hours),'code':'LOSS' if loss else 'NO_LOSS'}

def apply(state):
    state['energy_rows']=[integrate(e) for e in state['estimates']]
    state['settlement']=settle(state)
    from app.settlement_view import refresh
    refresh(state)
