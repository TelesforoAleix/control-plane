"""Executor adapters and detached turn supervisor; no shell interpretation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

FINAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "body"],
    "properties": {"kind": {"type": "string", "enum": ["handover", "question", "escalation", "waiting"]},
                   "body": {"type": "string"}},
}
CLAUDE_SETTINGS = {"sandbox": {
    "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
    "network": {"allowedDomains": ["github.com", "api.github.com", "*.githubusercontent.com",
                                  "codeload.github.com", "pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]},
}}
CLAUDE_PARAGRAPH = '''\n\nGitHub steps: gh and ssh do not work in your sandbox; git push does. Push your branch and end with kind "waiting". Its body must begin with a fenced JSON block containing {"branch": "your branch", "title": "PR title", "body": "PR description", "node_steps": null}. Set node_steps to text instead of null if work on the owner node is needed after merging. The runner opens the PR, checks CI, obtains any required merge approval, and merges it. It resumes you with "merged as <sha>"; then write your handover.'''


def atomic_json(path, data):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def command(job):
    folder = Path(job['run'])
    if job['tool'] == 'codex':
        args = ['codex', 'exec', '-m', job['model'], '-c', 'sandbox_mode="workspace-write"',
                '-c', 'sandbox_workspace_write.network_access=true',
                '-c', 'sandbox_workspace_write.writable_roots=' + json.dumps([str(Path(job['repo']) / '.git')]),
                '--output-schema', str(folder / 'schema.json'), '--json', '-o', str(folder / 'last.json')]
        if job.get('resume'):
            args += ['resume', job['resume']]
        return args + ['--', job['prompt']]
    args = ['claude', '-p', '--model', job['model'], '--settings', str(folder / 'sandbox.json'),
            '--permission-mode', 'acceptEdits', '--allowedTools', 'Bash', '--output-format', 'json',
            '--json-schema', json.dumps(FINAL_SCHEMA)]
    if job.get('resume'):
        args += ['--resume', job['resume']]
    return args + ['--', job['prompt']]


class Processes:
    def start(self, job):
        folder = Path(job['run'])
        atomic_json(folder / 'schema.json', FINAL_SCHEMA)
        atomic_json(folder / 'sandbox.json', CLAUDE_SETTINGS)
        atomic_json(folder / 'request.json', job)
        # The supervisor is outside the CLI process group and closes all terminal FDs.
        with (folder / 'supervisor.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(folder)],
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, close_fds=True)
        job['pid'] = proc.pid
        return proc.pid

    def poll(self, job):
        folder = Path(job['run'])
        session = job.get('session', '')
        final = None
        if job['tool'] == 'codex':
            logfile = folder / 'turn.jsonl'
            if logfile.exists():
                for line in logfile.read_text(errors='replace').splitlines():
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and event.get('type') == 'thread.started':
                            session = event['thread_id']
                    except (ValueError, KeyError):
                        continue
            last = folder / 'last.json'
            if last.exists() and last.stat().st_mtime_ns > job['started_ns']:
                try:
                    final = json.loads(last.read_text())
                except ValueError:
                    pass
        else:
            logfile = folder / 'claude.json'
            if logfile.exists() and logfile.stat().st_mtime_ns > job['started_ns']:
                try:
                    reply = json.loads(logfile.read_text())
                    if isinstance(reply, dict):
                        session = reply.get('session_id', session)
                        final = reply.get('structured_output')
                except ValueError:
                    pass
        done = (folder / 'exit.json').exists()
        if not done:
            # Detect a killed supervisor without confusing a reused PID for our process.
            pid = job.get('pid')
            if not pid and (folder / 'supervisor.json').exists():
                pid = json.loads((folder / 'supervisor.json').read_text())['pid']
            if pid:
                # A killed supervisor can leave its executor alive in this process group.
                # Reserving capacity for an ambiguous/reused group is safer than a duplicate.
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    done = True
                except PermissionError:
                    pass
        valid = (isinstance(final, dict) and set(final) == {'kind', 'body'}
                 and final['kind'] in FINAL_SCHEMA['properties']['kind']['enum']
                 and isinstance(final['body'], str))
        lines = logfile.read_text(errors='replace').splitlines() if logfile.exists() else []
        errors = folder / 'stderr.log'
        if errors.exists():
            lines += errors.read_text(errors='replace').splitlines()
        tail = '\n'.join(lines[-40:])
        return done, session, final if valid else None, tail


def supervise(folder):
    job = json.loads((folder / 'request.json').read_text())
    logfile = folder / ('turn.jsonl' if job['tool'] == 'codex' else 'claude.json')
    atomic_json(folder / 'supervisor.json', {'pid': os.getpid()})
    code = 1
    try:
        with logfile.open('ab') as log, (folder / 'stderr.log').open('ab') as errors:
            code = subprocess.run(command(job), cwd=job['repo'], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=errors, close_fds=True).returncode
    except Exception as error:
        with logfile.open('a') as log:
            log.write(str(error) + '\n')
    finally:
        atomic_json(folder / 'exit.json', {'code': code, 'at': time.time()})


if __name__ == '__main__':
    supervise(Path(sys.argv[1]))
