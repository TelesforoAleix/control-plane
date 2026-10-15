# Repository instructions

This repository implements a scheduler's state file and commands. It does not run coding agents.
Keep the version 1 state contract and CLI rules stable. Use Python 3.13 and uv;
jsonschema is the only runtime dependency. Test against synthetic fixtures and temporary git
repositories. Never check private state or outbox data into this repository.
Run `uv run pytest` before submitting changes. Work through a PR with a green `test` check.
