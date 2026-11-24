"""Tests for `lane-stats risk`: the light / normal / deep classifier. Runs on temp git repositories only."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "lane-stats")
loader = importlib.machinery.SourceFileLoader("lane_stats_risk_tested", SCRIPT)
stats = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
loader.exec_module(stats)


def git(repo, *argv):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(argv),
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        self.write("app.py", "x = 1\n# note\n")
        self.write("web.js", "let a = 1;\n// note\n")
        self.write("README.md", "hello\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = subprocess.check_output(["git", "-C", self.repo, "rev-parse", "HEAD"], text=True).strip()

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)

    def commit(self):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "change")

    def run_risk(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = stats.main(["risk", "--dir", self.repo, "--base", self.base] + list(extra))
        self.assertEqual(code, 0)
        return out.getvalue()

    def tier(self):
        return json.loads(self.run_risk("--json"))

    def test_empty_diff_is_light(self):
        result = self.tier()
        self.assertEqual(result["tier"], "light")
        self.assertEqual(result["reasons"], ["no changes"])
        self.assertEqual(result["files"], [])

    def test_docs_only_is_light(self):
        self.write("README.md", "changed\n")
        self.write("docs/guide.txt", "new\n")
        self.commit()
        result = self.tier()
        self.assertEqual(result["tier"], "light")
        self.assertIn("haiku low skim", result["review"])

    def test_code_is_normal(self):
        self.write("app.py", "x = 2\n# note\n")
        self.commit()
        result = self.tier()
        self.assertEqual(result["tier"], "normal")
        self.assertEqual(result["files"], ["app.py"])
        self.assertIn("lane-stats pick --class review", result["review"])

    def test_python_comment_only_is_light(self):
        self.write("app.py", "x = 1\n# a better note\n\n")
        self.commit()
        self.assertEqual(self.tier()["tier"], "light")

    def test_python_comment_plus_code_is_normal(self):
        self.write("app.py", "x = 1\n# a better note\ny = 2\n")
        self.commit()
        self.assertEqual(self.tier()["tier"], "normal")

    def test_js_comment_only_is_light(self):
        self.write("web.js", "let a = 1;\n/* block\n * more\n */\n// other\n")
        self.commit()
        self.assertEqual(self.tier()["tier"], "light")

    def test_js_hash_line_is_code(self):
        self.write("web.js", "let a = 1;\n# not a js comment\n")
        self.commit()
        self.assertEqual(self.tier()["tier"], "normal")

    def test_deep_paths(self):
        for name in ("install.sh", "src/oauth_client.py", ".github/workflows/ci.yml", "db/migrations/001.py",
                     "schema.sql", ".env.local", "lib/billing.js"):
            with self.subTest(name=name):
                git(self.repo, "reset", "-q", "--hard", self.base)
                self.write(name, "# comment\n")
                self.commit()
                result = self.tier()
                self.assertEqual(result["tier"], "deep")
                self.assertIn("Haiku second reader", result["review"])

    def test_deep_beats_docs(self):
        self.write("README.md", "changed\n")
        self.write("install.sh", "echo hi\n")
        self.commit()
        self.assertEqual(self.tier()["tier"], "deep")

    def test_repo_override_file(self):
        self.write("pay/checkout.py", "x = 1\n")
        self.commit()
        self.assertEqual(self.tier()["tier"], "normal")
        self.write(".review-risk.json", json.dumps({"deep": ["pay/**"], "light": ["notes/**"]}))
        self.assertEqual(self.tier()["tier"], "deep")
        git(self.repo, "reset", "-q", "--hard", self.base)
        self.write("notes/a.dat", "text\n")
        self.write(".review-risk.json", json.dumps({"light": ["notes/**"]}))
        self.commit()
        # The override file itself is code, so it keeps the diff normal; the notes file alone is light.
        self.assertEqual(self.tier()["tier"], "normal")
        files = {"notes/a.dat": {"added": ["x"], "removed": [], "binary": False}}
        tier = stats.classify_risk(files, stats.RISK_DEEP_PATTERNS, ["notes/**"])[0]
        self.assertEqual(tier, "light")

    def test_uncommitted_change_counts(self):
        self.write("app.py", "x = 3\n# note\n")
        self.assertEqual(self.tier()["tier"], "normal")

    def test_untracked_file_counts(self):
        # A worker's new file is untracked when Claude reviews it.
        self.write("tools/new_tool.py", "print('hi')\n")
        result = self.tier()
        self.assertEqual(result["tier"], "normal")
        self.assertIn("tools/new_tool.py", result["files"])
        self.write("src/auth/login.py", "token = 1\n")
        self.assertEqual(self.tier()["tier"], "deep")

    def test_binary_is_normal(self):
        with open(os.path.join(self.repo, "logo.png"), "wb") as stream:
            stream.write(b"\x89PNG\x00\x01\x02\xff")
        self.commit()
        self.assertEqual(self.tier()["tier"], "normal")

    def test_text_output_and_reason_cap(self):
        for i in range(12):
            self.write("app{}.py".format(i), "x = {}\n".format(i))
        self.commit()
        lines = self.run_risk().splitlines()
        self.assertEqual(lines[0], "normal")
        self.assertEqual(len([l for l in lines if l.startswith("  code change")]), 10)
        self.assertIn("12 reasons in all", lines[11])
        self.assertIn("Claude reads the diff", lines[-1])

    def test_json_keys(self):
        self.assertEqual(sorted(self.tier()), ["files", "review", "tier", "reasons"][:0] or ["files", "reasons", "review", "tier"])

    def test_default_base_head_minus_one(self):
        self.write("README.md", "changed\n")
        self.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats.main(["risk", "--dir", self.repo, "--json"])
        result = json.loads(out.getvalue())
        self.assertEqual(result["tier"], "light")
        self.assertEqual(result["files"], ["README.md"])

    def test_catalog_has_review_risk(self):
        with open(os.path.join(ROOT, "arms.json"), encoding="utf-8") as stream:
            block = json.load(stream)["explore"]["review_risk"]
        self.assertEqual(block["deep"], stats.RISK_DEEP_PATTERNS)
        self.assertEqual(block["light"], stats.RISK_DOC_PATTERNS)


if __name__ == "__main__":
    unittest.main()
