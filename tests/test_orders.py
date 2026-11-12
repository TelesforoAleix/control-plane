"""Contract tests for queued owner orders (all data here is synthetic)."""
import json
import subprocess
import io
from urllib.error import HTTPError
from urllib.request import Request
from unittest.mock import Mock

import pytest

from cplane import state
from conftest import attention


@pytest.fixture
def endpoint(monkeypatch):
    class Endpoint:
        orders = []
        acks = []
        status = 200
    handler = Endpoint()
    url = 'http://orders.invalid'
    class Response(io.BytesIO):
        status = 200
        def getcode(self):
            return self.status
    def urlopen(request, timeout=None):
        target = request.full_url if isinstance(request, Request) else request
        if handler.status != 200:
            raise HTTPError(target, handler.status, 'unavailable', {}, None)
        if isinstance(request, Request) and request.get_method() == 'POST':
            handler.acks.append((target.removeprefix(url), json.loads(request.data)))
            response = Response()
            response.status = 204
            return response
        return Response(json.dumps({'orders': handler.orders}).encode())
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    return handler, url


def order(item, action='pause', argument=None, id='order-1'):
    a = item['attention']
    return {'id': id, 'created': '2026-10-15T12:00:00+02:00', 'action': action,
            'slice': item['id'], 'argument': argument,
            'expect': {'state': item['state'], 'paused': item['paused'],
                       'attention': {'kind': a['kind'], 'since': a['since']} if a else None}}


def test_orders_transport_and_config(tmp_path, monkeypatch, endpoint):
    from cplane.orders import HttpClient, orders_url
    handler, url = endpoint
    runs = tmp_path / 'runs'
    monkeypatch.delenv('CPLANE_ORDERS_URL', raising=False)
    assert orders_url(runs) is None
    (tmp_path / 'orders-url').write_text(url + '\n')
    assert orders_url(runs) == url
    monkeypatch.setenv('CPLANE_ORDERS_URL', url + '/override')
    assert orders_url(runs) == url + '/override'
    handler.orders = [{'id': 'abc'}]
    client = HttpClient(url, timeout=1)
    assert client.fetch() == handler.orders
    client.ack('abc', 'done', 'ok')
    assert handler.acks == [('/orders/abc/ack', {'outcome': 'done', 'detail': 'ok'})]


@pytest.mark.parametrize('action,arg,kind', [
    ('start', None, None), ('slot', '2026-10-19T03:00:00+02:00', None),
    ('pause', None, None), ('resume', None, None),
    ('answer', 'owner response', 'question'), ('approve', None, 'merge'),
])
def test_orders_dispatch_commands(tmp_path, make_state, slice_data, action, arg, kind):
    from cplane.orders import collect
    slice_data['paused'] = action == 'resume'
    slice_data['attention'] = attention(kind) if kind else None
    slice_data['stop_before_merge'] = False
    path = make_state()
    client = Mock()
    client.fetch.return_value = [order(slice_data, action, arg)]
    collect(path, tmp_path / 'runs', client)
    result = state.load(path)['slices'][0]
    assert result['log'][-1]['actor'] == 'frontline'
    assert result['log'][-1]['event'] == action
    if action in ('slot', 'start'):
        assert result['slot'] and (action != 'slot' or result['slot'] == arg)
    if action in ('pause', 'resume'):
        assert result['paused'] is (action == 'pause')
    if action == 'answer':
        assert result['attention']['answer'] == arg
    if action == 'approve':
        assert result['attention']['approved'] is True
    assert client.ack.call_args.args[:2] == ('order-1', 'done')


def test_orders_stale_and_unreviewed(tmp_path, make_state, slice_data):
    from cplane.orders import collect
    slice_data['attention'] = attention('merge')
    path = make_state()
    stale = order(slice_data, 'pause', id='stale')
    stale['expect']['attention']['since'] = '2020-01-01T00:00:00+00:00'
    client = Mock()
    client.fetch.return_value = [stale, order(slice_data, 'approve', id='unreviewed')]
    collect(path, tmp_path / 'runs', client)
    calls = client.ack.call_args_list
    assert calls[0].args[0:2] == ('stale', 'dropped')
    assert calls[0].args[2]
    assert calls[1].args[0:2] == ('unreviewed', 'failed')
    assert 'review' in calls[1].args[2]
    assert not state.load(path)['slices'][0]['attention']['approved']
    notices = [json.loads(line) for line in path.with_name(path.name + '.outbox.jsonl').read_text().splitlines()]
    assert any(n['text'].startswith('Dropped order pause voice/W1: ') for n in notices)


def test_orders_replay_and_bad_fields(tmp_path, make_state, slice_data, capsys):
    from cplane.orders import collect
    path = make_state()
    client = Mock()
    good = order(slice_data)
    client.fetch.return_value = [good]
    client.ack.side_effect = [TimeoutError(), None]
    collect(path, tmp_path / 'runs', client)
    collect(path, tmp_path / 'runs', client)
    assert [c.args[:2] for c in client.ack.call_args_list] == [('order-1', 'done')] * 2
    assert len(state.load(path)['slices'][0]['log']) == 1
    bad = [order(slice_data, 'unknown', id='unknown'),
           order(slice_data, 'answer', 'x' * 4001, id='long'),
           {'id': 'malformed'}, {'action': 'pause'}]
    client.reset_mock(side_effect=True)
    client.fetch.return_value = bad
    collect(path, tmp_path / 'runs', client)
    assert [(c.args[0], c.args[1]) for c in client.ack.call_args_list] == [
        ('unknown', 'failed'), ('long', 'failed'), ('malformed', 'failed')]
    assert capsys.readouterr().err


