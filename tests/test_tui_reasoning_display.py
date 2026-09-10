"""Session reasoning display reaches the existing TUI config hydration contract."""
import copy
import json
from unittest.mock import Mock

import pytest

from hermes_state import SessionDB
from tui_gateway import server


def rpc(method, **params):
    return server.handle_request({'id': 'display', 'method': method, 'params': params})


@pytest.fixture
def display_runtime(monkeypatch, tmp_path):
    config = {'model': 'test-model', 'display': {
        'show_reasoning': True, 'sections': {'thinking': 'expanded', 'tools': 'collapsed'},
    }}
    db = SessionDB(db_path=tmp_path / 'state.db')
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_load_cfg', lambda: config)
    monkeypatch.setattr(server, '_save_cfg', Mock(side_effect=AssertionError('global write')))
    monkeypatch.setattr(server, '_write_config_key', Mock(side_effect=AssertionError('global write')))
    monkeypatch.setattr(server, '_sessions', {})
    monkeypatch.setattr(server, '_profile_home', lambda *a: None)
    monkeypatch.setattr(server, '_claim_active_session_slot', Mock(return_value=(None, None)))
    for name in ('_enable_gateway_prompts', '_schedule_agent_build', '_schedule_session_cap_enforcement',
                 '_register_managed_runtime', '_register_session_cwd'):
        monkeypatch.setattr(server, name, Mock())
    for sid in ('a', 'b'):
        db.create_session(sid, source='tui', model='test-model')
        server._sessions[sid] = {'session_key': sid, 'agent': None, 'running': False}
    yield db, config
    db.close()


@pytest.mark.parametrize('command,mode,visible', [
    ('full', 'expanded', False), ('all', 'expanded', False),
    ('clamp', 'collapsed', False), ('collapse', 'collapsed', False), ('short', 'collapsed', False),
    ('show', 'expanded', True), ('on', 'expanded', True),
    ('hide', 'hidden', False), ('off', 'hidden', False),
])
def test_reasoning_display_round_trip_to_effective_config(display_runtime, command, mode, visible):
    db, config = display_runtime
    config['display']['sections']['thinking'] = 'collapsed' if mode == 'expanded' else 'expanded'
    config['display']['show_reasoning'] = not visible
    before = copy.deepcopy(config)
    # Base ec5cb333: full/clamp change expansion without enabling show_reasoning.
    assert 'result' in rpc('config.set', session_id='a', key='reasoning', value='hide')
    assert 'result' in rpc('config.set', session_id='a', key='reasoning', value=command)
    effective = rpc('config.get', session_id='a', key='full')['result']['config']['display']
    assert effective['sections']['thinking'] == mode
    assert effective['show_reasoning'] is visible
    assert effective['sections']['tools'] == 'collapsed'
    assert rpc('config.get', session_id='b', key='full')['result']['config'] == before
    assert rpc('config.get', key='full')['result']['config'] == before
    assert config == before

    if command in {'full', 'all', 'clamp', 'collapse', 'short'}:
        assert effective['reasoning_full'] is (mode == 'expanded')

    # A real DB row and real cold session.resume, not a dict-only restore test.
    server._sessions.clear()
    resumed = rpc('session.resume', session_id='a')['result']['session_id']
    restored = rpc('config.get', session_id=resumed, key='full')['result']['config']['display']
    assert restored == effective
    assert rpc('config.get', session_id=resumed, key='reasoning')['result']['display'] == ('show' if visible else 'hide')
    assert server._sessions[resumed]['agent'] is None


def test_draft_display_persists_only_on_first_activity(display_runtime):
    db, _ = display_runtime
    sid = rpc('session.create', model='test-model')['result']['session_id']
    session = server._sessions[sid]
    key = session['session_key']
    assert 'result' in rpc('config.set', session_id=sid, key='reasoning', value='clamp')
    assert db.get_session(key) is None, 'display-only changes must not create abandoned drafts'
    server._ensure_session_db_row(session)
    server._sessions.clear()
    resumed = rpc('session.resume', session_id=key)['result']['session_id']
    display = rpc('config.get', session_id=resumed, key='full')['result']['config']['display']
    assert display['sections']['thinking'] == 'collapsed'
    assert display['show_reasoning'] is True


@pytest.mark.parametrize('command', ['full', 'clamp'])
def test_expansion_preserves_visible_display(display_runtime, command):
    rpc('config.set', session_id='a', key='reasoning', value=command)
    display = rpc('config.get', session_id='a', key='full')['result']['config']['display']
    assert display['show_reasoning'] is True


@pytest.mark.parametrize('command', ['full', 'clamp', 'show', 'hide'])
def test_busy_display_change_is_rejected(display_runtime, command):
    db, _ = display_runtime
    before = db.get_session('a')['model_config']
    server._sessions['a']['running'] = True
    assert rpc('config.set', session_id='a', key='reasoning', value=command)['error']['code'] == 4009
    assert db.get_session('a')['model_config'] == before
