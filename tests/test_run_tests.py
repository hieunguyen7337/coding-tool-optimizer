"""Tests for tools/run-tests.py. The tool name has a hyphen, so it is loaded by path."""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(ROOT, "tools", "run-tests.py")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


rt = load(RUNNER, "run_tests_tool")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


OK_LOG = """\
...
----------------------------------------------------------------------
Ran 45 tests in 12.345s

OK
"""

OK_SKIP_LOG = """\
Ran 10 tests in 0.010s

OK (skipped=3)
"""

OK_EXPECTED_LOG = """\
Ran 4 tests in 0.010s

OK (expected failures=2)
"""

FAIL_LOG = """\
======================================================================
FAIL: test_one (mod.T)
----------------------------------------------------------------------
Traceback (most recent call last):
AssertionError: no

======================================================================
ERROR: test_two (mod.T)
----------------------------------------------------------------------
Traceback (most recent call last):
RuntimeError: boom

----------------------------------------------------------------------
Ran 45 tests in 30.100s

FAILED (failures=2, errors=1, skipped=3)
"""

CRASH_LOG = """\
Traceback (most recent call last):
  File "test_x.py", line 1, in <module>
ImportError: missing
"""

PASS_MODULE = """\
import unittest


class PassTest(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(1 + 1, 2)
"""

FAIL_MODULE = """\
import unittest


class FailTest(unittest.TestCase):
    def test_bad(self):
        self.assertEqual(1, 2)
"""


class ChooseJobsTest(unittest.TestCase):
    def test_cpu_cap(self):
        # reserve = max(1536, 20000 // 10) = 2000
        # (10000 - 2000) // 400 = 20, capped by 3 cpus
        self.assertEqual(rt.choose_jobs(10000, 20000, 3, 400), 3)

    def test_floor_of_one(self):
        # reserve = max(1536, 20000 // 10) = 2000
        # (2000 - 2000) // 400 = 0 -> 1
        self.assertEqual(rt.choose_jobs(2000, 20000, 8, 400), 1)

    def test_below_reserve_is_one(self):
        # reserve = max(1536, 8000 // 10) = 1536
        # (100 - 1536) // 400 < 0 -> 1
        self.assertEqual(rt.choose_jobs(100, 8000, 4, 400), 1)

    def test_memory_bound(self):
        # reserve = max(1536, 10000 // 10) = 1536
        # (5000 - 1536) // 400 = 8, under the 16 cpu cap
        self.assertEqual(rt.choose_jobs(5000, 10000, 16, 400), 8)

    def test_per_job_changes_the_count(self):
        # reserve = 2000; (10000 - 2000) // 800 = 10
        self.assertEqual(rt.choose_jobs(10000, 20000, 32, 800), 10)

    def test_unknown_memory_uses_cpus(self):
        self.assertEqual(rt.choose_jobs(None, None, 6, 400), 6)
        self.assertEqual(rt.choose_jobs(None, None, None, 400), 1)


class OrderTest(unittest.TestCase):
    def test_longest_first_and_unknown_modules_by_size(self):
        modules = ["test_small.py", "test_big.py", "test_known.py", "test_tie_b.py", "test_tie_a.py"]
        times = {
            "test_known.py": {"seconds": 3, "runs": 1, "at": "t"},
            "test_tie_b.py": 4,
            "test_tie_a.py": {"seconds": 4},
        }
        sizes = {"test_small.py": 2000, "test_big.py": 20000, "test_known.py": 10 ** 9}
        # big = 20000/2000 = 10s, ties at 4s sort by name, known stays 3s, small = 1s
        self.assertEqual(
            rt.order_modules(modules, times, sizes),
            ["test_big.py", "test_tie_a.py", "test_tie_b.py", "test_known.py", "test_small.py"],
        )

    def test_full_record_is_accepted(self):
        record = {"version": 1, "modules": {"test_slow.py": {"seconds": 9}}}
        sizes = {"test_slow.py": 1, "test_fast.py": 100}
        self.assertEqual(
            rt.order_modules(["test_fast.py", "test_slow.py"], record, sizes),
            ["test_slow.py", "test_fast.py"],
        )


