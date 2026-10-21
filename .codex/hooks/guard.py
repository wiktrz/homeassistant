#!/usr/bin/env python3
"""Project hooks enforcing AI_DEVELOPMENT_GUIDELINES.md (Rules 2, 3, 6, 10). Stdlib only, fails open.

Usage (stdin = Claude Code hook JSON):  guard.py <pre-bash | post-edit | post-bash | stop>
  pre-bash   PreToolUse(Bash): deny `git add` / `git stage` / `git commit -a` (Rule 3).
  post-edit  PostToolUse(Edit|Write|MultiEdit): run check_duplicate_variable.py --validate (Rule 10).
  post-bash  PostToolUse(Bash): record a tests/runner.py pass for the current diff fingerprint (Rule 6).
  stop       Stop: block once if runner pass / doc sync / scenario registration is missing, then warn.
Env override (tests): HA_GUARD_ROOT (repo root; default $CLAUDE_PROJECT_DIR, then cwd).
"""
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys

ROOT = os.environ.get("HA_GUARD_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
STATE = os.path.join(ROOT, ".claude", "state", "runner_pass.json")
WATCHED = ("config/", "tests/", "scripts/")
DOCS = {"PLACES_AND_BEHAVIORS.md", "AGENT.md", "AGENTS.md", "llms.txt"}
VALIDATE_PATHS = re.compile(r"^(config/[^/]+\.py|\.env\.example|config/configuration\.yaml|config/templates\.yaml)$")
RUNNER_OK = "ALL TEST TIERS COMPLETED SUCCESSFULLY"
# runner.py rewrites last_verified timestamps in tests/test_registry.yaml; ignore them or a pass never sticks.
VOLATILE = re.compile(rb"^[+-]\s*last_verified:.*$", re.M)
PREFIX = {"sudo", "command", "exec", "time", "nohup", "env", "builtin", "!", "{", "then", "do", "else"}
GIT_OPTS_WITH_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
COMMIT_OPTS_WITH_ARG = {"-m", "--message", "-F", "--file", "-C", "-c", "--author", "--date", "--fixup", "--squash"}


def git(*args):
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, timeout=10).stdout


HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?^[ \t]*\2[ \t]*$", re.S | re.M)
SEPARATORS = set(";&|()`\n")


