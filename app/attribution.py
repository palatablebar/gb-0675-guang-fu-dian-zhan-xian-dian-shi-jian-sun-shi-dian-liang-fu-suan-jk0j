"""有功限发归因与结算。

口径（attribution-2026.11-a）：

* 辐照基线 B 与实际 A 的差额是本片段待解释的少发量；所有原因分项之和必须
  等于该差额，从构造上避免重复计入。
* 同一片段同时出现电网支撑与调度限电时，按物理发生顺序做瀑布分摊：电网支撑
  先把有功上限压到 active_cap_kw，调度限额再压到 limit_kw。两段增量分别归因，
  可申报电量只包含调度限额造成的增量。
* 设备故障（目标遥测状态故障、故障窗口覆盖、或事件申报为故障）优先认定，
  不进入可申报口径；故障窗口只覆盖片段一部分时按秒数拆分。
* 缺少可量化上限、申报上限与遥测矛盾、事件未申报或原因不可识别、实际低于
  全部申报上限且无故障解释的部分，一律留待查（pending），不填 0、不计入
  可申报。
* 片段、事件并集、全站三级总量必须相互核对，待查与未定分别展示。
"""

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json

from app.coerce import number, instant
from app.errors import BusinessError

Q = Decimal('0.0001')
CAUSES = ('GRID_SUPPORT', 'DISPATCH', 'FAULT')
CAUSE_LABELS = {
    'GRID_SUPPORT': '电网支撑',
    'DISPATCH': '调度限电',
    'FAULT': '设备故障',
    'UNKNOWN': '未定',
    'PENDING': '待查',
    'MIXED': '多原因分摊',
    'CLAIMABLE': '调度限电可申报',
    'GRID_SUPPORT_ONLY': '仅电网支撑',
    'NO_LOSS': '无少发',
    'FAULT_ONLY': '设备故障',
}
REASON_LABELS = {
    'NO_BASELINE': '无合格对照基线',
    'NO_ACTUAL': '目标遥测缺测',
    'CAUSE_MISSING': '事件未申报原因',
    'CAUSE_UNKNOWN': '事件原因不可识别',
    'BASIS_INVALID': '原因依据非法',
    'MISSING_CAP': '缺少可量化上限',
    'CAP_CONTRADICTION': '实际功率高于申报上限',
    'NO_BINDING_CAP': '申报上限未低于基线',
    'BELOW_CAP_UNEXPLAINED': '低于全部申报上限且无故障解释',
    'NO_LOSS': '实际不低于基线',
    'MIXED': '多原因分摊',
    'CLAIMABLE': '调度限电可申报',
    'GRID_SUPPORT_ONLY': '仅电网支撑不可申报',
    'FAULT_ONLY': '设备故障不可申报',
    'PENDING': '证据不足待查',
}


def quant(value):
    return str(Decimal(value).quantize(Q, rounding=ROUND_HALF_UP))


def basis_digest(entry):
    canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode()).hexdigest()


def normalize_entry(entry):
    """校验并规范化一条归因依据；供更正入口使用，非法输入不落库。"""
    if not isinstance(entry, dict):
        raise BusinessError('BAD_BASIS', '归因依据须为对象')
    cause = str(entry.get('cause', '')).strip().upper()
    if cause not in CAUSES:
        raise BusinessError('BAD_BASIS', '原因码无效')
    basis_id = str(entry.get('basis_id', '')).strip()
    if not basis_id:
        raise BusinessError('BAD_BASIS', '依据编号不能为空')
    out = {'cause': cause, 'basis_id': basis_id}
    if cause == 'GRID_SUPPORT':
        if 'active_cap_kw' not in entry or str(entry.get('active_cap_kw', '')).strip() == '':
            raise BusinessError('BAD_BASIS', '电网支撑缺少有功上限')
        cap = number(entry['active_cap_kw'])
        if cap <= 0:
            raise BusinessError('BAD_BASIS', '有功上限须为正数')
        out['active_cap_kw'] = str(cap)
    elif cause == 'DISPATCH':
        if 'limit_kw' not in entry or str(entry.get('limit_kw', '')).strip() == '':
            raise BusinessError('BAD_BASIS', '调度限电缺少限额')
        cap = number(entry['limit_kw'])
        if cap <= 0:
            raise BusinessError('BAD_BASIS', '限额须为正数')
        out['limit_kw'] = str(cap)
    return out


