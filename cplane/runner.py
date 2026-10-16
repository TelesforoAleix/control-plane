"""One locked scheduling pass. Adapters keep external effects fakeable."""
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from . import state
from .executors import Processes, atomic_json, CLAUDE_PARAGRAPH
from .times import now


def codex_usage(folder=None):
    paths = Path(folder or Path.home() / '.codex/sessions').rglob('*.jsonl')
    newest = max(paths, key=lambda p: p.stat().st_mtime_ns, default=None)
    if newest:
        # Walk backwards: the last non-null capacity report is the current one.
        for line in reversed(newest.read_text(errors='replace').splitlines()):
            try:
                limits = json.loads(line).get('payload', {}).get('rate_limits')
                if limits and limits.get('primary'):
                    return limits['primary']['used_percent']
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    return None


def claude_usage(path=None):
    try:
        return json.loads(Path(path or Path.home() / '.claude/usage-now.json').read_text())
    except (OSError, ValueError):
        return None


class Git:
    def pull(self, path):
        probe = subprocess.run(['git', '-C', str(path.parent), 'rev-parse', '--show-toplevel'],
                               capture_output=True, text=True)
        if not probe.returncode:
            state.git(Path(probe.stdout.strip()), 'pull', '--rebase')

    def commit(self, path):
        state.commit_state(path, Path(str(path) + '.outbox.jsonl'), 'run', 'pass')


class GitHub:
    def call(self, repo, *args):
        result = subprocess.run(['gh', *args, '--repo', repo], text=True, capture_output=True)
        if result.returncode:
            raise state.StateError('gh: ' + (result.stderr.strip() or result.stdout.strip()))
        return result.stdout.strip()

    def open(self, repo, request):
        # Recover an already-created PR if a previous pass stopped before saving its number.
        existing = json.loads(self.call(repo, 'pr', 'list', '--head', request['branch'], '--state', 'open', '--json', 'number'))
        if existing:
            return existing[0]['number']
        url = self.call(repo, 'pr', 'create', '--head', request['branch'], '--title', request['title'], '--body', request['body'])
        return int(url.rstrip('/').split('/')[-1])

    def checks(self, repo, number):
        info = json.loads(self.call(repo, 'pr', 'view', str(number), '--json', 'statusCheckRollup'))
        pending = False
        for check in info['statusCheckRollup']:
            conclusion = check.get('conclusion') or check.get('state')
            if conclusion in ('FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'):
                return 'red', check.get('name') or check.get('context', 'check')
            if conclusion not in ('SUCCESS', 'NEUTRAL', 'SKIPPED'):
                pending = True
        return ('pending' if pending else 'green'), ''

    def merge(self, repo, number):
        info = json.loads(self.call(repo, 'pr', 'view', str(number), '--json', 'state,mergeCommit'))
        if info['state'] != 'MERGED':
            self.call(repo, 'pr', 'merge', str(number), '--squash')
            info = json.loads(self.call(repo, 'pr', 'view', str(number), '--json', 'state,mergeCommit'))
        return info['mergeCommit']['oid']


