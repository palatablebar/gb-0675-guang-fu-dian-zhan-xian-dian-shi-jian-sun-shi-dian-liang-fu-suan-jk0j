from decimal import Decimal
from copy import deepcopy

import pytest

from app.engine import evaluate
from app.attribution import attribute_segment, settle, event_spans, recompute, normalize_entry
from app.errors import BusinessError


def parts(row):
    return [Decimal(row[k]) for k in ('grid_support_kwh', 'claimable_kwh', 'fault_kwh', 'pending_kwh')]


def test_grid_and_dispatch_split(clean):
    # 同片段同时有电网支撑与调度限额：按瀑布分摊，且分项之和等于损失。
    rows = settle(clean)['rows']
    a = next(r for r in rows if r['device'] == 'INV-A')
    assert Decimal(a['grid_support_kwh']) == Decimal('3.333333333333333333333333334')
    assert Decimal(a['claimable_kwh']) == Decimal('3.333333333333333333333333334')
    assert sum(parts(a)) == Decimal(a['loss_kwh'])
    assert a['code'] == 'MIXED'


def test_no_double_count(clean):
    # 可申报 + 电网支撑 + 故障 + 待查 == 已确定损失，总量不重复。
    result = settle(clean)
    assert result['balanced'] is True
    assert result['known_loss_kwh'] == '26.6667'
    assert result['claimable_kwh'] == '16.6667'
    assert result['grid_support_kwh'] == '10.0000'


def test_dispatch_only_fully_claimable(clean):
    rows = settle(clean)['rows']
    b = next(r for r in rows if r['device'] == 'INV-B')
    assert b['reason_code'] == 'CLAIMABLE'
    assert Decimal(b['grid_support_kwh']) == 0
    assert Decimal(b['claimable_kwh']) == Decimal(b['loss_kwh'])


def test_actual_below_dispatch_limit_is_pending(clean):
    # 实际低于申报限额，多出来的少发没有依据，不能算可申报。
    ctx = deepcopy(clean['context'])
    ctx['attribution']['events']['EB1'] = {'cause': 'DISPATCH', 'limit_kw': '40', 'basis_id': 'ORD-B-02'}
    row = next(r for r in clean['energy_rows'] if r['device'] == 'INV-B')
    out = attribute_segment(row, ctx, event_spans(clean))
    assert out['reason_code'] == 'PENDING'
    assert Decimal(out['claimable_kwh']) > 0
    assert Decimal(out['pending_kwh']) > 0
    assert sum(parts(out)) == Decimal(row['loss_kwh'])


def test_missing_basis_is_pending_not_zero(clean):
    ctx = deepcopy(clean['context'])
    del ctx['attribution']['events']['EB1']
    row = next(r for r in clean['energy_rows'] if r['device'] == 'INV-B')
    out = attribute_segment(row, ctx, event_spans(clean))
    assert out['reason_code'] == 'CAUSE_MISSING'
    assert out['claimable_kwh'] is not None and Decimal(out['claimable_kwh']) == 0
    assert Decimal(out['pending_kwh']) == Decimal(row['loss_kwh'])


def test_fault_window_splits_by_seconds(clean):
    ctx = deepcopy(clean['context'])
    ctx['attribution']['faults'] = [{'device': 'INV-B', 'start': '2026-10-15T10:00:00+08:00',
                                     'end': '2026-10-15T10:05:00+08:00', 'basis_id': 'FAULT-B-1'}]
    row = next(r for r in clean['energy_rows'] if r['device'] == 'INV-B')
    out = attribute_segment(row, ctx, event_spans(clean))
    assert Decimal(out['fault_kwh']) == Decimal('1.666666666666666666666666667')
    assert Decimal(out['claimable_kwh']) == Decimal('1.666666666666666666666666667')
    assert sum(parts(out)) == Decimal(row['loss_kwh'])


def test_fault_status_overrides_claim(clean):
    row = dict(next(r for r in clean['energy_rows'] if r['device'] == 'INV-B'), status='故障')
    out = attribute_segment(row, clean['context'], event_spans(clean))
    assert out['reason_code'] == 'FAULT_ONLY'
    assert Decimal(out['claimable_kwh']) == 0
    assert Decimal(out['fault_kwh']) == Decimal(row['loss_kwh'])


def test_cap_above_baseline_does_not_bind(clean):
    ctx = deepcopy(clean['context'])
    ctx['attribution']['events']['EB1'] = {'cause': 'DISPATCH', 'limit_kw': '60', 'basis_id': 'ORD-B-03'}
    row = next(r for r in clean['energy_rows'] if r['device'] == 'INV-B')
    out = attribute_segment(row, ctx, event_spans(clean))
    assert out['reason_code'] == 'NO_BINDING_CAP'
    assert Decimal(out['claimable_kwh']) == 0


def test_cap_contradiction_is_pending(clean):
    ctx = deepcopy(clean['context'])
    ctx['attribution']['events']['EB1'] = {'cause': 'DISPATCH', 'limit_kw': '20', 'basis_id': 'ORD-B-04'}
    row = next(r for r in clean['energy_rows'] if r['device'] == 'INV-B')
    out = attribute_segment(row, ctx, event_spans(clean))
    assert out['reason_code'] == 'CAP_CONTRADICTION'
    assert Decimal(out['claimable_kwh']) == 0


def test_unknown_baseline_stays_pending(sample):
    result = settle(sample)
    assert result['unknown_segments'] == 2
    assert result['balanced'] is True
    assert result['claimable_kwh'] == '10.0000'


def test_archive_blocks_on_pending(clean):
    from app.modules.archive.rules import archive_decision
    ctx = deepcopy(clean['context'])
    del ctx['attribution']['events']['EB1']
    from app.attribution import rollup
    spans = event_spans(clean)
    rows = [attribute_segment(row, ctx, spans) for row in clean['energy_rows']]
    settlement = rollup(rows, clean['event_unions'])
    assert settlement['pending_segments'] > 0
    state = {'balance': {'row_balanced': True, 'time_balanced': True, 'energy_balanced': True,
                         'allocation_balanced': settlement['balanced'],
                         'unknown_segments': 0, 'pending_segments': settlement['pending_segments']},
             'quarantine': [], 'energy_rows': rows, 'settlement': settlement}
    assert archive_decision(state)['status'] == '待复核'


def test_recompute_only_affected_events(clean):
    previous = {'settlement': settle(clean), 'event_unions': clean['event_unions']}
    ctx = deepcopy(clean['context'])
    ctx['attribution']['events']['EA2'] = {'cause': 'DISPATCH', 'limit_kw': '70', 'basis_id': 'ORD-A-05'}
    result = recompute(previous, ctx, ['EA2'], spans=event_spans(clean))
    assert result['affected_events'] == ['EA2']
    assert result['recomputed_segments'] == 3
    assert result['untouched_segments'] == 2
    assert result['balanced'] is True
    # INV-B 的归因结果应当保持原样。
    before = {r['sample_id']: r for r in previous['settlement']['rows'] if r['device'] == 'INV-B'}
    after = {r['sample_id']: r for r in result['rows'] if r['device'] == 'INV-B'}
    assert before == after


def test_normalize_entry_requires_cap():
    with pytest.raises(BusinessError):
        normalize_entry({'cause': 'DISPATCH', 'basis_id': 'X'})
    with pytest.raises(BusinessError):
        normalize_entry({'cause': 'GRID_SUPPORT', 'active_cap_kw': '-1', 'basis_id': 'X'})
    assert normalize_entry({'cause': 'DISPATCH', 'limit_kw': '60', 'basis_id': 'X'})['limit_kw'] == '60'
