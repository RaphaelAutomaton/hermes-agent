"""Workbench session option controls must not mutate other chats or defaults."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tui_gateway import server

_REAL_PERSIST = server._persist_live_session_runtime
_REAL_INFO = server._session_info


@pytest.fixture
def runtime(monkeypatch):
    a = SimpleNamespace(model='gpt-6-astra', provider='openai-codex', service_tier=None,
                        request_overrides={}, reasoning_config={'enabled': True, 'effort': 'high'})
    b = SimpleNamespace(model='gpt-6-astra', provider='openai-codex', service_tier=None,
                        request_overrides={}, reasoning_config={'enabled': True, 'effort': 'low'})
    sessions = {'a': {'agent': a, 'running': False}, 'b': {'agent': b, 'running': False}}
    monkeypatch.setattr(server, '_sessions', sessions)
    monkeypatch.setattr(server, '_emit', Mock())
    monkeypatch.setattr(server, '_session_info', lambda agent, session=None: {'model': agent.model})
    monkeypatch.setattr(server, '_persist_live_session_runtime', Mock())
    monkeypatch.setattr(server, '_load_cfg', lambda: {'agent': {'reasoning_effort': 'medium', 'service_tier': 'normal'}})
    monkeypatch.setattr(server, '_load_service_tier', lambda: None)
    write = Mock()
    monkeypatch.setattr(server, '_write_config_key', write)
    return sessions, write


@pytest.fixture
def drafts(runtime, monkeypatch):
    for name in ('_enable_gateway_prompts', '_schedule_agent_build',
                 '_schedule_session_cap_enforcement', '_register_managed_runtime',
                 '_register_session_cwd'):
        monkeypatch.setattr(server, name, Mock())
    monkeypatch.setattr(server, '_claim_active_session_slot', Mock(return_value=(None, None)))
    monkeypatch.setattr(server, '_profile_home', lambda *a: None)
    monkeypatch.setattr(server, '_get_db', lambda: None)
    return runtime


@pytest.mark.parametrize('fast,expected', [(False, 'normal'), (True, 'priority')])
def test_create_explicit_fast_pins_tier(drafts, monkeypatch, fast, expected):
    sessions, write = drafts
    monkeypatch.setattr(server, '_load_service_tier', lambda: 'priority')
    result = rpc('session.create', model='gpt-6-astra', fast=fast)['result']
    assert sessions[result['session_id']]['create_service_tier_override'] == expected
    write.assert_not_called()


@pytest.mark.parametrize('fast', ['false', 'true', 0, 1, None, [], {}])
def test_create_rejects_non_boolean_fast_without_allocating(drafts, fast):
    sessions, write = drafts
    response = rpc('session.create', model='gpt-6-astra', fast=fast)
    assert response['error']['code'] == 4002
    assert set(sessions) == {'a', 'b'}
    server._claim_active_session_slot.assert_not_called()
    server._schedule_agent_build.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize('model', ['unsupported-model', None])
def test_create_fast_uses_native_capability_check(drafts, monkeypatch, model):
    sessions, write = drafts
    monkeypatch.setattr(server, '_resolve_model', lambda: '')
    response = rpc('session.create', model=model, fast=True)
    assert response['error']['code'] == 4002
    assert set(sessions) == {'a', 'b'}
    server._schedule_agent_build.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize('effort', ['bogus', 'show', True, 0, 1, [], {}])
def test_create_rejects_invalid_reasoning_before_allocation(drafts, effort):
    sessions, write = drafts
    response = rpc('session.create', reasoning_effort=effort)
    assert response['error']['code'] == 4002
    assert set(sessions) == {'a', 'b'}
    server._claim_active_session_slot.assert_not_called()
    server._schedule_agent_build.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize('effort', ['none', 'false', 'disabled', False, ' HIGH ', 'max', '', None])
def test_create_reasoning_matches_native_parser(drafts, effort):
    from hermes_constants import parse_reasoning_effort
    sessions, _ = drafts
    sid = rpc('session.create', reasoning_effort=effort)['result']['session_id']
    assert sessions[sid]['create_reasoning_override'] == parse_reasoning_effort(effort)


def rpc(method, **params):
    return server.handle_request({'id': 'option', 'method': method, 'params': params})


def test_fast_is_session_scoped(runtime):
    sessions, write = runtime
    result = rpc('config.set', session_id='a', key='fast', value='fast', scope='session')
    assert result['result']['value'] == 'fast'
    write.assert_not_called()
    assert sessions['a']['agent'].service_tier == 'priority'
    assert sessions['b']['agent'].service_tier is None
    assert sessions['a']['create_service_tier_override'] == 'priority'
    assert rpc('config.set', session_id='a', key='fast', value='normal', scope='session')['result']['value'] == 'normal'
    assert sessions['a']['create_service_tier_override'] == 'normal'
    assert sessions['a']['agent'].request_overrides == {}


@pytest.mark.parametrize('key,value', [('fast', 'fast'), ('reasoning', 'high')])
def test_missing_session_never_falls_back_to_global(runtime, key, value):
    _, write = runtime
    response = rpc('config.set', session_id='expired', key=key, value=value, scope='session')
    assert response['error']['code'] == 4007
    write.assert_not_called()


@pytest.mark.parametrize('key,value', [('fast', 'fast'), ('reasoning', 'high')])
def test_busy_session_options_are_not_mutated(runtime, key, value):
    sessions, write = runtime
    sessions['a']['running'] = True
    response = rpc('config.set', session_id='a', key=key, value=value, scope='session')
    assert response['error']['code'] == 4009
    write.assert_not_called()


def test_lazy_fast_uses_session_model_and_normal_does_not_follow_global(runtime, monkeypatch):
    sessions, write = runtime
    sessions['a']['agent'] = None
    sessions['a']['model_override'] = {'model': 'gpt-6-astra', 'provider': 'openai-codex'}
    monkeypatch.setattr(server, '_resolve_model', lambda: 'unsupported-global')
    response = rpc('config.set', session_id='a', key='fast', value='fast', scope='session')
    assert response['result']['value'] == 'fast'
    assert rpc('config.get', session_id='a', key='fast')['result']['value'] == 'fast'
    rpc('config.set', session_id='a', key='fast', value='normal', scope='session')
    monkeypatch.setattr(server, '_load_service_tier', lambda: 'priority')
    assert rpc('config.get', session_id='a', key='fast')['result']['value'] == 'normal'
    assert rpc('config.get', session_id='b', key='fast')['result']['value'] == 'normal'
    write.assert_not_called()


def test_lazy_fast_status_and_toggle_read_local_override(runtime):
    sessions, write = runtime
    sessions['a'].update(agent=None, model_override={'model': 'gpt-6-astra'},
                         create_service_tier_override='priority')
    assert rpc('config.set', session_id='a', key='fast', value='status')['result']['value'] == 'fast'
    assert rpc('config.set', session_id='a', key='fast', value='toggle')['result']['value'] == 'normal'
    write.assert_not_called()


@pytest.mark.parametrize('value', ['show', 'hide', 'on', 'off', 'full', 'all', 'clamp', 'collapse', 'short'])
def test_session_reasoning_display_never_saves_global(runtime, monkeypatch, value):
    _, write = runtime
    save = Mock()
    monkeypatch.setattr(server, '_save_cfg', save)
    assert 'result' in rpc('config.set', session_id='a', key='reasoning', value=value)
    save.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize('key,value', [('fast', 'fast'), ('reasoning', 'high')])
def test_initializing_session_rejects_changes(runtime, key, value):
    sessions, write = runtime
    sessions['a'].update(agent=None, agent_build_started=True)
    assert rpc('config.set', session_id='a', key=key, value=value)['error']['code'] == 4009
    write.assert_not_called()


def test_lazy_metadata_reports_requested_not_effective(drafts, monkeypatch):
    sessions, _ = drafts
    result = rpc('session.create', model='gpt-6-astra', provider='openai-codex',
                 reasoning_effort='none', fast=False)['result']
    sid = result['session_id']
    status = rpc('session.status', session_id=sid)['result']
    for info in (result['info'], status):
        assert info['reasoning_effort'] == 'none'
        assert info['fast'] is False
        assert info['model_controls']['effective'] is None
        assert info['model_controls']['requested']['model'] == 'gpt-6-astra'
    assert rpc('config.get', session_id=sid, key='reasoning')['result']['value'] == 'none'


def test_live_session_info_matches_status_controls(runtime, monkeypatch):
    sessions, _ = runtime
    monkeypatch.setattr(server, '_session_info', _REAL_INFO)
    monkeypatch.setattr(server, '_get_db', lambda: None)
    session = sessions['a']
    session['create_service_tier_override'] = 'normal'
    session['create_reasoning_override'] = {'enabled': False}
    info = server._session_info(session['agent'], session)
    status = rpc('session.status', session_id='a')['result']
    assert info['model_controls'] == status['model_controls']
    assert info['model_controls']['requested']['reasoning_effort'] == 'none'
    assert info['model_controls']['effective']['reasoning_effort'] == 'high'
    for key, value in info['model_controls']['effective'].items():
        assert info[key] == value


def test_live_unset_reasoning_does_not_fall_back_to_global(runtime):
    sessions, _ = runtime
    sessions['a']['agent'].reasoning_config = None
    assert rpc('config.get', session_id='a', key='reasoning')['result']['value'] == ''
    rpc('config.set', session_id='a', key='reasoning', value='hide')
    assert rpc('config.get', session_id='a', key='reasoning')['result']['display'] == 'hide'


@pytest.mark.parametrize('key', ['fast', 'reasoning'])
def test_get_explicit_session_scope_requires_session(runtime, key):
    assert rpc('config.get', key=key, scope='session')['error']['code'] == 4007


@pytest.mark.parametrize('lazy', [False, True])
def test_session_options_persist_and_restore_without_global(runtime, monkeypatch, tmp_path, lazy):
    from hermes_state import SessionDB
    sessions, write = runtime
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('stored', source='tui', model='gpt-6-astra',
                          model_config={'provider': 'openai-codex', 'service_tier': 'priority'})
        session = sessions['a']
        session.update(session_key='stored', model_override={'model': 'gpt-6-astra', 'provider': 'openai-codex'})
        if lazy:
            session['agent'] = None
        monkeypatch.setattr(server, '_get_db', lambda: db)
        monkeypatch.setattr(server, '_persist_live_session_runtime', _REAL_PERSIST)
        assert 'result' in rpc('config.set', session_id='a', key='fast', value='normal')
        assert 'result' in rpc('config.set', session_id='a', key='reasoning', value='none')
        restored = server._stored_session_runtime_overrides(db.get_session('stored'))
        assert restored['service_tier_override'] == 'normal'
        assert restored['reasoning_config_override'] == {'enabled': False}
        write.assert_not_called()
    finally:
        db.close()


def test_resumed_lazy_changes_reach_build(drafts, monkeypatch, tmp_path):
    from hermes_state import SessionDB
    sessions, _ = drafts
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('stored', source='tui', model='gpt-6-astra', model_config={
            'provider': 'openai-codex', 'reasoning_config': {'enabled': True, 'effort': 'low'},
            'service_tier': 'priority'})
        monkeypatch.setattr(server, '_get_db', lambda: db)
        response = rpc('session.resume', session_id='stored')['result']
        sid = response['session_id']
        assert response['info']['reasoning_effort'] == 'low'
        assert response['info']['fast'] is True
        assert response['info']['model_controls']['effective'] is None
        assert rpc('config.get', session_id=sid, key='reasoning')['result']['value'] == 'low'
        assert 'result' in rpc('config.set', session_id=sid, key='fast', value='normal')
        assert 'result' in rpc('config.set', session_id=sid, key='reasoning', value='high')
        captured = {}
        def make_agent(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(model='gpt-6-astra')
        monkeypatch.setattr(server, '_make_agent', make_agent)
        monkeypatch.setattr(server, '_SlashWorker', Mock())
        for name in ('_attach_worker', '_wire_callbacks', '_start_notification_poller',
                     '_notify_session_boundary', '_probe_config_health'):
            monkeypatch.setattr(server, name, Mock())
        from tui_gateway.session_runtime import SessionRuntimeManager
        manager = SessionRuntimeManager()
        manager.register(sid, sessions[sid])
        monkeypatch.setattr(server, '_runtime_manager', manager)
        server._start_agent_build(sid, sessions[sid])
        assert sessions[sid]['agent_ready'].wait(5)
        assert captured['service_tier_override'] == 'normal'
        assert captured['reasoning_config_override']['effort'] == 'high'
    finally:
        db.close()


@pytest.mark.parametrize('tier,expected', [(None, 'normal'), ('priority', 'priority')])
def test_reset_preserves_live_tier(runtime, monkeypatch, tier, expected):
    import threading
    sessions, write = runtime
    session = sessions['a']
    session.update(session_key='stored', history_lock=threading.Lock(), history=[])
    session['agent'].service_tier = tier
    monkeypatch.setattr(server, '_load_service_tier', lambda: 'priority')
    monkeypatch.setattr(server, '_restart_slash_worker', Mock())
    make = Mock(return_value=SimpleNamespace(model='gpt-6-astra'))
    monkeypatch.setattr(server, '_make_agent', make)
    server._reset_session_agent('a', session)
    assert make.call_args.kwargs['service_tier_override'] == expected
    write.assert_not_called()


def test_branch_inherits_runtime_controls_not_profile(drafts, monkeypatch, tmp_path):
    import threading
    from hermes_state import SessionDB
    sessions, _ = drafts
    parent = sessions['a']
    parent.update(session_key='parent', history=[{'role': 'user', 'content': 'hello'}],
                  history_lock=threading.Lock())
    db = SessionDB(db_path=tmp_path / 'state.db')
    try:
        db.create_session('parent', source='tui', model='gpt-6-astra')
        monkeypatch.setattr(server, '_get_db', lambda: db)
        monkeypatch.setattr(server, '_sess', lambda *a: (parent, None))
        monkeypatch.setattr(server, '_resolve_model', lambda: 'unrelated-default')
        make = Mock(return_value=SimpleNamespace(model='gpt-6-astra'))
        monkeypatch.setattr(server, '_make_agent', make)
        monkeypatch.setattr(server, '_init_session', Mock())
        result = rpc('session.branch', session_id='a', name='child')['result']
        kwargs = make.call_args.kwargs
        assert kwargs['model_override']['model'] == 'gpt-6-astra'
        assert kwargs['provider_override'] == 'openai-codex'
        assert kwargs['reasoning_config_override']['effort'] == 'high'
        assert kwargs['service_tier_override'] == 'normal'
        row = db.get_session(make.call_args.args[1])
        restored = server._stored_session_runtime_overrides(row)
        assert restored['service_tier_override'] == 'normal'
        assert restored['model_override']['model'] == 'gpt-6-astra'
    finally:
        db.close()


@pytest.fixture
def native_agent(runtime, monkeypatch, tmp_path):
    """Keep construction and request builders real; isolate external I/O."""
    from pathlib import Path
    import socket
    import run_agent
    import agent.anthropic_adapter as anthropic
    import hermes_cli.config as config
    import hermes_cli.mcp_startup as mcp
    import tui_gateway.entry as entry

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('no network')))
    monkeypatch.setenv('HERMES_IGNORE_RULES', '1')
    monkeypatch.setattr(config, 'load_config', lambda: {
        'agent': {'environment_probe': False}, 'model': {'context_length': 128000},
    })
    monkeypatch.setattr(run_agent, 'get_tool_definitions', lambda **kw: [])
    monkeypatch.setattr(run_agent, 'check_toolset_requirements', lambda: {})
    monkeypatch.setattr(run_agent, 'OpenAI', Mock())
    monkeypatch.setattr(anthropic, 'build_anthropic_client', Mock())
    monkeypatch.setattr(mcp, 'wait_for_mcp_discovery', Mock())
    monkeypatch.setattr(entry, 'wait_for_mcp_discovery', Mock())
    monkeypatch.setattr(server, '_parse_tui_skills_env', lambda: [])
    monkeypatch.setattr(server, '_load_enabled_toolsets', lambda: [])
    monkeypatch.setattr(server, '_load_provider_routing', lambda: {})
    monkeypatch.setattr(server, '_load_fallback_model', lambda: None)
    monkeypatch.setattr(server, '_get_db', lambda: None)
    monkeypatch.setattr(server, '_restart_slash_worker', Mock())
    monkeypatch.setattr(server, '_load_service_tier', lambda: 'priority')

    def configure(model, provider, api_mode):
        monkeypatch.setattr(server, '_resolve_runtime_with_fallback', lambda *a: {
            'provider': provider, 'api_mode': api_mode,
            'base_url': ('https://api.anthropic.com' if provider == 'anthropic'
                         else 'https://chatgpt.com/backend-api/codex'),
            'api_key': 'test-key',
        })
        return {'model': model, 'provider': provider}

    return configure


@pytest.mark.parametrize('model,provider,api_mode,wire_key,wire_value', [
    ('gpt-6-astra', 'openai-codex', 'codex_responses', 'service_tier', 'priority'),
    ('claude-opus-4-6', 'anthropic', 'anthropic_messages', 'speed', 'fast'),
])
@pytest.mark.parametrize('tier', ['priority', 'normal', None])
def test_native_fast_survives_construction_and_reset(
    native_agent, runtime, model, provider, api_mode, wire_key, wire_value, tier,
):
    import threading
    from run_agent import AIAgent

    model_override = native_agent(model, provider, api_mode)
    agent = server._make_agent('a', 'stored', model_override=model_override,
                               service_tier_override=tier)
    assert isinstance(agent, AIAgent)
    session = runtime[0]['a']
    session.update(agent=agent, model_override=model_override, session_key='stored',
                   history_lock=threading.Lock(), history=[])
    for reset in (False, True):
        if reset:
            server._reset_session_agent('a', session)
            agent = session['agent']
        # Real AIAgent -> build_api_kwargs -> native transport; never send it.
        kwargs = agent._build_api_kwargs([{'role': 'user', 'content': 'offline'}])
        if tier == 'normal':
            assert agent.request_overrides == {}
            assert 'service_tier' not in kwargs
            assert 'speed' not in kwargs
            assert 'speed' not in kwargs.get('extra_body', {})
        else:
            body = kwargs.get('extra_body', {}) if provider == 'anthropic' else kwargs
            assert body.get(wire_key) == wire_value
            assert agent.request_overrides == {wire_key: wire_value}


def test_status_exposes_session_model_options_without_agent_build(runtime, monkeypatch):
    sessions, _ = runtime
    monkeypatch.setattr(server, '_get_db', lambda: None)
    monkeypatch.setattr(server, '_get_usage', lambda agent: {})
    response = rpc('session.status', session_id='a')['result']
    assert response['model'] == 'gpt-6-astra'
    assert response['provider'] == 'openai-codex'
    assert response['reasoning_effort'] == 'high'
    assert response['fast'] is False
    assert response['running'] is False