class ShardsTest(unittest.TestCase):
    def test_lpt_covers_every_module_once_and_balances(self):
        modules = ["m10", "m9", "m8", "m7", "m1b", "m1a"]
        times = {"m10": 10, "m9": 9, "m8": 8, "m7": 7, "m1a": 1, "m1b": 1}
        groups = rt.shards(modules, times, {}, 2)
        self.assertEqual(groups, [(18, ["m10", "m7", "m1a"]), (18, ["m9", "m8", "m1b"])])
        flat = [name for _, names in groups for name in names]
        self.assertEqual(sorted(flat), sorted(modules))
        self.assertEqual(len(flat), len(set(flat)))

    def test_no_empty_shard(self):
        groups = rt.shards(["a", "b"], {"a": 5, "b": 1}, {}, 4)
        self.assertEqual(len(groups), 2)
        flat = [name for _, names in groups for name in names]
        self.assertEqual(sorted(flat), ["a", "b"])
        self.assertEqual(len(flat), len(set(flat)))


class ParseResultTest(unittest.TestCase):
    def test_ok(self):
        parsed = rt.parse_result(OK_LOG)
        self.assertEqual(parsed, {"ok": True, "tests": 45, "failures": 0, "errors": 0, "skipped": 0})

    def test_ok_counts_skips_and_ignores_expected_failures(self):
        parsed = rt.parse_result(OK_SKIP_LOG)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["tests"], 10)
        self.assertEqual(parsed["skipped"], 3)
        self.assertEqual(parsed["failures"], 0)
        expected = rt.parse_result(OK_EXPECTED_LOG)
        self.assertTrue(expected["ok"])
        self.assertEqual(expected["failures"], 0)
        self.assertEqual(expected["errors"], 0)

    def test_failed_counts(self):
        parsed = rt.parse_result(FAIL_LOG)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["tests"], 45)
        self.assertEqual(parsed["failures"], 2)
        self.assertEqual(parsed["errors"], 1)
        self.assertEqual(parsed["skipped"], 3)
        blocks = rt.failure_blocks(FAIL_LOG)
        self.assertEqual(len(blocks), 2)
        self.assertIn("FAIL: test_one", blocks[0])
        self.assertIn("ERROR: test_two", blocks[1])

    def test_singular_ran_line(self):
        parsed = rt.parse_result("Ran 1 test in 0.001s\n\nOK\n")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["tests"], 1)

    def test_crash_without_ran_line_is_an_error(self):
        parsed = rt.parse_result(CRASH_LOG)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["tests"], 0)
        self.assertEqual(parsed["failures"], 0)
        self.assertEqual(parsed["errors"], 1)
        self.assertEqual(parsed["skipped"], 0)

    def test_module_and_summary_lines(self):
        ok = {
            "name": "test_x.py", "seconds": 12.34, "timed_out": False,
            "parsed": {"ok": True, "tests": 45, "failures": 0, "errors": 0, "skipped": 0},
        }
        self.assertEqual(rt.format_module_line(ok), "ok   test_x  12.3s  45 tests")
        bad = {
            "name": "test_x.py", "seconds": 30.14, "timed_out": False,
            "parsed": {"ok": False, "tests": 45, "failures": 2, "errors": 1, "skipped": 0},
        }
        self.assertEqual(
            rt.format_module_line(bad),
            "FAIL test_x  30.1s  45 tests (2 failures, 1 error)",
        )
        self.assertEqual(
            rt.format_module_line({"name": "test_x.py", "seconds": 1800, "timed_out": True, "parsed": {}}),
            "TIMEOUT test_x",
        )
        results = [
            {"seconds": 10.0, "timed_out": False,
             "parsed": {"ok": True, "tests": 45, "failures": 0, "errors": 0, "skipped": 0}},
            {"seconds": 5.0, "timed_out": False,
             "parsed": {"ok": False, "tests": 45, "failures": 2, "errors": 1, "skipped": 3}},
        ]
        self.assertEqual(
            rt.format_summary(results, 40.0, 4),
            "run-tests: 2 modules, 90 tests, 2 failures, 1 errors, 3 skipped "
            "in 40.0s wall (sum 15.0s, jobs 4)",
        )
        timed = [{"seconds": 1800.0, "timed_out": True,
                  "parsed": {"tests": 9, "failures": 0, "errors": 0, "skipped": 0}}]
        self.assertEqual(
            rt.format_summary(timed, 1800.0, 1),
            "run-tests: 1 modules, 0 tests, 0 failures, 1 errors, 0 skipped "
            "in 1800.0s wall (sum 1800.0s, jobs 1)",
        )


