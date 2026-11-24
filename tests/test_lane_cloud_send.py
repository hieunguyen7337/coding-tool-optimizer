import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import time
import unittest
import uuid
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "bin", "lane")

FAKE_CLAUDE = '''
    import json
    import os
    import sys
    import time

    args = sys.argv[1:]
    names = ("CLAUDECODE", "CLAUDE_CODE_FOO", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_DISABLE_AUTO_MEMORY")
    with open(os.environ["FAKE_SEND_LOG"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"argv": args, "env": {name: os.environ.get(name) for name in names}}) + "\\n")
    mode = os.environ.get("FAKE_SEND_MODE", "ok")
    session = args[args.index("--cloud") + 1] if "--cloud" in args else "session_unknown"
    if mode == "ok":
        print(json.dumps({"ok": True, "session_id": session, "url": "https://claude.ai/code/" + session}))
    elif mode == "refused":
        print(json.dumps({"ok": False, "session_id": session, "error": "fake refusal"}))
    elif mode == "error":
        sys.stderr.write("fake claude failed: no auth\\n")
        sys.exit(3)
    elif mode == "nojson":
        print("Message queued, probably.")
    elif mode == "sleep":
        sys.stdout.flush()
        time.sleep(float(os.environ.get("FAKE_SEND_SLEEP", "5")))
        print(json.dumps({"ok": True, "session_id": session}))
'''


def force_rmtree(path):
    def retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


class LaneCloudSendTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.join(os.path.dirname(__file__),
                                 ".lane-cloud-send-{}-{}".format(os.getpid(), uuid.uuid4().hex))
        os.makedirs(self.root)
        self.addCleanup(force_rmtree, self.root)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in list(os.environ):
            if name.upper() == "CLAUDECODE" or name.upper().startswith("CLAUDE_CODE_"):
                os.environ.pop(name, None)
        os.environ.pop("USAGE_OPTIMIZER_SESSION_GUARD", None)
        os.environ.pop("LANES_CLAUDE_BIN", None)
        self.bin = os.path.join(self.root, "bin")
        self.state = os.path.join(self.root, "state")
        self.config = os.path.join(self.root, "config")
        self.log = os.path.join(self.root, "send-log.jsonl")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.state, "runs"))
        os.makedirs(os.path.join(self.config, "bin"))
        self.launcher = self.install_cli("claude", FAKE_CLAUDE)
        self.env = os.environ.copy()
        self.env.update({
            "LANES_STATE": self.state,
            "LANES_CONFIG": self.config,
            "LANES_CLAUDE_BIN": self.launcher,
            "FAKE_SEND_LOG": self.log,
            "FAKE_SEND_MODE": "ok",
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
        })

    def write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(textwrap.dedent(text))

    def install_cli(self, name, body):
        script = os.path.join(self.bin, "fake_{}.py".format(name))
        self.write(script, body)
        if os.name == "nt":
            launcher = os.path.join(self.bin, name + ".cmd")
            self.write(launcher, '@echo off\n"{}" "{}" %*\n'.format(sys.executable, script))
        else:
            launcher = os.path.join(self.bin, name)
            self.write(launcher, "#!/bin/sh\nexec {} {} \"$@\"\n".format(shlex.quote(sys.executable),
                                                                               shlex.quote(script)))
            os.chmod(launcher, 0o755)
        return launcher

    def make_run(self, run_id, started_at="2026-11-20T10:00:00Z", kind="claude", session_id="session_X",
                 **extra):
        cloud = {"kind": kind}
        if session_id is not None:
            cloud["session_id"] = session_id
            cloud["url"] = "https://claude.ai/code/" + session_id
        meta = {"started_at": started_at, "place": "cloud", "cloud": cloud}
        meta.update(extra)
        run_dir = os.path.join(self.state, "runs", run_id)
        os.makedirs(run_dir)
        path = os.path.join(run_dir, "meta.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(meta, stream, indent=2, sort_keys=True)
        return path

    def read_meta(self, path):
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def send(self, *args, **env):
        child_env = dict(self.env)
        child_env.update(env)
        result = subprocess.run([sys.executable, SCRIPT, "cloud-send"] + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=child_env, timeout=30, check=False)
        return result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace")

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, "r", encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def lane_module(self):
        loader = importlib.machinery.SourceFileLoader("lane_cloud_send_tested", SCRIPT)
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(module)
        return module

    # 1
    def test_send_by_run_id_passes_text_and_records(self):
        path = self.make_run("run-a", verdict="DONE", lane="opus")
        text = "hello\nsecond line"
        code, out, err = self.send("run-a", "-m", text)
        self.assertEqual(code, 0, err)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["argv"], ["-p", text, "--cloud", "session_X", "--output-format", "json"])
        self.assertIn("ok: true", out)
        self.assertIn("url: https://claude.ai/code/session_X", out)
        self.assertNotIn("second line", out)
        self.assertNotIn("second line", err)
        meta = self.read_meta(path)
        sent = meta["cloud"]["sent"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(sent[0]), {"at", "bytes", "ok", "first_line"})
        self.assertEqual(sent[0]["bytes"], len(text.encode("utf-8")))
        self.assertIs(sent[0]["ok"], True)
        self.assertEqual(sent[0]["first_line"], "hello")
        self.assertEqual(meta["verdict"], "DONE")
        self.assertEqual(meta["lane"], "opus")
        self.assertEqual(meta["place"], "cloud")
        self.assertEqual(meta["started_at"], "2026-11-20T10:00:00Z")
        self.assertEqual(meta["cloud"]["kind"], "claude")
        self.assertEqual(meta["cloud"]["url"], "https://claude.ai/code/session_X")
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_run_folder_path_is_accepted(self):
        path = self.make_run("run-folder")
        code, out, err = self.send(os.path.dirname(path), "-m", "by folder")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[0]["argv"][3], "session_X")
        self.assertEqual(len(self.read_meta(path)["cloud"]["sent"]), 1)

    # 2
    def test_long_first_line_cut_and_file_and_append(self):
        path = self.make_run("run-b")
        long_line = "x" * 200
        code, _, err = self.send("run-b", "-m", "\n  \n" + long_line + "\nmore")
        self.assertEqual(code, 0, err)
        message = os.path.join(self.root, "message.txt")
        file_text = "Grüße aus München — ü\nzweite Zeile\n"
        with open(message, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(file_text)
        code, out, err = self.send("run-b", "-f", message)
        self.assertEqual(code, 0, err)
        self.assertNotIn("zweite", out + err)
        calls = self.calls()
        self.assertEqual(calls[1]["argv"], ["-p", file_text, "--cloud", "session_X", "--output-format", "json"])
        sent = self.read_meta(path)["cloud"]["sent"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["first_line"], "x" * 80)
        self.assertEqual(sent[1]["first_line"], "Grüße aus München — ü")
        self.assertEqual(sent[1]["bytes"], len(file_text.encode("utf-8")))
        self.assertGreater(sent[1]["bytes"], len(file_text))
        self.assertTrue(all(entry["ok"] is True for entry in sent))

    # 3
    def test_last_picks_newest_claude_run_with_session(self):
        older = self.make_run("run-old", started_at="2026-11-20T09:00:00Z", session_id="session_Old")
        newest = self.make_run("run-new", started_at="2026-11-20T11:00:00Z", session_id="session_New")
        codex = self.make_run("run-codex", started_at="2026-11-20T12:00:00Z", kind="codex", session_id=None)
        codex_with_id = self.make_run("run-codex2", started_at="2026-11-20T12:30:00Z", kind="codex",
                                      session_id="session_Codex")
        no_session = self.make_run("run-nosession", started_at="2026-11-20T13:00:00Z", session_id=None)
        code, out, err = self.send("last", "-m", "to the newest")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[0]["argv"][3], "session_New")
        self.assertIn("url: https://claude.ai/code/session_New", out)
        self.assertEqual(len(self.read_meta(newest)["cloud"]["sent"]), 1)
        for path in (older, codex, codex_with_id, no_session):
            self.assertNotIn("sent", self.read_meta(path)["cloud"])

    def test_last_without_cloud_runs_is_usage_error(self):
        self.make_run("run-codex", kind="codex", session_id=None)
        code, _, err = self.send("last", "-m", "nobody")
        self.assertEqual(code, 2)
        self.assertIn("no Claude cloud run", err)
        self.assertEqual(self.calls(), [])

    # 4
    def test_bare_session_id_records_in_matching_run(self):
        other = self.make_run("run-other", session_id="session_X")
        path = self.make_run("run-abc", started_at="2026-11-01T00:00:00Z", session_id="session_abc")
        code, out, err = self.send("session_abc", "-m", "direct")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[0]["argv"][3], "session_abc")
        self.assertIn("url: https://claude.ai/code/session_abc", out)
        self.assertEqual(len(self.read_meta(path)["cloud"]["sent"]), 1)
        self.assertNotIn("sent", self.read_meta(other)["cloud"])

    def test_bare_session_id_without_run_writes_nothing(self):
        path = self.make_run("run-x", session_id="session_X")
        before = sorted(os.listdir(os.path.join(self.state, "runs")))
        with open(path, "rb") as stream:
            meta_before = stream.read()
        code, out, err = self.send("session_abc", "-m", "orphan")
        self.assertEqual(code, 0, err)
        self.assertIn("ok: true", out)
        self.assertEqual(self.calls()[0]["argv"][3], "session_abc")
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "runs"))), before)
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), meta_before)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["meta.json"])

    # 5
    def test_cse_id_accepted(self):
        code, out, err = self.send("cse_01AbC-d_9", "-m", "hi")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls()[0]["argv"], ["-p", "hi", "--cloud", "cse_01AbC-d_9", "--output-format", "json"])
        self.assertIn("ok: true", out)

    # 6
    def test_refused_answer_exits_1_and_records_false(self):
        path = self.make_run("run-r")
        code, out, err = self.send("run-r", "-m", "nope", FAKE_SEND_MODE="refused")
        self.assertEqual(code, 1)
        self.assertIn("ok: false", out)
        self.assertIn("url: -", out)
        self.assertIn("not sent to session_X", err)
        sent = self.read_meta(path)["cloud"]["sent"]
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0]["ok"], False)

    def test_nonzero_exit_is_failure(self):
        path = self.make_run("run-e")
        code, out, err = self.send("run-e", "-m", "nope", FAKE_SEND_MODE="error")
        self.assertEqual(code, 1)
        self.assertIn("ok: false", out)
        self.assertIn("claude exited 3", err)
        self.assertIn("no auth", err)
        self.assertIs(self.read_meta(path)["cloud"]["sent"][0]["ok"], False)

    def test_no_json_is_failure(self):
        path = self.make_run("run-n")
        code, out, err = self.send("run-n", "-m", "nope", FAKE_SEND_MODE="nojson")
        self.assertEqual(code, 1)
        self.assertIn("ok: false", out)
        self.assertIn("no JSON", err)
        self.assertIs(self.read_meta(path)["cloud"]["sent"][0]["ok"], False)

    # 7
    def test_usage_errors_exit_2_without_starting_claude(self):
        self.make_run("run-u")
        self.make_run("run-codex", kind="codex", session_id=None)
        self.make_run("run-claude-noid", session_id=None)
        missing = os.path.join(self.root, "no-such-file.txt")
        cases = [
            ("run-u",),
            ("run-u", "-m", "a", "-f", missing),
            ("run-u", "-m", ""),
            ("run-u", "-m", "  \n\t "),
            ("run-nope", "-m", "hi"),
            ("run-codex", "-m", "hi"),
            ("run-claude-noid", "-m", "hi"),
            ("run-u", "-f", missing),
        ]
        for args in cases:
            with self.subTest(args=args):
                code, _, err = self.send(*args)
                self.assertEqual(code, 2, err)
                self.assertTrue(err.strip())
        self.assertEqual(self.calls(), [])
        self.assertNotIn("sent", self.read_meta(os.path.join(self.state, "runs", "run-u", "meta.json"))["cloud"])

    # 8
    def test_child_env_drops_session_markers(self):
        self.make_run("run-env")
        code, _, err = self.send("run-env", "-m", "env check", CLAUDECODE="1", CLAUDE_CODE_FOO="bar",
                                 CLAUDE_CODE_OAUTH_TOKEN="fake-token")
        self.assertEqual(code, 0, err)
        env = self.calls()[0]["env"]
        self.assertIsNone(env["CLAUDECODE"])
        self.assertIsNone(env["CLAUDE_CODE_FOO"])
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "fake-token")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")

    # 9
    def test_write_meta_keeps_sent_entries_on_disk(self):
        lane = self.lane_module()
        entries = [{"at": "2026-11-20T10:00:00Z", "bytes": 5, "ok": True, "first_line": "hello"},
                   {"at": "2026-11-20T10:01:00Z", "bytes": 3, "ok": False, "first_line": "bye"}]
        path = self.make_run("run-race")
        on_disk = self.read_meta(path)
        on_disk["cloud"]["sent"] = entries
        lane.write_meta(path, on_disk)
        # The live run's own copy, read before the follow-ups were sent.
        held = {"started_at": "2026-11-20T10:00:00Z", "place": "cloud", "verdict": "DONE",
                "cloud": {"kind": "claude", "session_id": "session_X", "result_sha": "abc"}}
        lane.write_meta(path, held)
        meta = self.read_meta(path)
        self.assertEqual(meta["cloud"]["sent"], entries)
        self.assertEqual(meta["verdict"], "DONE")
        self.assertEqual(meta["cloud"]["result_sha"], "abc")
        self.assertFalse(os.path.exists(path + ".tmp"))
        # A copy that already holds one of them does not duplicate it.
        held_some = {"place": "cloud", "cloud": {"kind": "claude", "session_id": "session_X",
                                                  "sent": [entries[0]]}}
        lane.write_meta(path, held_some)
        self.assertEqual(self.read_meta(path)["cloud"]["sent"], entries)

    def test_write_meta_non_cloud_unchanged(self):
        lane = self.lane_module()
        path = self.make_run("run-local")
        on_disk = self.read_meta(path)
        on_disk["cloud"]["sent"] = [{"at": "x", "bytes": 1, "ok": True, "first_line": "y"}]
        lane.write_meta(path, on_disk)
        local = {"started_at": "2026-11-20T10:00:00Z", "place": "local", "verdict": "DONE"}
        lane.write_meta(path, dict(local))
        self.assertEqual(self.read_meta(path), local)
        new_path = os.path.join(self.root, "fresh-meta.json")
        lane.write_meta(new_path, {"place": "cloud", "cloud": {"kind": "claude"}})
        self.assertEqual(self.read_meta(new_path), {"place": "cloud", "cloud": {"kind": "claude"}})

    # 10
    def test_timeout_returns_1_and_records_false(self):
        lane = self.lane_module()
        path = self.make_run("run-slow")
        env = {"LANES_STATE": self.state, "LANES_CONFIG": self.config, "LANES_CLAUDE_BIN": self.launcher,
               "FAKE_SEND_LOG": self.log, "FAKE_SEND_MODE": "sleep", "FAKE_SEND_SLEEP": "10"}
        out = io.StringIO()
        err = io.StringIO()
        started = time.monotonic()
        with mock.patch.dict(os.environ, env), mock.patch.object(lane, "CLOUD_SEND_TIMEOUT", 0.7), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lane.run_cloud_send(["run-slow", "-m", "slow one"])
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(code, 1)
        self.assertIn("ok: false", out.getvalue())
        self.assertIn("did not answer", err.getvalue())
        sent = self.read_meta(path)["cloud"]["sent"]
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0]["ok"], False)
        self.assertEqual(sent[0]["first_line"], "slow one")


if __name__ == "__main__":
    unittest.main()
