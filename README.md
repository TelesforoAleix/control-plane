# Control plane

A scheduler's state file and its commands. `cplane` helps an owner schedule slices of
coding work, answer questions, approve merges, and see what is pending. It records
intent only: no command starts, resumes, or calls an executor. `start` sets a slot to
now; `resume` clears a pause flag.

Requires Python 3.13 or later and uv. Install the locked environment with `uv sync`.
The only direct runtime dependency is the MIT-licensed `jsonschema` library.

```sh
uv run cplane --help
export CPLANE_STATE=/path/to/state.json
uv run cplane pending
uv run cplane pending --json
uv run cplane slot example/W1 'Mon 03:00'
uv run cplane slot example/W1 'tomorrow 06:00'
uv run cplane unslot example/W1
uv run cplane start example/W1
uv run cplane pause example/W1
uv run cplane resume example/W1
uv run cplane approve example/W1
uv run cplane answer example/W1 'Use the existing interface'
uv run cplane --actor owner --commit slot example/W1 '2026-10-19T03:00:00+02:00'
```

`--state PATH` overrides `CPLANE_STATE`. Global options may appear before or after
the command. The state file must already exist. It belongs to a separate repository;
this repository contains only the schema and synthetic test fixtures.

The version 1 shape is defined in [schema/slices.schema.json](schema/slices.schema.json)
(JSON Schema draft 2020-12). The loader additionally checks uniqueness of slice IDs,
which JSON Schema cannot express for one field of objects in an array. Unknown
properties, missing fields, and timestamps without offsets are rejected. Dependency
IDs may refer to absent slices; those dependencies count as unmet.

A minimal synthetic state file:

```json
{
  "version": 1,
  "slices": [{
    "id": "example/W1",
    "repo": "example/project",
    "feature": "example",
    "title": "An example slice",
    "state": "ready",
    "brief": "briefs/W1.md",
    "executor": {"tool": "codex", "model": "example-model"},
    "stop_before_merge": true,
    "depends_on": [],
    "slot": null,
    "paused": false,
    "session": null,
    "pr": null,
    "attention": null,
    "log": []
  }]
}
```

`slot` and `start` require `ready`. They name every unmet dependency in a warning
but still record the slot. `approve` requires merge attention; `answer` requires
question or escalation attention. Other mutations apply to any existing slice,
including repeated calls. Refusals exit nonzero with one line on stderr and leave
the state unchanged. A warning accompanies a successful command and exits zero.

Offset-free ISO timestamps and short forms use Europe/Copenhagen, including its
summer/winter offset. Explicit offsets are preserved. Weekday forms choose the
next occurrence of that time (later today, or next week if already passed).
`today HH:MM` is also supported. Ambiguous and nonexistent local times during
clock changes require an explicit offset. Quote time expressions and answers.

`pending` omits accepted slices and assigns each remaining slice to one group,
in this precedence order:

- **needs attention:** unanswered question/escalation, unapproved merge, node-step,
  owner-check, or a slice in review/escalated;
- **running:** issued or awaiting handover, even if its slot is still recorded;
- **scheduled:** other slices with a slot, sorted by instant and shown by local day;
- **ready:** unscheduled ready slices;
- **planned:** remaining slices, including researching.

Groups are displayed in the order needs attention, scheduled, ready, running,
planned. Within other groups, IDs sort alphabetically. Paused slices remain visible.
`--json` returns those group names as keys and arrays of full slice records as values.

Each mutation locks `<state>.lock`, re-reads and validates, changes one slice,
appends `{at, actor, event}` to its log, validates again, and atomically replaces
the state using a flushed temporary file in the same directory. The actor defaults
to `owner`; the event is the command name. A stable sidecar lock serializes writers
across replacement of the state file. Existing state permissions are preserved.
Readers see a complete old or new state file.

`slot`, `start`, `approve`, and `answer` also append one JSON line containing
`{at, id, event, text}` to `<state>.outbox.jsonl` under the same lock. Nothing sends
these notices. State replacement and outbox append are separate filesystem
operations: an I/O failure or crash between them can leave a saved state without
its notice. Errors after the state save do not roll back that save. The outbox
contains answer text; protect it like the state file. These are local files, not
a transaction across multiple files or machines.

With `--commit`, a successful mutation inside a git work tree stages the state
and existing outbox and commits only those paths as `cplane: <command> <id>`.
Then it pulls with rebase and pushes, for at most three pull/push attempts.
Outside a git work tree, `--commit` still saves the files. The state repository
must have a configured upstream, git identity, and credentials. Unrelated staged
changes stay outside the commit but can prevent rebase; keep that work tree clean.
A conflicting rebase is aborted to restore the local commit, then the command
exits nonzero with the reason. Other sync failures also leave the local commit.
Resolve synchronization in the state repository before retrying a mutation.
Add `<state>.lock` to that repository's ignore rules; keep the outbox tracked.

The tool never interprets brief paths, executor/model values or answer text as
shell commands. Git runs as an argument array and uses the state repository's
normal git configuration and hooks. Use a trusted local state repository. Python's
`fcntl` locking targets macOS and Unix; Windows is not supported.

Run `uv run pytest` for the synthetic fixture, concurrent writer, time, CLI, schema,
and temporary bare-remote Git tests. CI runs the tests and gitleaks on every PR and
on pushes to `main`. Main requires the `test` check and forbids force pushes.
