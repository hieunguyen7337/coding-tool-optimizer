#!/usr/bin/env python3
"""Refuse a push to a protected branch unless that exact commit passed a cloud verify run.

No model is involved. Uses:
  push-guard.py --install-hook <repo>   write <repo>/.git/hooks/pre-push (git hook mode reads git's stdin)
  push-guard.py --check <sha> [-C repo] exit 0 when a verify run passed that full sha, else print why and exit 1
  push-guard.py <remote> <url>          the pre-push hook itself (stdin: local ref, local sha, remote ref, remote sha)
Config: `git config kit.pushGuard.branches "main master"` overrides the guarded branches.
Escape hatch: KIT_PUSH_GUARD=off pushes anyway, warns, and logs to <state>/lead-log.md.
"""

import json
import os
import re
import subprocess
import sys
import time

ZERO = "0" * 40
DEFAULT_BRANCHES = ("master", "main")
FULL_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


def state_dir():
    # Same rule as bin/lane state_dir().
    return os.path.abspath(os.path.expanduser(os.environ.get("LANES_STATE", "~/.local/state/lanes")))


def same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def git(repo, *args):
    result = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    return result.returncode, result.stdout.strip()


def read_runs():
    """Every run's meta.json, newest first (run ids start with a timestamp)."""
    runs = os.path.join(state_dir(), "runs")
    try:
        names = sorted(os.listdir(runs), reverse=True)
    except OSError:
        return []
    out = []
    for name in names:
        try:
            with open(os.path.join(runs, name, "meta.json"), encoding="utf-8") as stream:
                meta = json.load(stream)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict):
            out.append((name, meta))
    return out


def is_verify(meta):
    return meta.get("verify") is True and meta.get("place") == "cloud" and isinstance(meta.get("cloud"), dict)


def tested_sha(meta):
    # bin/lane: the VM clones the pushed `cloud.base` (HEAD of the repo at launch) and adds an empty start commit
    # on top (`cloud.start_sha`), so the tested tree is `base`. If the session started from another parent
    # (`cloud.started_from` differs from base), nothing is proven for base.
    cloud = meta["cloud"]
    base = cloud.get("base")
    started = cloud.get("started_from")
    if started is not None and started != base:
        return None
    return base


# bin/lane's verify prompt (from 2026-11-24.1) ends the report with one `EXIT <code> <command>` line per command;
# when the report has them, only they count. Older reports are free text: then any exit-code mention counts that
# is not part of a word or path (a branch name 'verify-rc-3cpihh' once read as rc -3). Free text also quotes test
# output ('gh api exit 1'), so the fallback can refuse a passing run, never pass a failing one.
EXIT_LINE_RE = re.compile(r"^EXIT[ \t]+(-?\d+)\b", re.MULTILINE)
EXIT_RE = re.compile(r"(?<![\w/.-])(?:exit(?:ed)?(?:\s+with)?(?:\s+code|\s+status)?|exit_code|rc|returncode)"
                     r"\**[ \t:=*`]*(-?\d+)\b", re.IGNORECASE)