def segments(command):
    """Split a shell command into simple-command token lists; quoted text and heredoc bodies are data."""
    command = HEREDOC.sub("", command)
    lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()`\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        toks = command.split()
    seg = []
    for t in toks + [";"]:
        if set(t) <= SEPARATORS:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)


def staging_reason(command):
    """Return a deny reason if the command stages files, else None."""
    # ponytail: no recursion into `sh -c "..."`/eval/"$(...)" strings; add if agents start wrapping git that way.
    for toks in segments(command):
        while toks and (toks[0] in PREFIX or re.match(r"^[A-Za-z_]\w*=", toks[0])):
            toks.pop(0)
        if not toks or os.path.basename(toks[0]) != "git":
            continue
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 2 if toks[i] in GIT_OPTS_WITH_ARG else 1
        if i >= len(toks):
            continue
        sub, rest = toks[i], toks[i + 1:]
        if sub in ("add", "stage") and not ({"-n", "--dry-run", "-h", "--help"} & set(rest)):
            return f"`git {sub}`"
        if sub == "commit":
            j = 0
            while j < len(rest):
                t = rest[j]
                if t in COMMIT_OPTS_WITH_ARG:
                    j += 2
                    continue
                if t == "--all" or (re.match(r"^-[A-Za-z]+$", t) and "a" in t[1:]):
                    return "`git commit -a` (stages tracked files)"
                j += 1
    return None


def pre_bash(data):
    reason = staging_reason((data.get("tool_input") or {}).get("command") or "")
    if reason:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked {reason}: AI_DEVELOPMENT_GUIDELINES.md Rule 3 forbids staging. Leave all changes "
                "unstaged unless the user explicitly says 'commit' or 'push' in this turn; then ask the user "
                "to run it themselves."),
        }}
    return None


def post_edit(data):
    path = (data.get("tool_input") or {}).get("file_path") or ""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(ROOT)).replace(os.sep, "/")
    if not VALIDATE_PATHS.match(rel):
        return None
    script = os.path.join(ROOT, "scripts", "check_duplicate_variable.py")
    r = subprocess.run([sys.executable, script, "--validate"], capture_output=True, text=True, timeout=30, cwd=ROOT)
    if r.returncode == 0:
        return None
    return {"decision": "block", "reason": (
        f"Rule 10 violation after editing {rel}: `python3 scripts/check_duplicate_variable.py --validate` "
        f"exited {r.returncode}. Move fallbacks to .env.example and import them via config/energy_defaults.py.\n"
        + (r.stdout + r.stderr).strip()[-3000:])}


def changed_files():
    tracked = git("diff", "--name-only", "HEAD").decode(errors="replace").split()
    untracked = git("ls-files", "--others", "--exclude-standard", "-z", "--", *WATCHED).decode(errors="replace")
    return tracked, [p for p in untracked.split("\0") if p]


def fingerprint():
    h = hashlib.sha256(VOLATILE.sub(b"", git("diff", "HEAD", "--no-color", "--no-ext-diff")))
    for p in sorted(changed_files()[1]):
        h.update(b"\0" + p.encode())
        try:
            with open(os.path.join(ROOT, p), "rb") as f:
                h.update(VOLATILE.sub(b"", f.read()))
        except OSError:
            pass
    return h.hexdigest()


def post_bash(data):
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if "tests/runner.py" not in cmd:
        return None
    resp = data.get("tool_response")
    # Bash tool_response exposes stdout/stderr, not an exit code: rely on runner's success banner.
    text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
    # ponytail: banner match can be faked by e.g. `grep` of runner.py source; good enough for a self-check gate.
    if RUNNER_OK not in text or "SOME TESTS FAILED" in text or re.search(r"\brc=[1-9]", text):
        return None
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"fingerprint": fingerprint(), "command": cmd}, f)
    return None


def stop(data):
    tracked, untracked = changed_files()
    changed = set(tracked) | set(untracked)
    if not any(p.startswith(WATCHED) for p in changed):
        return None
    reasons = []
    try:
        with open(STATE, encoding="utf-8") as f:
            passed = json.load(f).get("fingerprint")
    except (OSError, ValueError):
        passed = None
    if passed != fingerprint():
        reasons.append("Rule 6: no passing run for the current diff - run `python3 tests/runner.py --auto`.")
    if any(p.startswith(("config/", "scripts/")) for p in changed) and not (DOCS & changed):
        reasons.append("Rule 2: config/ or scripts/ changed but none of PLACES_AND_BEHAVIORS.md, AGENT.md, "
                       "AGENTS.md, llms.txt did - sync docs.")
    added = set(untracked) | set(git("diff", "--name-only", "--diff-filter=A", "HEAD").decode(errors="replace").split())
    try:
        with open(os.path.join(ROOT, "tests", "test_registry.yaml"), encoding="utf-8") as f:
            registry = f.read()
    except OSError:
        registry = ""
    unregistered = sorted(p for p in added if p.startswith("tests/scenarios/") and p[len("tests/"):] not in registry)
    if unregistered:
        reasons.append("Rule 6: register scenarios in tests/test_registry.yaml: " + ", ".join(unregistered))
    if not reasons:
        return None
    msg = "Definition of Done not met (AI_DEVELOPMENT_GUIDELINES.md):\n- " + "\n- ".join(reasons)
    if data.get("stop_hook_active"):
        return {"systemMessage": "WARNING " + msg}
    return {"decision": "block", "reason": msg + "\nFix these, or tell the user explicitly why they are skipped."}


HANDLERS = {"pre-bash": pre_bash, "post-edit": post_edit, "post-bash": post_bash, "stop": stop}

if __name__ == "__main__":
    try:
        out = HANDLERS[sys.argv[1]](json.load(sys.stdin))
        if out:
            print(json.dumps(out))
    except Exception as e:  # fail open: never break the session
        print(f"guard.py {sys.argv[1:]} internal error (ignored): {e}", file=sys.stderr)
    sys.exit(0)
