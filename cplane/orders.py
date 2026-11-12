"""HTTP transport and processing for queued owner orders."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request
from urllib.parse import quote
from urllib.request import Request

from . import state
from .times import now

ENV_VAR = 'CPLANE_ORDERS_URL'
URL_FILE = 'orders-url'
DEFAULT_TIMEOUT = 10
ANSWER_LIMIT = 4000
TASK_LIMIT = 4000
RESULTS_DIR = 'order-results'
FRONTLINE_SESSION = 'frontline'

# Supported owner actions, including tmux orchestrator dispatch.
ACTIONS = ("slot", "unslot", "start", "pause", "resume", "approve", "answer",
           "orchestrator")


class Dropped(Exception):
    """An order whose expectations no longer match the scheduler state."""


def orders_url(runs):
    """Return the configured orders service URL, or None when unset."""
    from_env = os.environ.get(ENV_VAR)
    if from_env is not None and from_env.strip():
        return from_env.strip()
    path = Path(runs).parent / URL_FILE
    try:
        from_file = path.read_text().strip()
    except OSError:
        return None
    return from_file or None


class HttpClient:
    """Minimal JSON client for the orders service."""

    def __init__(self, base, timeout=DEFAULT_TIMEOUT):
        self.base = base.rstrip('/')
        self.timeout = timeout

    def fetch(self):
        request = Request(self.base + '/orders')
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode())
        return payload['orders']

    def ack(self, id, outcome, detail):
        segment = quote(str(id), safe='')
        body = json.dumps({'outcome': outcome, 'detail': detail}).encode()
        request = Request(self.base + '/orders/' + segment + '/ack',
                          data=body, method='POST',
                          headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(request, timeout=self.timeout)


def _result_path(runs, order_id):
    name = quote(str(order_id), safe='') + '.json'
    return Path(runs) / RESULTS_DIR / name


def _stored_result(runs, order_id):
    try:
        return json.loads(_result_path(runs, order_id).read_text())
    except (OSError, ValueError):
        return None


def _store_result(runs, order_id, outcome, detail):
    """Persist the outcome before acknowledgement so a replay never reapplies it."""
    target = _result_path(runs, order_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({'id': order_id, 'outcome': outcome, 'detail': detail},
                         ensure_ascii=False) + '\n'
    temp = target.with_name(target.name + '.tmp')
    with temp.open('w') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, target)


def _fail(problem):
    """Validate an order's required fields, action, expectation and answer limit."""
    if not isinstance(problem, dict):
        return 'order is not an object'
    action = problem.get('action')
    if not isinstance(action, str) or not action:
        return 'missing action'
    if action not in ACTIONS:
        return f'unsupported action: {action}'
    if not isinstance(problem.get('slice'), str) or not problem['slice']:
        return 'missing slice'
    if 'expect' not in problem:
        return 'missing expect'
    expect = problem['expect']
    if expect is not None:
        if not isinstance(expect, dict):
            return 'invalid expect'
        if not isinstance(expect.get('state'), str) or not isinstance(expect.get('paused'), bool):
            return 'invalid expect'
        attention = expect.get('attention')
        if attention is not None:
            if (not isinstance(attention, dict)
                    or not isinstance(attention.get('kind'), str)
                    or not isinstance(attention.get('since'), str)):
                return 'invalid expect'
    if action == 'orchestrator':
        if expect is not None:
            return 'orchestrator requires a null expect'
        argument = problem.get('argument')
        if not isinstance(argument, str):
            return 'orchestrator requires a task'
        if len(argument) > TASK_LIMIT:
            return f'orchestrator task exceeds {TASK_LIMIT} characters'
        return None
    if action == 'answer':
        argument = problem.get('argument')
        if not isinstance(argument, str):
            return 'answer requires text'
        if len(argument) > ANSWER_LIMIT:
            return f'answer exceeds {ANSWER_LIMIT} characters'
    return None


def _mismatch(item, slice_id, expect):
    """Explain why the current slice disagrees with an order expectation."""
    if item is None:
        return f'unknown slice {slice_id}'
    if expect is None:
        return None
    if item['state'] != expect.get('state'):
        return f"state is {item['state']}, expected {expect.get('state')}"
    if item['paused'] != expect.get('paused'):
        return f"paused is {item['paused']}, expected {expect.get('paused')}"
    actual = item['attention']
    wanted = expect.get('attention')
    if wanted is None:
        if actual is not None:
            return f"attention is {actual['kind']}, expected none"
        return None
    if actual is None:
        return f"attention is none, expected {wanted.get('kind')}"
    if actual['kind'] != wanted.get('kind'):
        return f"attention is {actual['kind']}, expected {wanted.get('kind')}"
    if actual['since'] != wanted.get('since'):
        return f"attention since is {actual['since']}, expected {wanted.get('since')}"
    return None


