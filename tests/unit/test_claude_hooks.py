"""Tests for .claude/hooks/guard.py (project Claude Code hooks). Hermetic: temp git repo, no network."""
import json
import os
import shutil
import subprocess
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GUARD = os.path.join(REPO, ".claude", "hooks", "guard.py")
VALIDATOR = os.path.join(REPO, "scripts", "check_duplicate_variable.py")


def sh(cwd, *args):
    env = dict(os.environ, HOME=str(cwd))
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path):
    for d in ("config", "scripts", "tests/scenarios/demo"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "tests" / "test_registry.yaml").write_text("tests: []\n")
    (tmp_path / "config" / "a.py").write_text("x = 1\n")
    (tmp_path / "AGENT.md").write_text("doc\n")
    (tmp_path / ".gitignore").write_text(".claude/state/\n")
    shutil.copy(VALIDATOR, tmp_path / "scripts" / "check_duplicate_variable.py")
    sh(tmp_path, "git", "init", "-q")
    sh(tmp_path, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    sh(tmp_path, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return tmp_path


def run(repo, mode, payload):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["HA_GUARD_ROOT"] = str(repo)
    env["HOME"] = str(repo)
    r = subprocess.run([sys.executable, GUARD, mode], input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else None


def test_git_add_denied_git_status_allowed(repo):
    out = run(repo, "pre-bash", {"tool_name": "Bash", "tool_input": {"command": "cd x && git add -A"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "Rule 3" in out["hookSpecificOutput"]["permissionDecisionReason"]
    for ok in ("git status", 'echo "git add ."', "git diff --name-only", "git commit -m '-a'"):
        assert run(repo, "pre-bash", {"tool_input": {"command": ok}}) is None, ok
    for bad in ("git stage f", "git commit -am msg", "git -C . add f"):
        assert run(repo, "pre-bash", {"tool_input": {"command": bad}}) is not None, bad


def test_validate_hook_reports_violation(repo):
    target = repo / "config" / "pstryk_engine.py"  # one of the files the validator checks
    target.write_text("d = {}\nx = d.get('a', 5000)\n")
    out = run(repo, "post-edit", {"tool_name": "Write", "tool_input": {"file_path": str(target)}})
    assert out["decision"] == "block" and "5000" in out["reason"]
    target.write_text("x = 1\n")
    assert run(repo, "post-edit", {"tool_input": {"file_path": str(target)}}) is None
    assert run(repo, "post-edit", {"tool_input": {"file_path": str(repo / "AGENT.md")}}) is None


def test_stop_blocks_once_then_warns(repo):
    (repo / "config" / "a.py").write_text("x = 2\n")
    (repo / "tests" / "scenarios" / "demo" / "test_new.yaml").write_text("steps: []\n")
    out = run(repo, "stop", {"stop_hook_active": False})
    assert out["decision"] == "block"
    for needle in ("tests/runner.py --auto", "Rule 2", "tests/scenarios/demo/test_new.yaml"):
        assert needle in out["reason"]
    warn = run(repo, "stop", {"stop_hook_active": True})
    assert "decision" not in warn and "runner.py" in warn["systemMessage"]


def test_runner_pass_unblocks_stop(repo):
    (repo / "config" / "a.py").write_text("x = 2\n")
    (repo / "AGENT.md").write_text("doc synced\n")
    run(repo, "post-bash", {"tool_input": {"command": "python3 tests/runner.py --auto"},
                            "tool_response": {"stdout": "...\n=== SOME TESTS FAILED ===", "stderr": ""}})
    assert run(repo, "stop", {})["decision"] == "block"
    run(repo, "post-bash", {"tool_input": {"command": "python3 tests/runner.py --auto"},
                            "tool_response": {"stdout": "🎉 === ALL TEST TIERS COMPLETED SUCCESSFULLY! ===",
                                              "stderr": "", "interrupted": False}})
    assert run(repo, "stop", {}) is None
    (repo / "config" / "a.py").write_text("x = 3\n")  # new diff -> pass no longer valid
    assert "runner.py" in run(repo, "stop", {})["reason"]


def test_clean_tree_and_bad_input_fail_open(repo):
    assert run(repo, "stop", {}) is None
    r = subprocess.run([sys.executable, GUARD, "stop"], input="not json", capture_output=True, text=True,
                       env={**os.environ, "HA_GUARD_ROOT": str(repo)})
    assert r.returncode == 0 and r.stdout == ""
