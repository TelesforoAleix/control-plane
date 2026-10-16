# Control plane

A scheduler's state file and its commands. `cplane` helps an owner schedule slices of
coding work, answer questions, approve merges, and see what is pending. It records
intent through owner commands. `start` sets a slot to now; `resume` clears a pause
flag. The separate `run` command starts and resumes Codex and Claude executors.
Planning agents use `add`, `edit`, `state`, `attention`, and `show` to prepare
briefs, mark slices ready, review handovers, and accept completed work.

Requires Python 3.13 or later and uv. Install the locked environment with `uv sync`.
The only direct runtime dependency is the MIT-licensed `jsonschema` library.

```sh
uv run cplane --help
export CPLANE_STATE=/path/to/state.json
uv run cplane pending
uv run cplane pending --json
uv run cplane --actor planner add --id example/W2 --repo example/project --feature example --title 'Next slice' --brief briefs/W2.md
uv run cplane --actor planner edit example/W2 --depends-on example/W1
uv run cplane --actor planner state example/W2 ready
uv run cplane show example/W2 --json
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

| Command | Purpose |
| --- | --- |
| `pending [--json]` | List pending slices by group. |
| `show <id> [--json]` | Print one complete slice, including its log. |
| `add --id <id> --repo <repo> --feature <text> --title <text> [options]` | Add a planned slice; refuse an existing ID. |
| `edit <id> [options]` | Change only the supplied fields. |
| `state <id> <state> [--session <tool>:<id>:<folder>] [--force --reason <text>]` | Record an allowed state transition. Quote states containing spaces. |
| `attention <id> --kind <kind> --body <text>` | Record attention and append a notice. |
| `attention <id> --clear` | Clear attention. |
| `slot <id> <when>` / `unslot <id>` | Record or clear a scheduled time. |
| `start <id>` | Slot a ready slice now. |
| `pause <id>` / `resume <id>` | Set or clear the pause flag. |
| `approve <id>` / `answer <id> <text>` | Approve merge attention or answer a question/escalation. |
| `run` | Make one runner pass. |
| `install-agent` / `uninstall-agent` | Install or remove the launchd runner. |

`add` and `edit` accept `--repo`, `--feature`, `--title`, `--brief`,
`--tool codex|claude`, `--model`, `--stop-before-merge` (or
`--no-stop-before-merge`), and `--depends-on <id> …`. Dependencies replace the
list; `--depends-on` with no IDs clears it, and repeated occurrences extend the
replacement list. `edit` also accepts `--id` to change the ID; references in other
slices are left as recorded. IDs remain unique. While issued or awaiting handover,
only the title and dependencies can be edited.

New slices default to executor `codex` / `gpt-6.1-sol`, no brief, no dependencies,
`stop_before_merge` and `paused` false, and null slot, session, PR, and attention.
The command creates the first log entry. Brief paths are stored without reading
the file. A brief must be set before moving to ready.

Allowed state transitions are:

| From | To |
| --- | --- |
| planned | researching, ready |
| researching | planned, ready |
| ready | planned (clears slot), issued (requires `--session`) |
| issued, awaiting handover | in review, escalated, awaiting handover |
| escalated | issued |
| in review | accepted, issued |

Every other transition is refused with a reason. From any state,
`state <id> planned --force --reason <text>` resets to planned and clears the
slot; the nonempty reason is recorded in the log. Other fields remain recorded.
`--session` is allowed only for ready → issued and records an executor already
started outside the runner; this command starts no process. The tool must be codex
or claude, and both ID and folder must be nonempty. Folder paths may contain colons.
State log events include the destination and any forced-reset reason.

Attention kinds are `question`, `escalation`, `merge`, `node-step`, and
`owner-check`. Setting attention replaces its body and timestamp, clears its
answer and approval, and leaves the slice's state as recorded. Clearing attention
adds no notice. All planner writes accept `--actor` (default `owner`) and
`--commit`, like the owner commands. `show --json` returns one slice object.

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
question or escalation attention. Other owner mutations apply to any existing slice,
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

`slot`, `start`, `approve`, `answer`, and setting `attention` also append one JSON line containing
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
shell commands. The runner passes brief and answer text to the executor as a single
argument; the executor itself can perform the work described by that text. Git runs as an argument array and uses the state repository's
normal git configuration and hooks. Use a trusted local state repository. Python's
`fcntl` locking targets macOS and Unix; Windows is not supported.

Run `uv run pytest` for the synthetic fixture, concurrent writer, time, CLI, schema,
and temporary bare-remote Git tests. CI runs the tests and gitleaks on every PR and
on pushes to `main`. Main requires the `test` check and forbids force pushes.

## Runner

```sh
export CPLANE_REPOS="$HOME/Code"
export CPLANE_RUNS="$HOME/Library/Application Support/cplane/runs"
uv run cplane --state /path/to/state.json run
uv run cplane --state /path/to/state.json install-agent
uv run cplane uninstall-agent
```

Only `run` starts or resumes executors. It makes one pass under the state lock:
pull the state repository, collect finished turns, deliver answers and approvals,
start due slices, validate and save state, append notices, then commit and push.
The runner always syncs state in a git repository; it does not need `--commit`.
Outside git it still saves state and notices, which is useful for temporary tests.
Brief paths resolve relative to the state file's directory. Repository checkouts
resolve as `$CPLANE_REPOS/<repository-name>` (default `~/Code`). Existing owner
commands, the version 1 schema, and the outbox format are unchanged.

A slice starts when ready, unpaused, at or after its slot, with every dependency
accepted. Accepted slices are skipped with a log entry on each pass; the runner
does not start, resume, deliver to, merge for, or collect into them, including when
saved jobs or attention remain. Codex uses the slice's model when its latest five-hour usage is below
100%. Otherwise Claude uses opus for a Sol model and sonnet for other models,
provided its usage file is less than an hour old and below 90% for five hours.
Missing usage is unavailable capacity. The next pass retries deferred work.
Resumes retain the session's tool and model and wait for that tool's capacity;
sessions cannot transfer between providers. At most two turns run concurrently,
and only one per repository. Sessions whose local run metadata is missing still
occupy a slot until reconciled. Use a single state file and consistent run directory
for the scheduler.

Codex receives the brief unchanged. Claude receives it followed by the GitHub
instructions in `cplane/executors.py`: push the branch and return a waiting message
with a fenced JSON PR request (`branch`, `title`, `body`, `node_steps`). The runner
creates the PR and checks it on later passes. Failing checks raise escalation;
pending checks wait. Green checks allow a merge, with owner approval first when
`stop_before_merge` is true. After merging it resumes Claude with the merge SHA,
or raises node-step attention if the request requires owner-node work. Codex
handles its own GitHub work; approved Codex merge attention resumes it with the
owner's approval. `answer` and `approve` only record the response; the next pass
delivers it subject to capacity and concurrency limits.

A handover moves the slice to `in review` and emits `finished`. Questions and
escalations create attention; an escalation sets `escalated`. Ordinary waiting
messages create node-step attention. A terminated turn without a valid, fresh
final message raises escalation with the last 40 log lines. All runner actions
append log entries with actor `runner`. Notices go to the existing local outbox;
there is no external notification sender.

The detached supervisor writes executor output, final replies, session IDs, exit
markers and private turn metadata under `CPLANE_RUNS` (default
`~/Library/Application Support/cplane/runs`). Each state path has its own directory,
with one subdirectory per turn. Session IDs are recorded when they appear in the
executor output; the initial session ID can be empty until collection. Codex final
files must be newer than the turn's start; Claude replies come from
`structured_output`. Run metadata supports collection across CLI restarts. Keep
these files private like state and outbox: they contain prompts and answers.
Do not delete them while a turn is active. Retain the same absolute state path.

`install-agent` writes and bootstraps `~/Library/LaunchAgents/com.cplane.runner.plist`.
It uses the current Python environment and checkout, an absolute state path,
a 300-second interval, and `AbandonProcessGroup = true`. Logs go to
`~/Library/Logs/cplane/runner.log` and `runner-error.log`. It captures PATH,
CPLANE_REPOS and CPLANE_RUNS, since launchd does not inherit a terminal environment.
Keep the Python environment, checkout and executor binaries available after
installation. `uninstall-agent` unloads and removes only that agent; detached
executors already running continue. Installation is a separate, explicit action.

As with owner commands, state, outbox, run metadata and remote GitHub effects are
separate writes, not a distributed transaction. Collection replies can be replayed
when state saving fails; PR lookup and merge lookup recover completed GitHub steps.
A crash in the launch/supervisor handoff may need inspection of the run directory;
the runner conservatively reserves capacity rather than launching a duplicate.
Sync failures retain the local state commit. Resolve them before another pass.