def validate_basis(basis):
    """校验归因依据的整体结构；缺少具体上限属于“证据不足”，留待结算判定为待查。"""
    if not isinstance(basis, dict):
        raise BusinessError('BAD_BASIS', '归因依据须为对象')
    events = basis.get('events', {})
    if not isinstance(events, dict):
        raise BusinessError('BAD_BASIS', '事件依据须为对象')
    for eid, entry in events.items():
        if not isinstance(entry, dict):
            raise BusinessError('BAD_BASIS', f'事件 {eid} 依据须为对象')
        cause = str(entry.get('cause', '')).upper()
        if cause not in CAUSES:
            raise BusinessError('BAD_BASIS', f'事件 {eid} 原因码无效')
        if not str(entry.get('basis_id', '')).strip():
            raise BusinessError('BAD_BASIS', f'事件 {eid} 缺少依据编号')
    faults = basis.get('faults', [])
    if not isinstance(faults, list):
        raise BusinessError('BAD_BASIS', '故障窗口须为列表')
    for item in faults:
        if not isinstance(item, dict) or not item.get('device'):
            raise BusinessError('BAD_BASIS', '故障窗口缺少设备')
        instant(item.get('start'))
        instant(item.get('end'))
    return basis


def _merge(intervals):
    merged = []
    for start, end, basis in sorted(intervals, key=lambda item: (item[0], item[1], item[2])):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end, merged[-1][2])
            if basis not in merged[-1][2]:
                merged[-1][2].append(basis)
        else:
            merged.append((start, end, [basis]))
    return merged


def _fault_intervals(row, context, event_spans):
    """返回故障覆盖区间；非法依据记录为阻断原因。"""
    start, end = instant(row['start']), instant(row['end'])
    intervals = []
    issues = []
    if row.get('status') == '故障':
        intervals.append((start, end, 'STATUS_FAULT'))
    for eid in row.get('event_ids') or []:
        entry = ((context.get('attribution') or {}).get('events') or {}).get(eid)
        if isinstance(entry, dict) and str(entry.get('cause', '')).upper() == 'FAULT':
            span = event_spans.get(eid)
            a, b = (instant(span[0]), instant(span[1])) if span else (start, end)
            a, b = max(start, a), min(end, b)
            if a < b:
                intervals.append((a, b, str(entry.get('basis_id', eid))))
    att = context.get('attribution') or {}
    for item in att.get('faults') or []:
        if not isinstance(item, dict) or item.get('device') != row['device']:
            continue
        try:
            f_start, f_end = instant(item['start']), instant(item['end'])
        except (BusinessError, KeyError):
            issues.append(('BASIS_INVALID', '故障窗口'))
            continue
        if not f_start < f_end:
            issues.append(('BASIS_INVALID', '故障窗口'))
            continue
        a, b = max(start, f_start), min(end, f_end)
        if a < b:
            intervals.append((a, b, str(item.get('basis_id', 'FAULT'))))
    return _merge(intervals), issues


def _declarations(row, context):
    """读取片段所依据的事件申报，返回 (grid, dispatch, 事件故障, 阻断原因, 依据号)。"""
    events = (context.get('attribution') or {}).get('events') or {}
    grid = None
    dispatch = None
    fault_event = False
    issues = []
    basis_ids = []
    for eid in row.get('event_ids') or []:
        if eid not in events:
            issues.append(('CAUSE_MISSING', eid))
            continue
        entry = events[eid]
        if not isinstance(entry, dict):
            issues.append(('CAUSE_UNKNOWN', eid))
            continue
        cause = str(entry.get('cause', '')).upper()
        basis_id = str(entry.get('basis_id', '')).strip()
        if basis_id:
            basis_ids.append(basis_id)
        if cause == 'FAULT':
            fault_event = True
            continue
        if cause not in ('GRID_SUPPORT', 'DISPATCH'):
            issues.append(('CAUSE_UNKNOWN', eid))
            continue
        field = 'active_cap_kw' if cause == 'GRID_SUPPORT' else 'limit_kw'
        if field not in entry:
            issues.append(('MISSING_CAP', eid))
            continue
        try:
            cap = number(entry[field])
        except BusinessError:
            issues.append(('BASIS_INVALID', eid))
            continue
        if cap <= 0:
            issues.append(('BASIS_INVALID', eid))
            continue
        item = (cap, basis_id or eid)
        if cause == 'GRID_SUPPORT':
            grid = item if grid is None or cap < grid[0] else grid
        else:
            dispatch = item if dispatch is None or cap < dispatch[0] else dispatch
    return grid, dispatch, fault_event, issues, sorted(basis_ids)