def report_exit_codes(path):
    """Exit codes found in the report. bin/lane's verify step asks for "its exit code" per command, as free text,
    so every `exit code: N` / `exited N` / `exit=N` mention counts. None when the report is unreadable or has none."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except (OSError, TypeError):
        return None
    codes = [int(code) for code in EXIT_LINE_RE.findall(text)]
    if not codes:
        codes = [int(code) for code in EXIT_RE.findall(text)]
    return codes or None


def passed(meta):
    """Pass rule: verdict OK (bin/lane gives OK only when the done subject has no PARTIAL), and the report
    lists at least one exit code and every one is 0."""
    if meta.get("verdict") != "OK":
        return False
    codes = report_exit_codes(meta["cloud"].get("report"))
    return bool(codes) and all(code == 0 for code in codes)


def verified(repo, sha):
    """Return (ok, newest verify run of this repo or None).

    A pass may come from any folder: verify runs are made from a separate clean worktree (`-C <worktree>`), not the
    checkout that pushes, and a full sha names the same tree in every clone. The folder only picks which run the
    refusal names."""
    newest = None
    for name, meta in read_runs():
        if not is_verify(meta):
            continue
        if tested_sha(meta) == sha and passed(meta):  # full sha equality, never a prefix
            return True, (name, meta)
        directory = meta.get("dir") or meta.get("dir_path") or meta.get("repo")
        if newest is None and (not directory or same_path(directory, repo)):
            newest = (name, meta)
    return False, newest


def guarded_branches(repo):
    code, out = git(repo, "config", "--get", "kit.pushGuard.branches")
    names = out.split() if code == 0 and out else list(DEFAULT_BRANCHES)
    return {"refs/heads/" + name for name in names}


def refusal(repo, sha, newest, ref=None):
    seen = "no verify run"
    if newest:
        meta = newest[1]
        seen = "newest verify {} tested {} verdict {}; report exit codes {}".format(
            newest[0], tested_sha(meta), meta.get("verdict"), report_exit_codes(meta["cloud"].get("report")))
    return "push-guard: refused{} {}: {}; run: lane haiku --cloud --verify -e low -C {} -b <brief>".format(
        " " + ref if ref else "", sha, seen, repo)


def repo_root():
    code, out = git(".", "rev-parse", "--show-toplevel")
    return out if code == 0 else os.getcwd()


def log_off(repo, shas):
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "lead-log.md"), "a", encoding="utf-8") as stream:
        for sha in shas:
            stream.write("{} {} {} [push-guard off]\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), repo, sha))


def hook(stdin):
    repo = repo_root()
    guarded = guarded_branches(repo)
    refused, off_shas = [], []
    off = os.environ.get("KIT_PUSH_GUARD") == "off"
    for line in stdin.read().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        _, sha, remote_ref, _ = parts[:4]
        if sha == ZERO or remote_ref not in guarded:
            continue
        ok, newest = verified(repo, sha)
        if ok:
            continue
        if off:
            off_shas.append(sha)
        else:
            refused.append(refusal(repo, sha, newest, remote_ref))
    if refused:
        print("\n".join(refused), file=sys.stderr)
        return 1
    if off_shas:
        print("push-guard: WARNING KIT_PUSH_GUARD=off, pushing unverified commit(s) {}".format(", ".join(off_shas)),
              file=sys.stderr)
        log_off(repo, off_shas)
    return 0


def check(sha, repo):
    if not FULL_SHA.match(sha):
        print("push-guard: --check needs a full sha, not a prefix", file=sys.stderr)
        return 2
    ok, newest = verified(repo, sha)
    if ok:
        print("push-guard: {} passed verify run {}".format(sha, newest[0]))
        return 0
    print(refusal(repo, sha, newest), file=sys.stderr)
    return 1


def install_hook(repo):
    git_dir_code, git_dir = git(repo, "rev-parse", "--git-path", "hooks")
    if git_dir_code:
        print("push-guard: {} is not a git repository".format(repo), file=sys.stderr)
        return 2
    hooks = git_dir if os.path.isabs(git_dir) else os.path.join(repo, git_dir)
    os.makedirs(hooks, exist_ok=True)
    path = os.path.join(hooks, "pre-push")
    script = os.path.abspath(__file__).replace("\\", "/")
    python = sys.executable.replace("\\", "/")
    content = '#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(python, script)
    if os.path.exists(path):
        with open(path, "rb") as stream:
            existing = stream.read()
        if existing == content.encode("utf-8"):
            print("push-guard: hook already installed at {}".format(path))
            return 0
        print("push-guard: {} already exists and differs; not overwriting.\n"
              "To chain it, put this in that hook so git's stdin reaches both:\n"
              "  input=$(cat); printf '%s\\n' \"$input\" | \"{}\" \"{}\" \"$@\" || exit 1".format(
                  path, python, script), file=sys.stderr)
        return 1
    with open(path, "wb") as stream:
        stream.write(content.encode("utf-8"))
    os.chmod(path, 0o755)
    print("push-guard: installed {}".format(path))
    return 0


def main(argv):
    if argv and argv[0] == "--install-hook":
        return install_hook(os.path.abspath(argv[1])) if len(argv) == 2 else 2
    if argv and argv[0] == "--check":
        repo = repo_root()
        rest = argv[1:]
        if "-C" in rest:
            index = rest.index("-C")
            repo = os.path.abspath(rest[index + 1])
            del rest[index:index + 2]
        return check(rest[0], repo) if len(rest) == 1 else 2
    return hook(sys.stdin)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
