"""Explicit /queue must never inherit the ordinary busy-input interrupt policy."""
import threading
from unittest.mock import Mock

import pytest
from tui_gateway import server


def rpc(**params):
    return server.handle_request({'jsonrpc': '2.0', 'id': 'queue-test',
                                  'method': 'session.queue', 'params': params})


def test_explicit_queue_preserves_active_agent_and_merges_pending_text(monkeypatch):
    agent = Mock()
    session = {'running': True, 'agent': agent, 'history_lock': threading.RLock()}
    monkeypatch.setattr(server, '_sessions', {'queue-session': session})
    monkeypatch.setattr(server, '_load_busy_input_mode', lambda: 'interrupt')
    result = rpc(session_id='queue-session', text='first instruction')
    assert result.get('result', {}).get('status') == 'queued'
    assert session['queued_prompt']['text'] == 'first instruction'
    assert rpc(session_id='queue-session', text='second instruction')['result']['status'] == 'queued'
    assert session['queued_prompt']['text'] == 'first instruction\n\nsecond instruction'
    agent.interrupt.assert_not_called()
    agent.steer.assert_not_called()


@pytest.mark.parametrize('text', ['', '   ', None, [], 1])
def test_queue_rejects_invalid_text_before_prompt_submission(monkeypatch, text):
    submit = Mock()
    monkeypatch.setitem(server._methods, 'prompt.submit', submit)
    result = rpc(session_id='queue-session', text=text)
    assert result['error']['code'] == 4002
    submit.assert_not_called()


def test_queue_missing_session_does_not_create_an_agent(monkeypatch):
    monkeypatch.setattr(server, '_sessions', {})
    result = rpc(session_id='missing', text='instruction')
    assert 'error' in result
    assert not server._sessions


@pytest.mark.parametrize('session_params', [
    {}, {'session_id': None}, {'session_id': ''}, {'session_id': '   '},
    {'session_id': []}, {'session_id': [1]},
    {'session_id': {}}, {'session_id': {'bad': 1}},
    {'session_id': 0}, {'session_id': 1}, {'session_id': 1.5},
    {'session_id': False}, {'session_id': True},
])
def test_queue_rejects_invalid_session_id_before_lookup(monkeypatch, session_params):
    lookup = Mock(wraps=server._sess_nowait)
    monkeypatch.setattr(server, '_sess_nowait', lookup)
    monkeypatch.setattr(server, '_sessions', {})
    result = rpc(text='instruction', **session_params)
    assert result['id'] == 'queue-test'
    assert result['error']['code'] == 4003
    lookup.assert_not_called()
    assert not server._sessions


def test_queue_rejects_idle_session_without_submitting_prompt(monkeypatch):
    agent = Mock()
    session = {'running': False, 'agent': agent, 'history_lock': threading.Lock()}
    monkeypatch.setattr(server, '_sessions', {'queue-session': session})
    submit = Mock()
    monkeypatch.setitem(server._methods, 'prompt.submit', submit)
    result = rpc(session_id='queue-session', text='instruction')
    assert result['error']['code'] == 4009
    assert 'queued_prompt' not in session
    assert 'last_active' not in session
    submit.assert_not_called()
    agent.interrupt.assert_not_called()
    agent.steer.assert_not_called()


@pytest.mark.parametrize('length', [199_999, 200_000, 200_001])
def test_queue_text_length_boundary(monkeypatch, length):
    session = {'running': True, 'history_lock': threading.Lock()}
    monkeypatch.setattr(server, '_sessions', {'queue-session': session})
    text = 'x' * length
    result = rpc(session_id='queue-session', text=text)
    if length <= 200_000:
        assert result['result']['status'] == 'queued'
        assert session['queued_prompt']['text'] == text
    else:
        assert result['error']['code'] == 4002
        assert 'queued_prompt' not in session
        assert 'last_active' not in session