def _blanks():
    return {'grid_support_kwh': '0', 'claimable_kwh': '0', 'fault_kwh': '0', 'pending_kwh': '0'}


def _unknown(row, code, reason):
    return {
        **row,
        'grid_support_kwh': None,
        'claimable_kwh': None,
        'fault_kwh': None,
        'pending_kwh': None,
        'code': code,
        'reason': REASON_LABELS.get(reason, reason),
        'reason_code': reason,
        'basis_ids': '',
    }


def attribute_segment(row, context, event_spans=None):
    """对单个已切片片段做归因；各分项之和严格等于该片段损失。"""
    event_spans = event_spans or {}
    if row.get('loss_kwh') is None:
        if row.get('actual_kw') is None:
            return _unknown(row, 'UNKNOWN', 'NO_ACTUAL')
        return _unknown(row, 'UNKNOWN', 'NO_BASELINE')

    loss = number(row['loss_kwh'])
    seconds = Decimal(row['seconds'])
    if loss <= 0 or seconds <= 0:
        return {**row, **_blanks(), 'code': 'NO_LOSS', 'reason': REASON_LABELS['NO_LOSS'],
                'reason_code': 'NO_LOSS', 'basis_ids': ''}

    baseline = number(row['baseline_kw'])
    actual = number(row['actual_kw'])
    fault_intervals, fault_issues = _fault_intervals(row, context, event_spans)
    grid, dispatch, fault_event, issues, basis_ids = _declarations(row, context)
    issues = fault_issues + issues

    start, end = instant(row['start']), instant(row['end'])
    fault_seconds = sum((min(end, b) - max(start, a)).total_seconds() for a, b, _ in fault_intervals)
    fault_seconds = Decimal(str(min(fault_seconds, row['seconds'])))
    if fault_event:
        fault_seconds = seconds
    non_fault_seconds = seconds - fault_seconds

    fault_kwh = loss * fault_seconds / seconds
    remaining = loss - fault_kwh
    if non_fault_seconds <= 0 or remaining <= Q:
        # 整段被故障覆盖；把除法尾差并入故障，保证分项之和精确等于损失。
        return {**row, 'grid_support_kwh': str(Decimal(0)), 'claimable_kwh': str(Decimal(0)),
                'fault_kwh': str(loss), 'pending_kwh': str(Decimal(0)), 'code': 'FAULT_ONLY',
                'reason': REASON_LABELS['FAULT_ONLY'], 'reason_code': 'FAULT_ONLY',
                'basis_ids': ','.join(basis_ids)}

    def finish(grid_kwh, claim_kwh, fault, pending, code, reason_code):
        return {
            **row,
            'grid_support_kwh': str(grid_kwh),
            'claimable_kwh': str(claim_kwh),
            'fault_kwh': str(fault),
            'pending_kwh': str(pending),
            'code': code,
            'reason': REASON_LABELS.get(reason_code, reason_code),
            'reason_code': reason_code,
            'basis_ids': ','.join(basis_ids),
        }

    # 依据缺失或非法时，非故障部分整体留待查，不做猜测。
    if issues:
        return finish(Decimal(0), Decimal(0), fault_kwh, remaining, 'PENDING', issues[0][0])

    # 实际高于已申报的约束上限，说明遥测与依据冲突，不结算。
    for cap in (grid, dispatch):
        if cap is not None and cap[0] < baseline and actual > cap[0]:
            return finish(Decimal(0), Decimal(0), fault_kwh, remaining, 'PENDING', 'CAP_CONTRADICTION')

    shortfall_kw = baseline - actual

    # 瀑布：先电网支撑压到 min(B,G)，再调度压到 min(上一步,D)。
    grid_kw = Decimal(0)
    ceiling = baseline
    if grid is not None and grid[0] < baseline:
        grid_kw = min(baseline - grid[0], shortfall_kw)
        ceiling = grid[0]
    dispatch_kw = Decimal(0)
    if dispatch is not None and dispatch[0] < ceiling:
        dispatch_kw = min(ceiling - dispatch[0], shortfall_kw - grid_kw)

    if grid_kw <= 0 and dispatch_kw <= 0:
        # 有申报但所有上限都不低于基线，解释不了任何少发。
        return finish(Decimal(0), Decimal(0), fault_kwh, remaining, 'PENDING', 'NO_BINDING_CAP')

    pending_kw = max(Decimal(0), shortfall_kw - grid_kw - dispatch_kw)
    factor = non_fault_seconds / Decimal(3600)
    grid_kwh = grid_kw * factor
    claim_kwh = dispatch_kw * factor
    pending_kwh = remaining - grid_kwh - claim_kwh
    # pending_kwh 与短路电流无关，仅吸收除法尾差或已解释部分不足的残差。

    if pending_kwh <= Q:
        pending_kwh = Decimal(0)
        # 仅吸收除法尾差，保证分项之和精确等于片段损失；尾差归入已存在的分项，
        # 不应凭尾差凭空制造出可申报电量。
        residual = remaining - grid_kwh - claim_kwh
        if claim_kwh > 0:
            claim_kwh += residual
        else:
            grid_kwh += residual
    if pending_kwh > 0:
        # 存在未解释少发：即使有可申报部分，也须在原因上标出待查。
        code = 'MIXED' if claim_kwh > 0 or grid_kwh > 0 else 'PENDING'
        return finish(grid_kwh, claim_kwh, fault_kwh, pending_kwh, code, 'PENDING')
    if claim_kwh > 0 and grid_kwh > 0:
        code, reason = 'MIXED', 'MIXED'
    elif claim_kwh > 0:
        code, reason = 'CLAIMABLE', 'CLAIMABLE'
    elif grid_kwh > 0:
        code, reason = 'GRID_SUPPORT_ONLY', 'GRID_SUPPORT_ONLY'
    else:
        code, reason = 'PENDING', 'BELOW_CAP_UNEXPLAINED'
    return finish(grid_kwh, claim_kwh, fault_kwh, pending_kwh, code, reason)


