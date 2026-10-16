# Repository instructions

This repository implements a scheduler's state file, commands, and runner. The runner
starts and resumes Codex and Claude agents only as the runner slice contract describes: at most two at once, never two in one
repository, and only through `cplane run` under launchd or an explicit test.
Keep the version 1 state contract and CLI rules stable. Use Python 3.13 and uv;
jsonschema is the only runtime dependency. Test against synthetic fixtures and temporary git
repositories. Never check private state or outbox data into this repository.
Run `uv run pytest` before submitting changes. Work through a PR with a green `test` check.
