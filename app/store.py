import sqlite3, json, hashlib
from pathlib import Path
from app.errors import BusinessError
from app import attribution
from app.settlement_view import refresh

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    # 归因依据版本：同一 (run_id, basis_sha) 只存一份，重试同一依据不新增结果。
    con.execute("CREATE TABLE IF NOT EXISTS settlements (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, basis_sha TEXT NOT NULL, basis TEXT NOT NULL, settlement TEXT NOT NULL, affected_events TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(run_id,basis_sha))")
    # 当前生效指针；切换失败时回滚到旧指针。
    con.execute("CREATE TABLE IF NOT EXISTS basis_state (run_id TEXT PRIMARY KEY, basis_sha TEXT NOT NULL, switched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con:
        row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
        result=json.loads(row['result'])
        result.update(revision=row['revision'],status=row['status'])
        state=con.execute('SELECT basis_sha FROM basis_state WHERE run_id=?',(run_id,)).fetchone()
        if state:
            stored=con.execute('SELECT settlement,basis,affected_events FROM settlements WHERE run_id=? AND basis_sha=?',(run_id,state['basis_sha'])).fetchone()
            if stored:
                result['settlement']=json.loads(stored['settlement'])
                result['active_basis']=json.loads(stored['basis'])
                result['active_basis_sha']=state['basis_sha']
    refresh(result)
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _canonical(attribution_basis):
    return json.dumps(attribution_basis,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def _basis_sha(basis):
    return hashlib.sha256(_canonical(basis).encode()).hexdigest()

def _events_in_run(result):
    ids=set()
    for union in result.get('event_unions',[]):
        ids.update(union.get('event_ids') or [])
    return ids

def _changed_events(old,new,run_event_ids):
    old_events=old.get('events') or {}
    new_events=new.get('events') or {}
    changed=set()
    for eid in set(old_events)|set(new_events):
        if _canonical(old_events.get(eid))!=_canonical(new_events.get(eid)):
            if eid in run_event_ids:
                changed.add(eid)
    if _canonical(old.get('faults') or [])!=_canonical(new.get('faults') or []):
        changed.update(run_event_ids)
    if (old.get('ruleset') or '')!=(new.get('ruleset') or ''):
        changed.update(run_event_ids)
    return changed

def _original_fingerprint(result):
    return hashlib.sha256(json.dumps({k:result.get(k) for k in ('records','quarantine','counts')},ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def _seed_original(con, result, run_id):
    """首次更正前把原始依据也登记成一个版本，保证随时可切回且不删除。"""
    state=con.execute('SELECT basis_sha FROM basis_state WHERE run_id=?',(run_id,)).fetchone()
    if state:
        return
    original=result.get('settlement')
    if not original:
        return
    basis=(result.get('input_snapshot') or {}).get('context',{}).get('attribution') or {}
    sha=_basis_sha(basis)
    con.execute('INSERT OR IGNORE INTO settlements(run_id,basis_sha,basis,settlement,affected_events) VALUES(?,?,?,?,?)',
                (run_id,sha,json.dumps(basis,ensure_ascii=False),json.dumps(original,ensure_ascii=False),'[]'))
    con.execute('INSERT INTO basis_state(run_id,basis_sha) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET basis_sha=excluded.basis_sha,switched_at=CURRENT_TIMESTAMP',(run_id,sha))


def submit_basis(path,run_id,basis):
    """提交一条归因依据。事务内完成校验、增量重算与落库；任一步失败整体回滚，
    当前指针保持旧版，原始记录、隔离与计数不被改写。"""
    result=get(path,run_id)
    snapshot=result.get('input_snapshot') or {}
    old_context=snapshot.get('context') or {}
    # 增量口径以“当前生效依据”为旧版；尚未提交过更正时即原始快照依据。
    old_basis=result.get('active_basis') or old_context.get('attribution') or {}
    if result.get('settlement') is None:
        raise BusinessError('SETTLEMENT_MISSING','批次缺少结算结果，请先重新计算')
    run_event_ids=_events_in_run(result)

    new_context=json.loads(json.dumps(old_context,ensure_ascii=False))
    new_context['attribution']=basis
    changed=sorted(_changed_events(old_basis,basis,run_event_ids))
    sha=_basis_sha(basis)
    fingerprint=_original_fingerprint(result)

    with connect(path) as con:
        _seed_original(con,result,run_id)
        existing=con.execute('SELECT settlement FROM settlements WHERE run_id=? AND basis_sha=?',(run_id,sha)).fetchone()
        if existing:
            # 同一依据重试：复用已存结果，不新增版本、不重算；若未生效只切回该版本。
            settlement=json.loads(existing['settlement'])
            if result.get('active_basis_sha')!=sha:
                con.execute('INSERT INTO basis_state(run_id,basis_sha) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET basis_sha=excluded.basis_sha,switched_at=CURRENT_TIMESTAMP',(run_id,sha))
            settlement.update(duplicate=True,revision=result['revision'],active_basis_sha=sha)
            return settlement
        if not changed:
            raise BusinessError('NO_AFFECTED_EVENT','该依据不影响任何片段')
        settlement=attribution.recompute(result,new_context,changed,spans=_spans_from_records(result))
        if _original_fingerprint(result)!=fingerprint:
            raise BusinessError('ORIGINAL_TAMPERED','原始记录不得因依据更正而改变')
        if not settlement.get('balanced'):
            raise BusinessError('SETTLEMENT_UNBALANCED','重算后分项之和与损失不符')
        # 只有在全部校验通过后才写入版本并切换指针；事务保证失败时一并回滚。
        con.execute('INSERT INTO settlements(run_id,basis_sha,basis,settlement,affected_events) VALUES(?,?,?,?,?)',(run_id,sha,json.dumps(basis,ensure_ascii=False),json.dumps(settlement,ensure_ascii=False),json.dumps(changed,ensure_ascii=False)))
        con.execute('INSERT INTO basis_state(run_id,basis_sha) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET basis_sha=excluded.basis_sha,switched_at=CURRENT_TIMESTAMP',(run_id,sha))
    settlement.update(duplicate=False,revision=result['revision'],active_basis_sha=sha)
    return settlement

def activate_basis(path,run_id,sha):
    """切换到已存依据版本；失败保持旧版，绝不删除任何版本。"""
    with connect(path) as con:
        stored=con.execute('SELECT settlement FROM settlements WHERE run_id=? AND basis_sha=?',(run_id,sha)).fetchone()
        if not stored:
            raise BusinessError('BASIS_NOT_FOUND','依据版本不存在',404)
        settlement=json.loads(stored['settlement'])
        if not settlement.get('balanced'):
            raise BusinessError('SETTLEMENT_UNBALANCED','该版本分项不守恒，拒绝切换')
        # 校验通过后才切换指针；事务失败则旧指针仍在。
        con.execute('INSERT INTO basis_state(run_id,basis_sha) VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET basis_sha=excluded.basis_sha,switched_at=CURRENT_TIMESTAMP',(run_id,sha))
    result=get(path,run_id)
    result['settlement'].update(switched=True,active_basis_sha=sha)
    return result['settlement']

def basis_history(path,run_id):
    with connect(path) as con:
        rows=con.execute('SELECT basis_sha,basis,affected_events,created_at FROM settlements WHERE run_id=? ORDER BY id',(run_id,)).fetchall()
        active=con.execute('SELECT basis_sha FROM basis_state WHERE run_id=?',(run_id,)).fetchone()
    active_sha=active['basis_sha'] if active else None
    return {'active_basis_sha':active_sha,'versions':[{'basis_sha':r['basis_sha'],'basis':json.loads(r['basis']),'affected_events':json.loads(r['affected_events']),'created_at':r['created_at'],'active':r['basis_sha']==active_sha} for r in rows]}

def _spans_from_records(result):
    spans={}
    for row in result.get('records',[]):
        if row.get('kind')=='EVENT':
            spans[row['id']]=(row['start'],row['end'])
    return spans