class TimesTest(unittest.TestCase):
    def test_average_skips_timeouts_and_keeps_other_modules(self):
        record = {"version": 1, "modules": {
            "test_x.py": {"seconds": 10, "runs": 3, "at": "old"},
            "test_keep.py": {"seconds": 7, "runs": 2, "at": "old"},
        }}
        updated = rt.update_times(record, [
            {"name": "test_x.py", "seconds": 20},
            {"name": "test_new", "seconds": 4},
            {"name": "test_slow.py", "seconds": 1800, "timed_out": True},
        ], "2026-11-16T00:00:00Z")
        self.assertEqual(updated["version"], 1)
        self.assertEqual(updated["modules"]["test_x.py"]["seconds"], 15)
        self.assertEqual(updated["modules"]["test_x.py"]["runs"], 4)
        self.assertEqual(updated["modules"]["test_x.py"]["at"], "2026-11-16T00:00:00Z")
        self.assertEqual(updated["modules"]["test_new.py"],
                         {"seconds": 4, "runs": 1, "at": "2026-11-16T00:00:00Z"})
        self.assertEqual(updated["modules"]["test_keep.py"]["seconds"], 7)
        self.assertEqual(updated["modules"]["test_keep.py"]["runs"], 2)
        self.assertNotIn("test_slow.py", updated["modules"])
        self.assertEqual(record["modules"]["test_x.py"]["runs"], 3)

    def test_timestamp_is_utc(self):
        when = datetime(2026, 11, 16, 1, 2, 3, tzinfo=timezone.utc)
        updated = rt.update_times({}, [{"name": "test_a.py", "seconds": 1.5}], when)
        self.assertEqual(updated["modules"]["test_a.py"]["at"], "2026-11-16T01:02:03Z")

    def test_atomic_write_and_unreadable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state", "test-times.json")
            first = rt.update_times(None, [{"name": "test_x.py", "seconds": 10}], "t0")
            rt.write_times(path, first)
            loaded = rt.load_times(path)
            self.assertEqual(loaded["modules"]["test_x.py"]["runs"], 1)
            again = rt.update_times(loaded, [{"name": "test_x.py", "seconds": 30}], "t1")
            rt.write_times(path, again)
            self.assertEqual(rt.load_times(path)["modules"]["test_x.py"]["seconds"], 20)
            self.assertEqual(rt.load_times(path)["modules"]["test_x.py"]["runs"], 2)

            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{")
            self.assertEqual(rt.load_times(path)["modules"], {})
            self.assertEqual(rt.load_times(os.path.join(tmp, "missing.json"))["modules"], {})
            self.assertEqual(rt.load_times(tmp)["modules"], {})

            rt.write_times(path, first)
            real_replace = rt.os.replace

            def boom(src, dst):
                raise OSError("disk full")

            rt.os.replace = boom
            try:
                with self.assertRaises(OSError):
                    rt.write_times(path, {"version": 1, "modules": {}})
            finally:
                rt.os.replace = real_replace
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle)["modules"]["test_x.py"]["seconds"], 10)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["test-times.json"])


class WindowsModulesTest(unittest.TestCase):
    def test_scan(self):
        sources = {
            "test_plain.py": "import os\nvalue = os.name\n",
            "test_nt.py": 'WINDOWS = os.name == "nt"\n',
            "test_win32.py": 'if sys.platform == "win32":\n    x = 1\n',
            "test_start.py": 'sys.platform.startswith("win")\n',
            "test_skip.py": 'unittest.skipUnless(os.name == "nt", "win")\n',
            "test_quote.py": "os.name == 'nt'\n",
        }
        self.assertEqual(
            rt.windows_modules(sources),
            ["test_nt.py", "test_win32.py", "test_start.py", "test_skip.py", "test_quote.py"],
        )


def _kill_pid(pid):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def copy_runner(root):
    tools = os.path.join(root, "tools")
    os.makedirs(tools, exist_ok=True)
    shutil.copyfile(RUNNER, os.path.join(tools, "run-tests.py"))