def settlement_rows(settlement):
    """把结算分项转成页面/接口用的行：带可读原因与依据。"""
    rows=[]
    for row in settlement['rows']:
        rows.append({
            'device':row['device'],'start':row['start'],'end':row['end'],'seconds':row['seconds'],
            'actual_kw':row['actual_kw'],'baseline_kw':row['baseline_kw'],'loss_kwh':row['loss_kwh'],
            'grid_support_kwh':row['grid_support_kwh'],'claimable_kwh':row['claimable_kwh'],
            'fault_kwh':row['fault_kwh'],'pending_kwh':row['pending_kwh'],
            'event_ids':row.get('event_ids') or [],'basis_ids':row.get('basis_ids',''),
            'reference_ids':row.get('reference_ids') or [],'sample_id':row.get('sample_id',''),
            'reason':row.get('reason',''),'reason_code':row.get('reason_code',''),
        })
    return rows


def _sum(rows, key):
    return sum((number(r[key]) for r in rows if r.get(key) is not None), Decimal(0))


def rollup(rows, unions):
    known = [r for r in rows if r.get('loss_kwh') is not None]
    unknown = [r for r in rows if r.get('loss_kwh') is None]
    totals = {
        'claimable_kwh': _sum(known, 'claimable_kwh'),
        'grid_support_kwh': _sum(known, 'grid_support_kwh'),
        'fault_kwh': _sum(known, 'fault_kwh'),
        'pending_kwh': _sum(known, 'pending_kwh'),
        'known_loss_kwh': _sum(known, 'loss_kwh'),
    }
    allocated = totals['claimable_kwh'] + totals['grid_support_kwh'] + totals['fault_kwh'] + totals['pending_kwh']
    balanced = quant(allocated) == quant(totals['known_loss_kwh'])

    grouped = {}
    for row in rows:
        key = (row['device'], tuple(row.get('event_ids') or []))
        item = grouped.setdefault(key, {'device': row['device'], 'start': row['start'], 'end': row['end'],
                                        'seconds': 0, 'event_ids': row.get('event_ids') or [],
                                        'known_loss_kwh': Decimal(0), 'claimable_kwh': Decimal(0),
                                        'grid_support_kwh': Decimal(0), 'fault_kwh': Decimal(0),
                                        'pending_kwh': Decimal(0), 'unknown_seconds': 0, 'segments': 0})
        item['segments'] += 1
        item['seconds'] += row['seconds']
        item['start'] = min(item['start'], row['start'])
        item['end'] = max(item['end'], row['end'])
        if row.get('loss_kwh') is None:
            item['unknown_seconds'] += row['seconds']
            continue
        item['known_loss_kwh'] += number(row['loss_kwh'])
        for field in ('claimable_kwh', 'grid_support_kwh', 'fault_kwh', 'pending_kwh'):
            item[field] += number(row[field])
    union_rows = []
    for item in grouped.values():
        if item['unknown_seconds']:
            item['status'] = '未定'
        elif item['claimable_kwh'] > 0 and item['pending_kwh'] == 0:
            item['status'] = '可结算'
        elif item['pending_kwh'] > 0:
            item['status'] = '待查'
        else:
            item['status'] = '不可申报'
        for field in ('known_loss_kwh', 'claimable_kwh', 'grid_support_kwh', 'fault_kwh', 'pending_kwh'):
            item[field] = quant(item[field])
        union_rows.append(item)
    union_rows.sort(key=lambda r: (r['device'], r['start']))

    union_seconds = sum(r['seconds'] for r in unions)
    slice_seconds = sum(r['seconds'] for r in rows)
    return {
        'ruleset': None,
        'claimable_kwh': quant(totals['claimable_kwh']),
        'grid_support_kwh': quant(totals['grid_support_kwh']),
        'fault_kwh': quant(totals['fault_kwh']),
        'pending_kwh': quant(totals['pending_kwh']),
        'known_loss_kwh': quant(totals['known_loss_kwh']),
        'claimable_segments': sum(1 for r in known if number(r['claimable_kwh']) > 0),
        'grid_segments': sum(1 for r in known if number(r['grid_support_kwh']) > 0),
        'fault_segments': sum(1 for r in known if number(r['fault_kwh']) > 0),
        'pending_segments': sum(1 for r in known if number(r['pending_kwh']) > 0),
        'unknown_segments': len(unknown),
        'unknown_seconds': sum(r['seconds'] for r in unknown),
        'balanced': balanced,
        'union_seconds': union_seconds,
        'slice_seconds': slice_seconds,
        'time_balanced': union_seconds == slice_seconds,
        'rows': rows,
        'unions': union_rows,
    }


