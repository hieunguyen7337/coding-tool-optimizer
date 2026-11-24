import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, "tools", "push-guard.py")
ZERO = "0" * 40


class PushGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        self.env = dict(os.environ, LANES_STATE=self.state)
        self.env.pop("KIT_PUSH_GUARD", None)
        self.git("init", "-q")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", "x")
        self.sha = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo] + list(args), check=True, stdout=subprocess.PIPE,
                              universal_newlines=True).stdout

    def run_record(self, name, base=None, verdict="OK", report="### cmd\nexit code: 0\n", directory=None):
        run = os.path.join(self.state, "runs", name)
        os.makedirs(run)
        report_path = os.path.join(run, "report.md")
        with open(report_path, "w") as stream:
            stream.write(report)
        meta = {"verify": True, "place": "cloud", "dir": directory or self.repo, "verdict": verdict,
                "cloud": {"base": base or self.sha, "start_sha": "s" * 40, "report": report_path}}
        with open(os.path.join(run, "meta.json"), "w") as stream:
            json.dump(meta, stream)

    def hook(self, ref="refs/heads/main", sha=None, extra=None):
        env = dict(self.env, **(extra or {}))
        line = "refs/heads/x {} {} {}\n".format(sha or self.sha, ref, ZERO)
        return subprocess.run([sys.executable, GUARD, "origin", "url"], input=line, cwd=self.repo, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def test_pass(self):
        self.run_record("20261124-000000-haiku-repo")
        self.assertEqual(self.hook().returncode, 0)
        check = subprocess.run([sys.executable, GUARD, "--check", self.sha, "-C", self.repo], env=self.env)
        self.assertEqual(check.returncode, 0)

    def test_pass_from_other_worktree(self):
        # Verify runs are made from a clean worktree, not the checkout that pushes.
        self.run_record("20261124-000000-haiku-wt", directory=os.path.join(self.tmp.name, "clean-worktree"))
        self.assertEqual(self.hook().returncode, 0)

    def test_wrong_sha(self):
        self.run_record("20261124-000000-haiku-repo", base="a" * 40)
        result = self.hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn(self.sha, result.stderr)
        self.assertIn("lane haiku --cloud --verify -e low -C", result.stderr)
        self.assertIn("a" * 40, result.stderr)

    def test_prefix_not_accepted(self):
        self.run_record("20261124-000000-haiku-repo", base=self.sha[:12])
        self.assertEqual(self.hook().returncode, 1)

    def test_failed_verdict(self):
        self.run_record("20261124-000000-haiku-repo", verdict="PARTIAL")
        result = self.hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn("PARTIAL", result.stderr)

    def test_nonzero_exit_in_report(self):
        self.run_record("20261124-000000-haiku-repo", report="exit code: 0\nexit code: 2\n")
        self.assertEqual(self.hook().returncode, 1)

    def test_exit_lines_win_over_free_text(self):
        # Quoted test output ('gh api exit 1') and a branch name ('rc-3') are ignored once EXIT lines exist.
        text = ("Branch claude/kit-verify-rc-3cpihh\nshard 1 rc=0\n.... gh api exit 1: not found\n"
                "EXIT 0 python3 tools/run-tests.py\nEXIT 0 bash -n x\n")
        self.run_record("20261124-000000-haiku-repo", report=text + "EXIT 2 python3 -m json.tool a\n")
        result = self.hook()
        self.assertEqual(result.returncode, 1)
        self.assertIn("report exit codes [0, 0, 2]", result.stderr)
        self.run_record("20261124-000001-haiku-repo", report=text)
        self.assertEqual(self.hook().returncode, 0)

    def test_free_text_fallback_skips_words_and_paths(self):
        self.run_record("20261124-000000-haiku-repo",
                        report="Branch claude/kit-verify-rc-3cpihh\nshard 1 rc=0\n== Command 2 -> exit 0\n")
        self.assertEqual(self.hook().returncode, 0)

    def test_unguarded_branch(self):
        self.assertEqual(self.hook(ref="refs/heads/feature").returncode, 0)
        self.git("config", "kit.pushGuard.branches", "feature")
        self.assertEqual(self.hook(ref="refs/heads/feature").returncode, 1)
        self.assertEqual(self.hook(ref="refs/heads/main").returncode, 0)

    def test_deletion_and_tag(self):
        self.assertEqual(self.hook(sha=ZERO).returncode, 0)
        self.assertEqual(self.hook(ref="refs/tags/v1").returncode, 0)

    def test_off(self):
        result = self.hook(extra={"KIT_PUSH_GUARD": "off"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("WARNING", result.stderr)
        with open(os.path.join(self.state, "lead-log.md")) as stream:
            text = stream.read()
        self.assertIn(self.sha, text)
        self.assertIn("[push-guard off]", text)

    def test_install_refuses_foreign_hook(self):
        hook = os.path.join(self.repo, ".git", "hooks", "pre-push")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        with open(hook, "w") as stream:
            stream.write("#!/bin/sh\nexit 0\n")
        result = subprocess.run([sys.executable, GUARD, "--install-hook", self.repo], env=self.env,
                                stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("chain", result.stderr)
        with open(hook) as stream:
            self.assertEqual(stream.read(), "#!/bin/sh\nexit 0\n")

    def test_install_writes_hook(self):
        subprocess.run([sys.executable, GUARD, "--install-hook", self.repo], env=self.env, check=True)
        with open(os.path.join(self.repo, ".git", "hooks", "pre-push"), "rb") as stream:
            data = stream.read()
        self.assertTrue(data.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", data)
        self.assertIn(GUARD.replace("\\", "/").encode(), data)


if __name__ == "__main__":
    unittest.main()