def test_orders_orchestrator_tmux_is_shell_safe(tmp_path, make_state, slice_data):
    from cplane.orders import open_orchestrator
    path = make_state()
    repos = tmp_path / 'repos'
    (repos / 'repo').mkdir(parents=True)
    task = 'do $(touch NO) `echo NO` "quoted"\nand more'
    run = Mock(side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0),
                            subprocess.CompletedProcess([], 0, stdout=''), subprocess.CompletedProcess([], 0)])
    open_orchestrator(path, repos, 'example/repo', task, run=run)
    calls = run.call_args_list
    assert all(isinstance(c.args[0], list) and not c.kwargs.get('shell', False) for c in calls)
    argv = calls[-1].args[0]
    assert argv[0:2] == ['tmux', 'new-window']
    assert 'frontline' in ' '.join(argv) and 'example/repo' in argv
    assert str(repos / 'repo') in argv
    assert 'claude' in argv
    prompts = [x for x in argv if task in x]
    assert len(prompts) == 1 and 'attention' in prompts[0].lower()
    with pytest.raises(ValueError):
        open_orchestrator(path, repos, '../escape', task, run=run)
    with pytest.raises(ValueError):
        open_orchestrator(path, repos, 'development', 'x' * 4001, run=run)


def test_orders_orchestrator_existing_window(tmp_path, make_state):
    from cplane.orders import open_orchestrator
    path = make_state()
    run = Mock(side_effect=[subprocess.CompletedProcess([], 0),
                            subprocess.CompletedProcess([], 0, stdout='development\n')])
    with pytest.raises(ValueError, match='already open'):
        open_orchestrator(path, tmp_path, 'development', 'task', run=run)
    assert run.call_count == 2


def test_orders_runner_collects_before_scheduling(tmp_path, make_state, slice_data, monkeypatch, endpoint):
    from datetime import datetime, timezone
    from cplane.runner import Runner
    handler, url = endpoint
    path = make_state()
    (tmp_path / 'briefs').mkdir()
    (tmp_path / 'briefs/W1.md').write_text('synthetic brief')
    (tmp_path / 'repos/repo').mkdir(parents=True)
    handler.orders = [order(slice_data, 'start')]
    monkeypatch.setenv('CPLANE_ORDERS_URL', url)
    processes = Mock()
    processes.start.return_value = 123
    runner = Runner(path, processes=processes, github=Mock(), git=Mock(),
                    clock=lambda: datetime.now(timezone.utc), codex_reader=lambda: 0,
                    claude_reader=lambda: None, runs=tmp_path / 'runs', repos=tmp_path / 'repos')
    runner.run()
    assert handler.acks[0][1]['outcome'] == 'done'
    assert processes.start.call_count == 1
    assert any(e['actor'] == 'frontline' and e['event'] == 'start'
               for e in state.load(path)['slices'][0]['log'])


@pytest.mark.parametrize('mode', ['missing', 'unavailable', 'timeout'])
def test_orders_runner_network_failure_continues(tmp_path, make_state, slice_data, monkeypatch, endpoint, capsys, mode):
    from cplane.runner import Runner
    handler, url = endpoint
    path = make_state()
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('CPLANE_ORDERS_URL', raising=False)
    if mode == 'unavailable':
        handler.status = 503
        monkeypatch.setenv('CPLANE_ORDERS_URL', url)
    elif mode == 'timeout':
        from cplane.orders import HttpClient
        monkeypatch.setenv('CPLANE_ORDERS_URL', url)
        monkeypatch.setattr(HttpClient, 'fetch', Mock(side_effect=TimeoutError('timed out')))
    git = Mock()
    runner = Runner(path, processes=Mock(), github=Mock(), git=git,
                    runs=tmp_path / 'runs', repos=tmp_path / 'repos')
    runner.run()
    git.pull.assert_called_once()
    output = capsys.readouterr().err
    assert bool(output) is (mode != 'missing')
    if output:
        assert len(output.splitlines()) == 1


def test_orders_never_log_argument(tmp_path, make_state, slice_data, capsys):
    from cplane.orders import collect
    secret = 'private task $(unsafe)'
    slice_data['attention'] = attention('question')
    path = make_state()
    client = Mock()
    client.fetch.return_value = [order(slice_data, 'answer', secret)]
    collect(path, tmp_path / 'runs', client)
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert secret not in json.dumps(state.load(path)['slices'][0]['log'])


def test_orders_orchestrator_dispatch(tmp_path, make_state, slice_data):
    from cplane.orders import collect
    path = make_state()
    client = Mock()
    client.fetch.return_value = [dict(order(slice_data, 'orchestrator', 'do work'),
                                      slice='development', expect=None)]
    launcher = Mock()
    collect(path, tmp_path / 'runs', client, repos=tmp_path / 'repos', launcher=launcher)
    launcher.assert_called_once_with(path, tmp_path / 'repos', 'development', 'do work')
    assert client.ack.call_args.args[:2] == ('order-1', 'done')


def test_orders_launchd_captures_url():
    from cplane.agent import render
    env = render('/tmp/state.json', environment={'CPLANE_ORDERS_URL': 'http://orders.invalid'})['EnvironmentVariables']
    assert env['CPLANE_ORDERS_URL'] == 'http://orders.invalid'