def event_spans(state):
    spans = {}
    for row in state.get('records', []):
        if row.get('kind') == 'EVENT':
            spans[row['id']] = (row['start'], row['end'])
    return spans


def settle(state):
    spans = event_spans(state)
    rows = [attribute_segment(row, state['context'], spans) for row in state['energy_rows']]
    result = rollup(rows, state['event_unions'])
    result['ruleset'] = (state['context'].get('attribution') or {}).get('ruleset')
    return result


def recompute(previous, context, affected_event_ids, spans=None):
    """只重算受影响事件的片段，其余归因行原样保留。"""
    settled = previous['settlement']
    rows = [dict(row) for row in settled['rows']]
    affected = set(affected_event_ids)
    spans = spans or {}
    rebuilt = 0
    for index, row in enumerate(rows):
        if affected & set(row.get('event_ids') or []):
            rows[index] = attribute_segment(row, context, spans)
            rebuilt += 1
    if rebuilt == 0:
        raise BusinessError('NO_AFFECTED_EVENT', '该依据不影响任何片段')
    result = rollup(rows, previous['event_unions'])
    result['ruleset'] = (context.get('attribution') or {}).get('ruleset')
    result['affected_events'] = sorted(affected)
    result['recomputed_segments'] = rebuilt
    result['untouched_segments'] = len(rows) - rebuilt
    return result
