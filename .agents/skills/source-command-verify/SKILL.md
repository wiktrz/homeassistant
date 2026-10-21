---
name: "source-command-verify"
description: "Run the full Definition of Done check (tests, Rule 10 validator, runner, doc sync, registry, retrospective)"
---

# source-command-verify

Use this skill when the user asks to run the migrated source command `verify`.

## Command Template

Verify the current working tree against AI_DEVELOPMENT_GUIDELINES.md. Run each step in order, record the exit code (`; echo rc=$?`), and keep going after a failure so the table is complete. Do not stage or commit anything.

1. `python3 -m pytest tests/invariants tests/unit -q -p no:cacheprovider; echo rc=$?`
2. `python3 scripts/check_duplicate_variable.py --validate; echo rc=$?` (Rule 10)
3. `python3 tests/runner.py --auto; echo rc=$?` (Rule 6), then always `python3 tests/runner.py --stop; echo rc=$?`
4. Doc sync (Rule 2): `git diff --name-only HEAD` plus `git ls-files --others --exclude-standard`. If anything under `config/` or `scripts/` changed, at least the relevant ones of `PLACES_AND_BEHAVIORS.md`, `AGENT.md`, `AGENTS.md`, `llms.txt` must also be changed (and `SPEC_DEYE_BATTERY_SYSTEM.md` when battery/inverter logic changed). Check the content matches the change, not only the file name.
5. Registry (Rule 6): every new or untracked file under `tests/scenarios/` must be referenced (as `scenarios/<area>/<file>`) in `tests/test_registry.yaml`.
6. Retrospective (Rule 7): load the `retro` skill if available; otherwise write a short retro covering the four Rule 7 points (domain logic vs mere success, hardware flags least privilege, user feedback, docs/invariants re-check).

Report a table:

| Step | Command | Exit code | Result |
|------|---------|-----------|--------|

Then one line: `VERIFY: PASS` only if every step passed, else `VERIFY: FAIL` with the failing steps.