def _notify(path, order, detail):
    """Append a drop notice to the state outbox without leaking argument text."""
    notice = {'at': now().isoformat(), 'id': order['slice'], 'event': order['action'],
              'text': f"Dropped order {order['action']} {order['slice']}: {detail}"}
    outbox = Path(str(path) + '.outbox.jsonl')
    with outbox.open('a') as stream:
        stream.write(json.dumps(notice, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _apply(path, order):
    """Run one matched order through the locked state/outbox mutation path."""
    from .cli import change_for

    def guard(item, data):
        detail = _mismatch(item, order['slice'], order.get('expect'))
        if detail is not None:
            _notify(path, order, detail)
            raise Dropped(detail)

    try:
        state.update(path, order['slice'], order['action'], 'frontline',
                     change_for(order['action'], order.get('argument')), guard=guard)
    except Dropped as error:
        return 'dropped', _redact(str(error), order.get('argument'))
    except state.StateError as error:
        return 'failed', _redact(' '.join(str(error).splitlines()), order.get('argument'))
    except Exception as error:
        text = ' '.join(str(error).splitlines()) or type(error).__name__
        return 'failed', _redact(f'{type(error).__name__}: {text}', order.get('argument'))
    return 'done', 'ok'


def _redact(detail, argument):
    """Keep owner argument text out of every diagnostic and stored detail."""
    if isinstance(argument, str) and argument:
        return detail.replace(argument, '<argument>')
    return detail


def _orchestrator_folder(path, repos, target):
    """Resolve the target repository folder, rejecting unknown targets."""
    if not isinstance(target, str) or not target:
        raise ValueError('orchestrator target must name a repository')
    path = Path(path)
    if target == 'development':
        return path.parent
    data = state.load(path)
    names = {item.get('repo') for item in data.get('slices', [])}
    if target in names:
        name = target.split('/')[-1]
        folder = Path(repos) / name
        if name not in ('', '.', '..') and folder.is_dir():
            return folder
    raise ValueError(f'orchestrator target is not a repository: {target}')


def open_orchestrator(path, repos, target, task, *, run=subprocess.run):
    """Open (or reuse) a frontline tmux window running claude for a target."""
    if not isinstance(task, str):
        raise ValueError('orchestrator task must be text')
    if len(task) > TASK_LIMIT:
        raise ValueError(f'orchestrator task exceeds {TASK_LIMIT} characters')
    folder = _orchestrator_folder(path, repos, target)
    probe = run(['tmux', 'has-session', '-t', FRONTLINE_SESSION])
    if probe.returncode != 0:
        run(['tmux', 'new-session', '-d', '-s', FRONTLINE_SESSION])
    listed = run(['tmux', 'list-windows', '-t', FRONTLINE_SESSION, '-F',
                  '#{window_name}'])
    windows = (listed.stdout or '').splitlines()
    if target in windows:
        raise ValueError(f'orchestrator window for {target} is already open')
    prompt = (f'You are the orchestrator for {target}. Do only this task.\n\n'
              f'{task}\n\n'
              'Put any questions for the owner on the concerned slice as '
              'cplane attention.')
    run(['tmux', 'new-window', '-t', FRONTLINE_SESSION, '-n', target,
         '-c', str(folder), 'claude', prompt])


def _dispatch(path, order, repos, launcher):
    """Run one orchestrator order without ever invoking a shell."""
    try:
        launcher(Path(path), Path(repos) if repos is not None else None,
                 order['slice'], order['argument'])
    except Exception as error:
        text = ' '.join(str(error).splitlines()) or type(error).__name__
        return 'failed', _redact(f'{type(error).__name__}: {text}',
                                 order.get('argument'))
    return 'done', 'ok'


def _acknowledge(client, order_id, outcome, detail):
    try:
        client.ack(order_id, outcome, detail)
    except Exception as error:
        text = ' '.join(str(error).splitlines()) or type(error).__name__
        print(f'orders: acknowledgement failed for {order_id}: {text}', file=sys.stderr)


def collect(path, runs, client, *, repos=None, launcher=None):
    """Fetch owner orders and apply each against the scheduler once."""
    if launcher is None:
        launcher = open_orchestrator
    path = Path(path)
    orders = client.fetch()
    for order in orders:
        if not isinstance(order, dict) or not isinstance(order.get('id'), str) or not order['id']:
            print('orders: skipping entry without an id', file=sys.stderr)
            continue
        order_id = order['id']
        stored = _stored_result(runs, order_id)
        if stored is not None:
            _acknowledge(client, order_id, stored.get('outcome'), stored.get('detail'))
            continue
        problem = _fail(order)
        if problem is not None:
            outcome, detail = 'failed', problem
        elif order['action'] == 'orchestrator':
            outcome, detail = _dispatch(path, order, repos, launcher)
        else:
            outcome, detail = _apply(path, order)
        _store_result(runs, order_id, outcome, detail)
        _acknowledge(client, order_id, outcome, detail)
