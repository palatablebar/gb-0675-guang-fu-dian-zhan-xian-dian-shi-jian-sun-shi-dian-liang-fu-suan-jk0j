import pytest
from fastapi.testclient import TestClient

from app import main, store, attribution
from app.errors import BusinessError


def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)


def corrected_basis(limit_a1='90'):
    return {
        'ruleset': 'attribution-2026.11-a',
        'events': {
            'EA1': {'cause': 'DISPATCH', 'limit_kw': limit_a1, 'basis_id': 'ORD-A-09'},
            'EA2': {'cause': 'DISPATCH', 'limit_kw': '60', 'basis_id': 'ORD-A-01'},
            'EB1': {'cause': 'DISPATCH', 'limit_kw': '30', 'basis_id': 'ORD-B-01'},
        },
        'faults': [],
    }


def test_correction_recomputes_only_affected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    before = {r['sample_id']: r for r in run['settlement']['rows'] if r['device'] == 'INV-B'}
    out = c.post('/api/runs/' + rid + '/basis', json={'attribution': corrected_basis()}).json()
    assert out['affected_events'] == ['EA1']
    assert out['recomputed_segments'] == 3
    assert out['untouched_segments'] == 2
    after = {r['sample_id']: r for r in out['rows'] if r['device'] == 'INV-B'}
    assert before == after
    assert out['balanced'] is True


def test_retry_same_basis_no_new_version(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    first = c.post('/api/runs/' + rid + '/basis', json={'attribution': corrected_basis()}).json()
    count = len(c.get('/api/runs/' + rid + '/basis').json()['versions'])
    again = c.post('/api/runs/' + rid + '/basis', json={'attribution': corrected_basis()}).json()
    assert again['duplicate'] is True
    assert len(c.get('/api/runs/' + rid + '/basis').json()['versions']) == count
    assert again['claimable_kwh'] == first['claimable_kwh']


def test_switch_back_keeps_versions(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    c.post('/api/runs/' + rid + '/basis', json={'attribution': corrected_basis()})
    history = c.get('/api/runs/' + rid + '/basis').json()
    original = next(v for v in history['versions'] if v['basis'].get('events', {}).get('EA1', {}).get('cause') == 'GRID_SUPPORT')
    back = c.post('/api/runs/' + rid + '/basis/' + original['basis_sha'] + '/activate').json()
    assert back['claimable_kwh'] == '16.6667'
    assert back['grid_support_kwh'] == '10.0000'
    latest = c.get('/api/runs/' + rid + '/basis').json()
    assert latest['active_basis_sha'] == original['basis_sha']
    assert len(latest['versions']) == len(history['versions'])


def test_failed_switch_restores_old(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    c.post('/api/runs/' + rid + '/basis', json={'attribution': corrected_basis()})
    active = c.get('/api/runs/' + rid + '/basis').json()['active_basis_sha']
    assert c.post('/api/runs/' + rid + '/basis/deadbeef/activate').status_code == 404
    assert c.get('/api/runs/' + rid + '/basis').json()['active_basis_sha'] == active


def test_failed_recompute_rolls_back(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'clean'}).json()
    rid = run['manifest']['run_id']
    original = attribution.recompute

    def boom(*args, **kwargs):
        raise BusinessError('BOOM', '模拟失败')

    monkeypatch.setattr(attribution, 'recompute', boom)
    resp = c.post('/api/runs/' + rid + '/basis', json={'attribution': corrected_basis()})
    monkeypatch.setattr(attribution, 'recompute', original)
    assert resp.status_code == 422
    history = c.get('/api/runs/' + rid + '/basis').json()
    assert history['versions'] == []
    fresh = c.get('/api/runs/' + rid).json()
    assert fresh['balance']['claimable_kwh'] == '16.6667'


def test_originals_never_dropped(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={'dataset': 'dirty'}).json()
    rid = run['manifest']['run_id']
    before = (len(run['records']), len(run['quarantine']), run['counts'])
    c.post('/api/runs/' + rid + '/basis', json={'attribution': corrected_basis()})
    after = c.get('/api/runs/' + rid).json()
    assert (len(after['records']), len(after['quarantine']), after['counts']) == before


def test_invalid_basis_rejected(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    bad = {'ruleset': 'x', 'events': {'EA1': {'cause': 'NOPE', 'basis_id': 'x'}}, 'faults': []}
    resp = c.post('/api/runs/' + rid + '/basis', json={'attribution': bad})
    assert resp.status_code == 422
    assert c.get('/api/runs/' + rid).json()['balance']['claimable_kwh'] == '16.6667'


def test_missing_cap_becomes_pending(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    # EA1 申报调度但没给限额：属于证据不足，结算为待查而不是填 0 或高估。
    basis = corrected_basis()
    basis['events']['EA1'] = {'cause': 'DISPATCH', 'basis_id': 'ORD-A-99'}
    out = c.post('/api/runs/' + rid + '/basis', json={'attribution': basis}).json()
    assert out['affected_events'] == ['EA1']
    assert float(out['pending_kwh']) > 0
    assert out['balanced'] is True


def test_loss_page_shows_settlement(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={'dataset': 'clean'}).json()['manifest']['run_id']
    html = c.get('/loss?run_id=' + rid).text
    assert '可申报电量' in html
    assert '电网支撑' in html
    assert '16.6667' in html