def run_script(root, args, timeout):
    script = os.path.join(root, "tools", "run-tests.py")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", script, *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_pid(proc.pid)
        stdout, stderr = proc.communicate()
        raise AssertionError("run-tests timed out\n" + stdout.decode("utf-8", "replace"))
    return (
        proc.returncode,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


class EndToEndTest(unittest.TestCase):
    def test_pass_and_fail_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy_runner(tmp)
            write(os.path.join(tmp, "tests", "test_pass.py"), PASS_MODULE)
            write(os.path.join(tmp, "tests", "test_fail.py"), FAIL_MODULE)
            times = os.path.join(tmp, "times.json")
            code, stdout, stderr = run_script(
                tmp, ["--jobs", "2", "--timeout", "60", "--times", times], timeout=90,
            )
            self.assertEqual(code, 1, stdout + "\n" + stderr)
            self.assertTrue(stdout.splitlines()[0].startswith("jobs 2: --jobs"))
            self.assertIn("ok   test_pass", stdout)
            self.assertIn("FAIL test_fail", stdout)
            self.assertIn("(1 failure)", stdout)
            self.assertIn("AssertionError", stdout)
            last = [line for line in stdout.splitlines() if line.strip()][-1]
            self.assertRegex(
                last,
                r"^run-tests: 2 modules, 2 tests, 1 failures, 0 errors, 0 skipped "
                r"in \d+\.\d+s wall \(sum \d+\.\d+s, jobs 2\)$",
            )
            with open(times, encoding="utf-8") as handle:
                saved = json.loads(handle.read())
            self.assertEqual(saved["version"], 1)
            self.assertEqual(saved["modules"]["test_pass.py"]["runs"], 1)
            self.assertEqual(saved["modules"]["test_fail.py"]["runs"], 1)

    def test_print_shards_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy_runner(tmp)
            for name in ("test_a.py", "test_b.py", "test_c.py"):
                write(os.path.join(tmp, "tests", name), "x = 1\n")
            times = os.path.join(tmp, "times.json")
            record = {"version": 1, "modules": {
                "test_a.py": {"seconds": 10, "runs": 1, "at": "t"},
                "test_b.py": {"seconds": 9, "runs": 1, "at": "t"},
                "test_c.py": {"seconds": 1, "runs": 1, "at": "t"},
            }}
            write(times, json.dumps(record))
            code, stdout, stderr = run_script(tmp, ["--print-shards", "2", "--times", times], timeout=30)
            self.assertEqual(code, 0, stdout + "\n" + stderr)
            lines = [line for line in stdout.splitlines() if line.strip()]
            self.assertEqual(lines, ["10 test_a.py", "10 test_b.py test_c.py"])


def git(root, *args):
    subprocess.run(
        ["git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class ChangedSelectionTests(unittest.TestCase):
    def make_repo(self, tmp):
        git(tmp, "init", "-q", "-b", "master")
        write(os.path.join(tmp, "tests", "test_a.py"), 'import fake_x\nX = "bin/tool-a"\n')
        write(os.path.join(tmp, "tests", "test_b.py"), 'Y = "tools/helper.py"\nZ = "README.md"\n')
        write(os.path.join(tmp, "tests", "test_c.py"), 'W = "fixtures/data.txt"\n')
        write(os.path.join(tmp, "tests", "fake_x.py"), "a = 1\n")
        write(os.path.join(tmp, "bin", "tool-a"), "a\n")
        write(os.path.join(tmp, "bin", "tool"), "a\n")
        write(os.path.join(tmp, "tools", "helper.py"), "a\n")
        write(os.path.join(tmp, "tests", "fixtures", "data.txt"), "a\n")
        write(os.path.join(tmp, "README.md"), "a\n")
        write(os.path.join(tmp, "NOTES.md"), "a\n")
        write(os.path.join(tmp, "install.sh"), "a\n")
        copy_runner(tmp)
        git(tmp, "add", "-A")
        git(tmp, "commit", "-q", "-m", "base")
        git(tmp, "branch", "base")

    def selected(self, tmp, *extra):
        git(tmp, "add", "-A")
        code, out, err = run_script(tmp, ["--changed", "base", "--print-selected", *extra], timeout=60)
        self.assertEqual(code, 0, out + err)
        return out

    def commit_change(self, tmp, rel):
        write(os.path.join(tmp, rel), "changed\n")
        git(tmp, "add", "-A")
        git(tmp, "commit", "-q", "-m", "change " + rel)

    def run_case(self, rel):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_repo(tmp)
            self.commit_change(tmp, rel)
            return self.selected(tmp)

    def test_test_module_selects_itself(self):
        out = self.run_case("tests/test_c.py")
        self.assertEqual(out.strip(), "test_c.py  tests/test_c.py changed")

    def test_fake_and_fixture_select_mentioning_modules(self):
        self.assertEqual(self.run_case("tests/fake_x.py").split()[0], "test_a.py")
        out = self.run_case("tests/fixtures/data.txt")
        self.assertTrue(out.startswith("test_c.py  test_c.py mentions data.txt"), out)
        self.assertNotIn("test_a.py", out)

    def test_bin_and_tools_select_mentioning_modules(self):
        out = self.run_case("bin/tool-a")
        self.assertIn("test_a.py  test_a.py mentions tool-a", out)
        self.assertNotIn("whole suite", out)
        out = self.run_case("tools/helper.py")
        self.assertEqual(out.strip(), "test_b.py  test_b.py mentions helper")
        # "tool" must not match "tool-a" (simple boundary rule), so bin/tool maps to nothing: whole suite.
        out = self.run_case("bin/tool")
        self.assertIn("whole suite: bin/tool changed", out)

    def test_docs_select_by_basename_else_nothing(self):
        self.assertIn("test_b.py  test_b.py mentions README.md", self.run_case("README.md"))
        out = self.run_case("NOTES.md")
        self.assertEqual(out.strip(), "no test module selected")

    def test_unmapped_file_selects_whole_suite_and_names_it(self):
        out = self.run_case("install.sh")
        self.assertIn("whole suite: install.sh changed and maps to no test module", out)
        for name in ("test_a.py", "test_b.py", "test_c.py"):
            self.assertIn(name + "  install.sh changed", out)

    def test_uncommitted_and_untracked_files_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_repo(tmp)
            write(os.path.join(tmp, "tools", "helper.py"), "dirty\n")
            write(os.path.join(tmp, "tests", "test_new.py"), "x = 1\n")
            code, out, err = run_script(tmp, ["--changed", "base", "--print-selected"], timeout=60)
            self.assertEqual(code, 0, out + err)
            self.assertIn("test_b.py", out)
            self.assertIn("test_new.py  tests/test_new.py changed", out)

    def test_default_base_prefers_origin_master_then_head_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_repo(tmp)
            self.commit_change(tmp, "tests/test_c.py")
            self.commit_change(tmp, "tools/helper.py")
            # No origin: HEAD~1 -> only the last commit.
            code, out, err = run_script(tmp, ["--changed", "--print-selected"], timeout=60)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(out.strip().splitlines()[0].split()[0], "test_b.py")
            self.assertNotIn("test_c.py  tests/test_c.py", out)
            # origin/master at the first commit: both commits count.
            git(tmp, "update-ref", "refs/remotes/origin/master", "base")
            code, out, err = run_script(tmp, ["--changed", "--print-selected"], timeout=60)
            self.assertEqual(code, 0, out + err)
            self.assertIn("test_c.py  tests/test_c.py changed", out)
            self.assertIn("test_b.py", out)

    def test_changed_runs_selected_modules_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            git(tmp, "init", "-q", "-b", "master")
            copy_runner(tmp)
            write(os.path.join(tmp, "tests", "test_p.py"), OK_TEST)
            write(os.path.join(tmp, "tests", "test_q.py"), OK_TEST)
            git(tmp, "add", "-A")
            git(tmp, "commit", "-q", "-m", "base")
            git(tmp, "branch", "base")
            write(os.path.join(tmp, "tests", "test_q.py"), OK_TEST + "\n")
            times = os.path.join(tmp, "times.json")
            code, out, err = run_script(
                tmp, ["--changed", "base", "--jobs", "1", "--times", times, "--timeout", "60"], timeout=90,
            )
            self.assertEqual(code, 0, out + err)
            self.assertIn("test_q", out)
            self.assertNotIn("test_p", out)
            self.assertTrue(os.path.exists(times))

    def test_print_selected_needs_changed(self):
        code, out, err = run_script(ROOT, ["--print-selected"], timeout=30)
        self.assertEqual(code, 2)


OK_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n\n\nif __name__ == '__main__':\n    unittest.main()\n"


if __name__ == "__main__":
    unittest.main()