class Runner:
    def __init__(self, path, *, processes=None, github=None, git=None, clock=now,
                 codex_reader=codex_usage, claude_reader=claude_usage, runs=None, repos=None):
        self.path = Path(path).expanduser().resolve()
        self.processes = processes or Processes()
        self.github = github or GitHub()
        self.git = git or Git()
        self.clock = clock
        self.codex_reader, self.claude_reader = codex_reader, claude_reader
        base = Path(runs or os.environ.get('CPLANE_RUNS', '~/Library/Application Support/cplane/runs')).expanduser().resolve()
        self.runs = base / hashlib.sha256(str(self.path).encode()).hexdigest()[:20]
        self.repos = Path(repos or os.environ.get('CPLANE_REPOS', '~/Code')).expanduser().resolve()
        self.notices = []

    def log(self, item, event, notice=False):
        item['log'].append({'at': self.at, 'actor': 'runner', 'event': event})
        if notice:
            self.notices.append({'at': self.at, 'id': item['id'], 'event': event.split(':')[0], 'text': event})

    def attention(self, item, kind, body):
        item['attention'] = {'kind': kind, 'body': body, 'since': self.at, 'answer': None, 'approved': False}
        if kind == 'escalation':
            item['state'] = 'escalated'
        self.log(item, f'{kind}: {body}', True)

    def save_job(self, job):
        atomic_json(Path(job['run']) / 'job.json', job)

    def choose(self, item, resume=False):
        model = item['executor']['model']
        if resume:
            tool = item['session']['tool']
            model = self.jobs[item['id']]['model']
        else:
            tool = 'codex'
        if tool == 'codex':
            codex = self.codex_reader()
            if isinstance(codex, (int, float)) and 0 <= codex < 100:
                return 'codex', model
        if resume and tool == 'codex':
            return None
        usage = self.claude_reader()
        if usage:
            try:
                age = self.instant.timestamp() - usage['at']
                used = usage['rate_limits']['five_hour']['used_percentage']
                if 0 <= age < 3600 and 0 <= used < 90:
                    return 'claude', model if resume else ('opus' if 'sol' in model.lower() else 'sonnet')
            except (KeyError, TypeError):
                pass
        return None

    def room(self, item):
        active = [j for j in self.jobs.values() if j['phase'] == 'running']
        # An issued session whose local run files are missing still occupies a slot.
        unknown = [s for s in self.data['slices'] if s['id'] not in self.jobs
                   and s['state'] in ('issued', 'awaiting handover') and not s['attention']]
        return len(active) + len(unknown) < 2 and not any(
            j['repo'] == str(self.repo(item)) for j in active
        ) and not any(self.repo(s) == self.repo(item) for s in unknown)

    def repo(self, item):
        name = item['repo'].split('/')[-1]
        if name in ('', '.', '..') or '\\' in name:
            raise state.StateError('invalid repository name')
        return (self.repos / name).resolve()

    def launch(self, item, prompt, resume=False):
        if not self.room(item):
            self.log(item, 'deferred: executor limit or repository busy')
            return False
        choice = self.choose(item, resume)
        if choice is None:
            self.log(item, 'deferred: capacity unavailable')
            return False
        tool, model = choice
        folder = self.repo(item)
        if not folder.is_dir():
            raise state.StateError(f'missing repository: {folder}')
        run = self.runs / uuid.uuid4().hex
        run.mkdir(parents=True, mode=0o700)
        if tool == 'claude' and not resume:
            prompt += CLAUDE_PARAGRAPH
        job = {'id': item['id'], 'repo': str(folder), 'run': str(run), 'tool': tool,
               'model': model, 'prompt': prompt, 'started_ns': time.time_ns(),
               'session': item['session']['id'] if resume else '',
               'resume': item['session']['id'] if resume else None, 'phase': 'running'}
        # Persist intent before starting; an ambiguous handoff reserves a slot.
        self.save_job(job)
        try:
            job['pid'] = self.processes.start(job)
        except OSError as error:
            atomic_json(run / 'exit.json', {'error': str(error)})
            self.log(item, f'launch failed: {error}')
        self.save_job(job)
        self.jobs[item['id']] = job
        item['session'] = {'tool': tool, 'id': job['session'], 'folder': str(folder)}
        item['state'] = 'issued'
        item['attention'] = None
        self.log(item, 'resumed' if resume else 'started', True)
        return True

    def collect(self, item, job):
        if job['phase'] != 'running':
            return
        done, session, final, tail = self.processes.poll(job)
        if session:
            job['session'] = session
        item['session'] = {'tool': job['tool'], 'id': job['session'], 'folder': job['repo']}
        item['state'] = 'issued'
        if not done:
            self.save_job(job)
            self.log(item, 'collect: still running')
            return
        # Save the reply for replay if state persistence is interrupted.
        job['final'], job['tail'] = final, tail
        job['phase'] = 'ended'
        self.save_job(job)
        self.apply_final(item, job)

    def apply_final(self, item, job):
        final = job.get('final')
        if not final:
            self.attention(item, 'escalation', 'the run ended without a final message\n\n' + job.get('tail', ''))
        elif final['kind'] == 'handover':
            item['state'], item['attention'] = 'in review', None
            self.log(item, 'finished', True)
        elif final['kind'] in ('question', 'escalation'):
            self.attention(item, final['kind'], final['body'])
        elif job['tool'] == 'claude' and re.match(r'\s*```', final['body']):
            match = re.match(r'\s*```(?:json)?\s*\n(.*?)\n```', final['body'], re.S)
            try:
                request = json.loads(match[1]) if match else None
                if not isinstance(request, dict) or set(request) != {'branch', 'title', 'body', 'node_steps'}:
                    raise ValueError('expected branch, title, body, node_steps')
                if not all(isinstance(request[k], str) and request[k] for k in ('branch', 'title', 'body')):
                    raise ValueError('invalid PR fields')
                if request['node_steps'] is not None and not isinstance(request['node_steps'], str):
                    raise ValueError('invalid node_steps')
            except ValueError as error:
                self.attention(item, 'escalation', 'invalid Claude PR request: ' + str(error))
            else:
                job['request'], job['phase'] = request, 'pr'
                item['pr'] = self.github.open(item['repo'], request)
                self.log(item, f'opened PR: {item["pr"]}', True)
        else:
            self.attention(item, 'node-step', final['body'])
        if job['phase'] == 'ended':
            job['phase'] = 'collected'
        self.log(item, 'collected turn: ' + Path(job['run']).name)
        self.save_job(job)

    def merge(self, item, job):
        sha = self.github.merge(item['repo'], item['pr'])
        job['phase'], job['sha'] = 'merged', sha
        self.save_job(job)
        self.log(item, f'merged: {sha}', True)
        item['attention'] = None
        if job['request']['node_steps'] is not None:
            self.attention(item, 'node-step', job['request']['node_steps'])
            job['phase'] = 'node-step'
            self.save_job(job)

    def deliver(self, item):
        job = self.jobs.get(item['id'])
        attention = item['attention']
        if attention and attention['answer'] is not None:
            if not job or not item['session'] or not item['session']['id']:
                self.log(item, 'deferred: session unavailable')
            else:
                self.launch(item, attention['answer'], resume=True)
            return
        if attention and attention['kind'] == 'merge' and attention['approved']:
            if item['session']['tool'] == 'codex':
                if job and item['session']['id']:
                    self.launch(item, 'The owner approves the merge.', resume=True)
                else:
                    self.log(item, 'deferred: session unavailable')
                return
            if not job or not item['pr']:
                self.log(item, 'deferred: PR metadata unavailable')
                return
            status, failing = self.github.checks(item['repo'], item['pr'])
            self.log(item, 'approval checks: ' + status)
            if status == 'red':
                self.attention(item, 'escalation', 'failing check: ' + failing)
                return
            if status != 'green':
                return
            self.merge(item, job)
        if job and job['phase'] == 'merged' and item['session']['id']:
            self.launch(item, 'merged as ' + job['sha'], resume=True)

    def pr_checks(self, item, job):
        if job['phase'] != 'pr' or item['attention']:
            return
        # Recover PR number if creation succeeded but saving state did not.
        if item['pr'] is None:
            item['pr'] = self.github.open(item['repo'], job['request'])
        status, failing = self.github.checks(item['repo'], item['pr'])
        self.log(item, 'checks: ' + status)
        if status == 'red':
            self.attention(item, 'escalation', 'failing check: ' + failing)
        elif status == 'green':
            if item['stop_before_merge']:
                self.attention(item, 'merge', f'Approve PR #{item["pr"]}')
            else:
                self.merge(item, job)

    def run(self):
        with Path(str(self.path) + '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.git.pull(self.path)
            self.data = state.load(self.path)
            if not self.data["slices"]:
                return  # Nothing to mutate or commit in an empty scheduler.
            self.instant = self.clock()
            self.at = self.instant.isoformat()
            self.notices = []
            self.jobs = {}
            for path in sorted(self.runs.glob('*/job.json')):
                job = json.loads(path.read_text())
                previous = self.jobs.get(job['id'])
                if previous is None or previous['started_ns'] < job['started_ns']:
                    self.jobs[job['id']] = job
            for item in self.data['slices']:
                if item['state'] == 'accepted':
                    self.log(item, 'skipped: accepted slice')
                    continue
                self.log(item, 'pulled state')
                job = self.jobs.get(item['id'])
                if job:
                    before = job['phase']
                    if (before in ('merged', 'node-step')
                            and not any(e['event'] == 'merged: ' + job['sha'] for e in item['log'])):
                        self.log(item, 'merged: ' + job['sha'], True)
                        item['attention'] = None
                        if before == 'node-step':
                            self.attention(item, 'node-step', job['request']['node_steps'])
                    marker = 'collected turn: ' + Path(job['run']).name
                    if (job['phase'] in ('ended', 'collected', 'pr')
                            and not any(e['event'] == marker for e in item['log'])):
                        item['session'] = {'tool': job['tool'], 'id': job['session'], 'folder': job['repo']}
                        item['state'] = 'issued'
                        self.apply_final(item, job)
                    else:
                        self.collect(item, job)
                    # PR checks begin on a later pass than the waiting reply.
                    if before == 'pr':
                        self.pr_checks(item, job)
            for item in self.data['slices']:
                if item['state'] != 'accepted':
                    self.deliver(item)
            accepted = {s['id'] for s in self.data['slices'] if s['state'] == 'accepted'}
            for item in self.data['slices']:
                if (item['state'] == 'ready' and not item['paused'] and item['slot']
                        and datetime.fromisoformat(item['slot']) <= self.instant
                        and all(dep in accepted for dep in item['depends_on'])):
                    if item['brief'] is None:
                        self.attention(item, 'escalation', 'missing brief')
                        continue
                    brief = Path(item['brief'])
                    if not brief.is_absolute():
                        brief = self.path.parent / brief
                    try:
                        with brief.open(encoding='utf-8', newline='') as stream:
                            prompt = stream.read()
                        self.launch(item, prompt)
                    except OSError as error:
                        self.attention(item, 'escalation', str(error))
            for item in self.data['slices']:
                if item['state'] != 'accepted':
                    self.log(item, 'save and sync state')
            state.save(self.path, self.data)
            outbox = Path(str(self.path) + '.outbox.jsonl')
            if self.notices:
                with outbox.open('a') as stream:
                    for notice in self.notices:
                        stream.write(json.dumps(notice, ensure_ascii=False) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
            self.git.commit(self.path)
