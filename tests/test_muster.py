"""Self-check for skills/muster/muster.py: usage math and a full run with a fake claude worker.
Run: python3 -m unittest discover -s tests"""
import contextlib, datetime, importlib.util, io, threading, time, unittest.mock, json, os, pathlib, re, stat, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("muster", ROOT / "skills/muster/muster.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
PRICES = json.loads((ROOT / "prices.json").read_text())


class TaskId(unittest.TestCase):
    def test_slug_rule(self):
        for ok in ("f191", "fix-pr-assoc-wait", "f44-r2", "a1-b2-c3"):
            self.assertTrue(M.TASK_ID.match(ok), ok)
        for bad in ("FIX-A", "f191_workflow", "-lead", "trail-", "double--dash", "9lives", ""):
            self.assertFalse(M.TASK_ID.match(bad), bad)


class Usage(unittest.TestCase):
    def test_codex_rollout_dedupes_and_prices(self):
        with tempfile.TemporaryDirectory() as d:
            home, task = pathlib.Path(d, "home"), pathlib.Path(d, "task")
            (home / "sessions/2026/11/05").mkdir(parents=True)
            task.mkdir()
            (task / "log.txt").write_text("session id: abc-123\n")
            ev = lambda tot, inp, cached, out: json.dumps({"payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": tot},
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}}}})
            lines = [json.dumps({"payload": {"model": "gpt-6-astra"}}),
                     ev(1100, 1000, 500, 100), ev(1100, 1000, 500, 100),  # duplicate emission
                     ev(2200, 1000, 0, 100)]
            (home / "sessions/2026/11/05/rollout-x-abc-123.jsonl").write_text("\n".join(lines))
            u = M.usage_codex(str(task), str(home), PRICES, "subscription")
            self.assertEqual((u["input"], u["cached"], u["output"]), (2000, 500, 200))
            # 1500 uncached * $10 + 500 cached * $1 + 200 out * $50, per 1M
            self.assertAlmostEqual(u["usd"], (1500 * 10 + 500 * 1 + 200 * 50) / 1e6, places=6)

    def test_peak_doubles_deepseek(self):
        p = PRICES["deepseek-flash"]
        thu_peak = datetime.datetime(2026, 11, 5, 7, 0, tzinfo=datetime.timezone.utc)
        thu_off = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.timezone.utc)
        sat = datetime.datetime(2026, 11, 7, 7, 0, tzinfo=datetime.timezone.utc)
        self.assertTrue(M.is_peak(p, thu_peak))
        self.assertFalse(M.is_peak(p, thu_off))
        self.assertFalse(M.is_peak(p, sat))


class Run(unittest.TestCase):
    def test_claude_worker_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            fake = pathlib.Path(d, "fake-claude")
            fake.write_text("#!/bin/sh\ncat >/dev/null\n"
                            "echo '{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"working\"}]}}'\n"
                            "echo '{\"type\":\"result\",\"result\":\"DONE\",\"modelUsage\":{\"deepseek-flash\":"
                            "{\"inputTokens\":1000000,\"outputTokens\":1000000,\"cacheReadInputTokens\":0}}}'\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            task = pathlib.Path(d, "run/A")
            task.mkdir(parents=True)
            (task / "prompt.md").write_text("# t\nsay DONE\n")
            cfg = {"runsDir": d, "workers": {"ds": {"type": "claude", "command": str(fake), "billing": "metered"}}}
            self.assertEqual(M.run(cfg, "ds", str(task), d), 0)
            self.assertEqual((task / "status").read_text().strip(), "done")
            self.assertEqual((task / "result.md").read_text(), "DONE")
            meta = M.read_meta(str(task))
            self.assertEqual((meta["worker"], meta["type"], meta["exit"]), ("ds", "claude", "0"))
            u = json.loads((task / "usage").read_text())
            self.assertEqual((u["input"], u["output"], u["billing"]), (1000000, 1000000, "metered"))
            self.assertIn(u["usd"], (0.75, 1.5))  # off-peak or peak, by when the test runs


    def test_changes_diff_holds_commits_edits_and_new_files(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            repo = pathlib.Path(d, "repo")
            repo.mkdir()
            g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                          check=True, capture_output=True)
            g("init", "-q")
            (repo / "kept.txt").write_text("one\n")
            (repo / "edited.txt").write_text("old\n")
            g("add", ".")
            g("commit", "-qm", "base")
            fake = pathlib.Path(d, "fake-claude")
            fake.write_text("#!/bin/sh\ncat >/dev/null\n"
                            "echo two >> kept.txt && git -c user.name=t -c user.email=t@t commit -qam work\n"
                            "echo new > edited.txt\necho hi > added.txt\n"
                            "echo '{\"type\":\"result\",\"result\":\"Status: DONE\"}'\n")
            fake.chmod(0o755)
            task = pathlib.Path(d, "run/A")
            task.mkdir(parents=True)
            (task / "prompt.md").write_text("# t\n")
            cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": str(fake)}}}
            self.assertEqual(M.run(cfg, "w", str(task), str(repo)), 0)
            diff = (task / "changes.diff").read_text()
            for needle in ("+two", "-old", "+new", "+hi", "added.txt"):
                self.assertIn(needle, diff)
            meta = M.read_meta(str(task))
            self.assertEqual(len(meta["base"]), 40)
            self.assertEqual(meta["changes"], "3 files +3 -1")
            self.assertEqual(len(meta["commits"].split()), 1)  # the worker's own commit
            self.assertEqual(meta["changes_sha256"], __import__("hashlib").sha256((task / "changes.diff").read_bytes()).hexdigest())
            self.assertEqual(subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                                            capture_output=True, text=True).stdout, "")  # index untouched

    def git_repo(self, d, files):
        import subprocess
        repo = pathlib.Path(d, "repo")
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                      check=True, capture_output=True)
        g("init", "-q")
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        g("add", ".")
        g("commit", "-qm", "base")
        return repo

    def run_script(self, d, repo, script, cwd=None, check=None):
        fake = pathlib.Path(d, "fake-claude")
        fake.write_text("#!/bin/sh\ncat >/dev/null\n" + script + "\necho '{\"type\":\"result\",\"result\":\"Status: DONE\"}'\n")
        fake.chmod(0o755)
        task = pathlib.Path(d, "run/a")
        task.mkdir(parents=True)
        (task / "prompt.md").write_text("# t\n")
        cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": str(fake)}}}
        rc = M.run(cfg, "w", str(task), str(cwd or repo), check=check)
        return rc, task

    def test_changes_diff_leaves_out_what_was_already_uncommitted(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n", "b.txt": "b\n"})
            (repo / "a.txt").write_text("users own edit\n")
            (repo / "old-untracked.txt").write_text("was here\n")
            rc, task = self.run_script(d, repo, "echo worker >> b.txt")
            self.assertEqual(rc, 0)
            diff = (task / "changes.diff").read_text()
            self.assertIn("+worker", diff)
            self.assertNotIn("users own edit", diff)
            self.assertNotIn("old-untracked", diff)
            self.assertEqual(M.read_meta(str(task))["changes"], "1 files +1 -0")

    def test_changes_diff_from_a_subdirectory_covers_the_whole_repo(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"sub/q.sql": "-- old comment\nselect 1;\n"})
            rc, task = self.run_script(d, repo, "printf 'select 1;\\n' > q.sql\necho top > ../top.txt\n"
                                       "echo s > 'sp ace.txt'\necho '++ x' > -dash", cwd=repo / "sub")
            self.assertEqual(rc, 0)
            diff = (task / "changes.diff").read_text()
            for path in ("a/sub/q.sql", "b/top.txt", "b/sub/sp ace.txt", "b/sub/-dash"):
                self.assertIn(path, diff)
            self.assertEqual(M.read_meta(str(task))["changes"], "4 files +3 -1")  # `-- old` and `++ x` are lines too

    def test_check_runs_after_the_worker_and_its_exit_code_decides(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            rc, task = self.run_script(d, repo, "echo made > marker.txt", check="echo checking; test -f marker.txt")
            self.assertEqual((rc, M.read_meta(str(task))["check"]), (0, "pass"))
            self.assertIn("checking", (task / "check.txt").read_text())
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            rc, task = self.run_script(d, repo, "true", check="echo 'Error: 2 tests failed'; exit 3")
            self.assertEqual(rc, 3)
            self.assertEqual(M.read_meta(str(task))["check"], "fail (exit 3)")
            status = (task / "status").read_text()
            self.assertTrue(status.startswith("failed: done check failed (exit 3)"), status)
            self.assertIn("2 tests failed", status)

    def test_tests_touched_names_test_files_and_what_the_check_runs(self):
        names = ["src/app.ts", "tests/runtime/a.test.mjs", "pkg/x_test.go", "Makefile", "scripts/check.sh",
                 "Package/Tests/FooTests/FooTests.swift", "docs/testing.md", "src/latest.ts"]
        self.assertEqual(M.tests_touched(names, "make verify && ./scripts/check.sh"),
                         ["tests/runtime/a.test.mjs", "pkg/x_test.go", "Makefile", "scripts/check.sh",
                          "Package/Tests/FooTests/FooTests.swift"])
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"src/a.py": "x = 1\n", "tests/test_a.py": "assert True\n"})
            rc, task = self.run_script(d, repo, "echo 'x = 2' > src/a.py\necho '' > tests/test_a.py", check="true")
            self.assertEqual(M.read_meta(str(task))["tests_touched"], "tests/test_a.py")

    def test_edits_hidden_by_assume_unchanged_still_count(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            subprocess.run(["git", "-C", str(repo), "update-index", "--assume-unchanged", "a.txt"], check=True)
            rc, task = self.run_script(d, repo, "echo changed > a.txt")
            self.assertIn("+changed", (task / "changes.diff").read_text())

    def test_dirty_submodule_is_named_as_missing(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            child = self.git_repo(d, {"c.txt": "c\n"})
            child = child.rename(pathlib.Path(d, "child-src"))
            repo = self.git_repo(d, {"a.txt": "a\n"})
            g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "-c", "user.name=t",
                                           "-c", "user.email=t@t", *a], check=True, capture_output=True)
            g("submodule", "add", "-q", str(child), "child")
            g("commit", "-qm", "add child")
            rc, task = self.run_script(d, repo, "echo edit >> child/c.txt")
            meta = M.read_meta(str(task))
            self.assertEqual(meta["changes_missing"], "uncommitted edits inside submodules child")

    def test_a_failing_git_diff_marks_changes_unavailable(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            real = subprocess_popen = __import__("subprocess").Popen
            def popen(cmd, *a, **k):  # the streamed diff fails; everything else runs
                if cmd[:1] == ["git"] and "--no-textconv" in cmd:
                    cmd = ["sh", "-c", "echo partial; exit 2"]
                return real(cmd, *a, **k)
            with unittest.mock.patch.object(M.subprocess, "Popen", side_effect=popen):
                rc, task = self.run_script(d, repo, "echo x >> a.txt")
            self.assertEqual(M.read_meta(str(task))["changes"], "unavailable: git diff failed")
            self.assertFalse((task / "changes.diff").exists())

    def test_tests_touched_keeps_hidden_names_and_reads_absolute_paths(self):
        self.assertEqual(M.tests_touched([".check.sh", "x/run.sh"], "./.check.sh && /r/x/run.sh", "/r"), [".check.sh", "x/run.sh"])

    def test_sigterm_during_the_check_stops_its_children_and_keeps_usage(self):
        import fcntl, signal, subprocess, time as t
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            fake = pathlib.Path(d, "fake-claude")
            fake.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"result\",\"result\":\"worker done\",\"modelUsage\":{}}'\n")
            fake.chmod(0o755)
            cfg = pathlib.Path(d, "config.json")
            cfg.write_text(json.dumps({"runsDir": d, "workers": {"w": {"type": "claude", "command": str(fake)}}}))
            task = pathlib.Path(d, "run/a")
            task.mkdir(parents=True)
            (task / "prompt.md").write_text("# t\n")
            pidfile = pathlib.Path(d, "child.pid")
            p = subprocess.Popen([sys.executable, str(ROOT / "skills/muster/muster.py"), "run", "w", str(task), str(repo),
                                  "--check", f"sleep 60 & echo $! > {pidfile}; wait"], env=dict(os.environ, MUSTER_CONFIG=str(cfg)),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if pidfile.exists() and pidfile.read_text().strip():
                    break
                t.sleep(0.1)
            self.assertTrue((task / "result.md").exists() and (task / "usage").exists())  # written before the check
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=30), 128 + signal.SIGTERM)
            child = int(pidfile.read_text())
            t.sleep(0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

    def test_meta_names_tasks_that_shared_the_repo(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            other_repo = pathlib.Path(d, "elsewhere")
            other_repo.mkdir()
            for name, meta in (("writer", f"repo={os.path.realpath(repo)}\nstarted=2000-01-01 00:00:00\n"),  # still running
                               ("old", f"repo={os.path.realpath(repo)}\nstarted=2000-01-01 00:00:00\nfinished=2000-01-01 01:00:00\n"),
                               ("apart", f"cwd={other_repo}\nstarted=2000-01-01 00:00:00\n"),
                               ("legacy", f"cwd={os.path.realpath(repo)}/sub\nstarted=2000-01-01 00:00:00\n")):
                t = pathlib.Path(d, "run", name)
                t.mkdir(parents=True)
                (t / "meta").write_text(meta)
            rc, task = self.run_script(d, repo, "true")
            self.assertEqual(rc, 0)
            self.assertEqual(M.read_meta(str(task))["shared_with"], "run/legacy,run/writer")

    def test_checks_run_one_at_a_time(self):
        import fcntl, subprocess
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            fake = pathlib.Path(d, "fake-claude")
            fake.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n")
            fake.chmod(0o755)
            cfg = pathlib.Path(d, "config.json")
            cfg.write_text(json.dumps({"runsDir": d, "workers": {"w": {"type": "claude", "command": str(fake)}}}))
            task = pathlib.Path(d, "run/a")
            task.mkdir(parents=True)
            (task / "prompt.md").write_text("# t\n")
            with open(pathlib.Path(d, ".check.lock"), "a") as held:
                fcntl.flock(held, fcntl.LOCK_EX)  # another task's check is running
                p = subprocess.Popen([sys.executable, str(ROOT / "skills/muster/muster.py"), "run", "w", str(task), str(repo),
                                      "--check", "true"], env=dict(os.environ, MUSTER_CONFIG=str(cfg)),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with self.assertRaises(subprocess.TimeoutExpired):
                    p.wait(timeout=3)  # waits for the lock
            self.assertEqual(p.wait(timeout=30), 0)
            self.assertEqual(M.read_meta(str(task))["check"], "pass")

    def test_changes_diff_is_cut_at_the_cap(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            with unittest.mock.patch.object(M, "CHANGES_MAX", 2000):
                rc, task = self.run_script(d, repo, "seq 1 2000 > big.txt")
            self.assertEqual(rc, 0)
            diff = (task / "changes.diff").read_text()
            self.assertLess(len(diff), 2300)
            self.assertIn("cut at", diff)
            meta = M.read_meta(str(task))
            self.assertTrue(meta["changes"].endswith("(cut)"), meta["changes"])
            self.assertEqual(meta["changes_sha256"], __import__("hashlib").sha256((task / "changes.diff").read_bytes()).hexdigest())

    def test_a_failing_snapshot_does_not_stop_the_task(self):
        with tempfile.TemporaryDirectory() as d:
            repo = self.git_repo(d, {"a.txt": "a\n"})
            with unittest.mock.patch.object(M, "snapshot", side_effect=FileNotFoundError("index vanished")):
                rc, task = self.run_script(d, repo, "true")
            self.assertEqual(rc, 0)
            self.assertEqual((task / "status").read_text().strip(), "done")
            self.assertIn("unavailable: index vanished", M.read_meta(str(task))["changes"])

    def test_no_changes_diff_outside_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            fake = pathlib.Path(d, "fake-claude")
            fake.write_text("#!/bin/sh\ncat >/dev/null\necho '{\"type\":\"result\",\"result\":\"ok\"}'\n")
            fake.chmod(0o755)
            task = pathlib.Path(d, "run/A")
            task.mkdir(parents=True)
            (task / "prompt.md").write_text("# t\n")
            plain = pathlib.Path(d, "plain")
            plain.mkdir()
            cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": str(fake)}}}
            with unittest.mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": d}):
                self.assertEqual(M.run(cfg, "w", str(task), str(plain)), 0)
            self.assertFalse((task / "changes.diff").exists())
            meta = M.read_meta(str(task))
            self.assertNotIn("base", meta)
            self.assertEqual(meta["changes"], "unavailable: cwd is not in a git repo")

    def test_model_and_effort_come_from_the_worker_log(self):
        with tempfile.TemporaryDirectory() as d:
            task = pathlib.Path(d, "a")
            task.mkdir()
            (task / "log.txt").write_text("OpenAI Codex v0.169.0\n--------\nmodel: gpt-6-astra\nreasoning effort: high\n--------\n")
            M.record_model(str(task), "codex")
            meta = M.read_meta(str(task))
            self.assertEqual((meta["model"], meta["effort"]), ("gpt-6-astra", "high"))
            other = pathlib.Path(d, "b")
            other.mkdir()
            (other / "stream.jsonl").write_text('{"type":"system","subtype":"hook_started"}\n'
                                                '{"type":"system","subtype":"init","model":"deepseek-flash[1m]"}\n')
            M.record_model(str(other), "claude")
            self.assertEqual(M.read_meta(str(other))["model"], "deepseek-flash[1m]")

    def test_record_model_skips_noise_before_the_banner_and_ignores_headerless_logs(self):
        with tempfile.TemporaryDirectory() as d:
            noisy = pathlib.Path(d, "a")
            noisy.mkdir()
            (noisy / "log.txt").write_text("ERROR rmcp transport failed\n" * 400
                                           + "OpenAI Codex v0.169.0\n--------\nmodel: gpt-6-astra\nreasoning effort: high\n--------\n")
            M.record_model(str(noisy), "codex")
            meta = M.read_meta(str(noisy))
            self.assertEqual((meta["model"], meta["effort"]), ("gpt-6-astra", "high"))
            bare = pathlib.Path(d, "b")
            bare.mkdir()
            (bare / "log.txt").write_text("user prompt\nmodel: foo\nreasoning effort: low\n")
            M.record_model(str(bare), "codex")
            self.assertNotIn("model", M.read_meta(str(bare)))

    def test_sigterm_still_records_a_final_status(self):
        import signal, subprocess, time
        with tempfile.TemporaryDirectory() as d:
            slow = pathlib.Path(d, "slow")
            slow.write_text("#!/bin/sh\nsleep 30\n")
            slow.chmod(0o755)
            cfg_path = pathlib.Path(d, "config.json")
            cfg_path.write_text(json.dumps({"runsDir": d, "workers": {"s": {"type": "claude", "command": str(slow)}}}))
            task = pathlib.Path(d, "run/A")
            task.mkdir(parents=True)
            (task / "prompt.md").write_text("# t\n")
            env = dict(os.environ, MUSTER_CONFIG=str(cfg_path))
            p = subprocess.Popen([sys.executable, str(ROOT / "skills/muster/muster.py"), "run", "s", str(task), d], env=env)
            for _ in range(50):
                if (task / "status").exists() and (task / "status").read_text().strip() == "running":
                    break
                time.sleep(0.1)
            time.sleep(0.3)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=20), 128 + signal.SIGTERM)
            self.assertEqual((task / "status").read_text().strip(), "failed: interrupted (SIGTERM)")
            self.assertIn("finished=", (task / "meta").read_text())
            self.assertEqual(M.read_meta(str(task))["failure"], "interrupted")


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.task = pathlib.Path(self.d, "run/A")
        self.task.mkdir(parents=True)
        (self.task / "prompt.md").write_text("# t\n")
        self.repo = pathlib.Path(self.d, "work-repo")
        self.other = pathlib.Path(self.d, "other")
        self.repo.mkdir()
        self.other.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self, **extra):
        return {"runsDir": self.d, "workers": {"w": {"type": "claude", "command": sys.executable}}, **extra}

    def test_only_refuses_outside_its_repos(self):
        cfg = self.cfg(only={"w": [str(pathlib.Path(self.d, "work-*"))]})
        self.assertEqual(M.run(cfg, "w", str(self.task), str(self.other)), 3)
        self.assertTrue((self.task / "status").read_text().startswith("failed: refused"))
        self.assertTrue(M.allowed_here(cfg, "w", str(self.repo)))
        self.assertTrue(M.allowed_here(cfg, "w", str(self.repo / "sub")))

    def test_caps_counts_live_tasks_only(self):
        busy = pathlib.Path(self.d, "run/B")
        busy.mkdir()
        (busy / "status").write_text("running\n")
        (busy / "meta").write_text(f"worker=w\npid={os.getpid()}\nstarted=2026-11-05 10:00:00\n")
        stale = pathlib.Path(self.d, "run/C")
        stale.mkdir()
        (stale / "status").write_text("running\n")
        (stale / "meta").write_text("worker=w\npid=999999\nstarted=2026-11-05 10:00:00\n")
        nopid = pathlib.Path(self.d, "run/D")
        nopid.mkdir()
        (nopid / "status").write_text("running\n")
        (nopid / "meta").write_text("worker=w\nstarted=2026-11-05 10:00:00\n")
        self.assertEqual(M.running_on(self.cfg(), "w"), 1)  # neither the dead pid nor the missing one counts


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = pathlib.Path(self.tmp.name)
        ok = self.d / "ok"
        ok.write_text("#!/bin/sh\ncat >/dev/null\n")
        ok.chmod(0o755)
        self.cfg = {"runsDir": str(self.d), "workers": {"w": {"type": "claude", "command": str(ok)}}, "caps": {"w": 1}}
        self.cfg_path = self.d / "config.json"
        self.cfg_path.write_text(json.dumps(self.cfg))

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, tid, status=None, meta=None):
        t = self.d / "run" / tid
        t.mkdir(parents=True)
        (t / "prompt.md").write_text("# t\n")
        if status is not None:
            (t / "status").write_text(status + "\n")
        if meta is not None:
            (t / "meta").write_text(meta)
        return t

    def launch(self, t, *opts, worker="w"):
        import subprocess
        return subprocess.Popen([sys.executable, str(ROOT / "skills/muster/muster.py"), "run", worker, str(t), str(self.d), *opts],
                                env=dict(os.environ, MUSTER_CONFIG=str(self.cfg_path)))

    def wait_status(self, t, prefix):
        import time
        for _ in range(100):
            if (t / "status").exists() and (t / "status").read_text().startswith(prefix):
                return
            time.sleep(0.1)
        self.fail(f"{t.name} status {(t / 'status').read_text()!r}, expected {prefix!r}")

    def test_over_the_cap_waits_for_a_slot(self):
        busy = self.task("B", "running", f"worker=w\npid={os.getpid()}\nstarted=2026-11-05 10:00:00\n")
        t = self.task("A")
        p = self.launch(t)
        self.wait_status(t, "queued, w runs 1 of 1")
        self.assertNotIn("started=", (t / "meta").read_text())
        (busy / "status").write_text("done\n")
        self.assertEqual(p.wait(timeout=20), 0)
        self.assertEqual((t / "status").read_text().strip(), "done")

    def test_after_waits_for_its_dependency(self):
        dep = self.task("A", "running", f"worker=x\npid={os.getpid()}\n")
        t = self.task("B")
        p = self.launch(t, "--after", "A")
        self.wait_status(t, "queued, waiting for A")
        (dep / "status").write_text("done\n")
        self.assertEqual(p.wait(timeout=20), 0)
        self.assertEqual((t / "status").read_text().strip(), "done")

    def test_a_failed_dependency_cancels_the_task(self):
        self.task("A", "failed: exit 1")
        t = self.task("B")
        self.assertEqual(M.run(self.cfg, "w", str(t), str(self.d), after=["A"]), 5)
        self.assertEqual((t / "status").read_text().strip(), "canceled: A failed")
        self.task("L", "running", "worker=x\npid=999999\n")
        t2 = self.task("C")
        self.assertEqual(M.run(self.cfg, "w", str(t2), str(self.d), after=["L"]), 5)
        self.assertEqual((t2 / "status").read_text().strip(), "canceled: L lost")

    def test_retry_of_records_the_attempt_it_reruns(self):
        self.task("A", "failed: exit 1")
        old, t = self.task("O", "failed: exit 1"), self.task("B")
        self.assertEqual(M.run(self.cfg, "w", str(t), str(self.d), after=["A"], retry_of=str(old)), 5)
        self.assertIn(f"retry_of={old.parent.name}/O\n", (t / "meta").read_text())

    def test_retry_of_refuses_itself_a_missing_task_and_another_runs_folder(self):
        t = self.task("B")
        other = pathlib.Path(self.d, "elsewhere/deep/run/X")
        other.mkdir(parents=True)
        (other / "prompt.md").write_text("# x\n")
        for target in (t, t.parent / "nope", other):
            with self.assertRaises(SystemExit):
                M.run(self.cfg, "w", str(t), str(self.d), retry_of=str(target))
        self.assertFalse((t / "meta").exists())

    def test_unknown_dependency_exits(self):
        with self.assertRaises(SystemExit):
            M.run(self.cfg, "w", str(self.task("B")), str(self.d), after=["nope"])

    def test_higher_priority_then_first_queued_starts_first(self):
        q = lambda when, prio, after="": f"workers=w\npid={os.getpid()}\nqueued=2026-11-05 10:0{when}:00\npriority={prio}\nafter={after}\n"
        late_high = self.task("A", "queued", q(5, 9))
        early = self.task("B", "queued", q(1, 0))
        later = self.task("C", "queued", q(2, 0))
        self.task("D", "queued", q(0, 99, after="Z"))  # its dependency is not done, so it is not in line
        self.task("Z", "running", f"worker=x\npid={os.getpid()}\n")
        head = M.queue_head(self.cfg, "w")
        self.assertEqual(head, [os.path.realpath(x) for x in (late_high, early, later)])

    def test_a_task_starts_on_whichever_listed_worker_is_free(self):
        self.cfg["workers"]["v"] = dict(self.cfg["workers"]["w"])
        self.task("B", "running", f"worker=w\npid={os.getpid()}\nstarted=2026-11-05 10:00:00\n")
        t = self.task("A")
        self.assertEqual(M.run(self.cfg, "w,v", str(t), str(self.d)), 0)
        meta = M.read_meta(str(t))
        self.assertEqual((meta["workers"], meta["worker"], meta["type"]), ("w,v", "v", "claude"))
        self.assertEqual((t / "status").read_text().strip(), "done")

    def test_a_task_waits_on_every_listed_worker(self):
        self.cfg["workers"]["v"] = dict(self.cfg["workers"]["w"])
        self.cfg["caps"]["v"] = 1
        self.cfg_path.write_text(json.dumps(self.cfg))
        self.task("B", "running", f"worker=w\npid={os.getpid()}\nstarted=2026-11-05 10:00:00\n")
        busy_v = self.task("C", "running", f"worker=v\npid={os.getpid()}\nstarted=2026-11-05 10:00:00\n")
        t2 = self.task("D")
        p = self.launch(t2, worker="w,v")
        self.wait_status(t2, "queued, w runs 1 of 1, v runs 1 of 1")
        self.assertEqual(M.queue_head(self.cfg, "v"), [os.path.realpath(t2)])
        (busy_v / "status").write_text("done\n")
        self.assertEqual(p.wait(timeout=20), 0)
        self.assertEqual(M.read_meta(str(t2))["worker"], "v")

    def test_fenced_workers_drop_out_of_the_list(self):
        self.cfg["workers"]["v"] = dict(self.cfg["workers"]["w"])
        self.cfg["only"] = {"w": ["/nowhere/*"]}
        t = self.task("A")
        self.assertEqual(M.run(self.cfg, "w,v", str(t), str(self.d)), 0)
        self.assertEqual(M.read_meta(str(t))["worker"], "v")
        self.cfg["only"]["v"] = ["/nowhere/*"]
        t2 = self.task("B")
        self.assertEqual(M.run(self.cfg, "w,v", str(t2), str(self.d)), 3)
        self.assertTrue((t2 / "status").read_text().startswith("failed: refused"))

    def test_cancel_stops_a_queued_task(self):
        self.task("B", "running", f"worker=w\npid={os.getpid()}\nstarted=2026-11-05 10:00:00\n")
        t = self.task("A")
        p = self.launch(t)
        self.wait_status(t, "queued")
        self.assertEqual(M.cancel(str(t), "superseded by C"), 0)
        self.assertEqual(p.wait(timeout=20), 128 + 15)
        self.assertEqual((t / "status").read_text().strip(), "canceled: superseded by C")
        self.assertEqual(M.cancel(str(t)), 1)  # already finished

    def test_cancel_before_the_runner_starts(self):
        t = self.task("A")
        self.assertEqual(M.cancel(str(t)), 0)
        self.assertEqual((t / "status").read_text().strip(), "canceled")
        self.assertEqual(M.run(self.cfg, "w", str(t), str(self.d)), 0)
        self.assertEqual((t / "status").read_text().strip(), "canceled")



class Setup(unittest.TestCase):
    def test_doctor_names_each_problem(self):
        self.assertIn("only: expected", "\n".join(M.doctor({"workers": {}, "only": ["x"], "caps": [1]})))
        odd = "\n".join(M.doctor({"workers": {"x": {"type": "claude", "command": 5, "args": None}}}))
        self.assertIn("workers.x.command: expected a path string", odd)
        self.assertIn("workers.x.args: expected a list of strings", odd)
        cfg = {"workers": {"a": {"type": "codex", "codexHome": "/nonexistent", "command": sys.executable},
                           "b": {"type": "cli"}},
               "routing": [{"match": "~/x/*", "workers": ["a", "ghost"], "preamble": "one line"}],
               "only": {"nobody": ["*"]}, "caps": {"a": 0}, "preamble": [1]}
        probs = "\n".join(M.doctor(cfg))
        for needle in ("workers.a.codexHome", "workers.b.type", "unknown worker 'ghost'",
                       "no '*' fallback", "only.nobody", "caps.a",
                       "preamble: expected", "routing[0] (~/x/*).preamble: expected"):
            self.assertIn(needle, probs)

    def test_doctor_catches_typos_and_wrong_shapes(self):
        cfg = {"workers": {"c": {"type": "claude", "command": sys.executable, "arg": ["-x"], "billing": "free"}},
               "routing": [{"match": "*", "workers": "c", "preambel": ["x"]}],
               "only": {"c": "~/w/*"}, "preambel": ["x"], "_note": "kept"}
        probs = "\n".join(M.doctor(cfg))
        for needle in ("preambel: unknown key", "workers.c.arg: unknown key", "workers.c.billing",
                       "routing[0].preambel: unknown key", "routing[0] (*).workers: expected", "only.c: expected"):
            self.assertIn(needle, probs)
        self.assertNotIn("_note", probs)

    def test_doctor_passes_a_good_config(self):
        cfg = {"workers": {"c": {"type": "claude", "command": sys.executable}},
               "routing": [{"match": "~/w/*", "workers": ["c"], "preamble": ["Open PRs with pr.sh."]},
                           {"match": "*", "workers": ["c"]}],
               "caps": {"claude": 2}, "preamble": ["No secrets."]}
        self.assertEqual(M.doctor(cfg), [])

    def test_detect_finds_wrappers_and_skips_backups(self):
        with tempfile.TemporaryDirectory() as d:
            bin_ = pathlib.Path(d)
            for name, body in [("ds-claude", "#!/bin/sh\nexport ANTHROPIC_BASE_URL=https://example.invalid\nexec claude \"$@\"\n"),
                               ("ds-claude.bak-1", "#!/bin/sh\nexport ANTHROPIC_BASE_URL=x\nexec claude\n"),
                               ("other", "#!/bin/sh\necho hi\n")]:
                f = bin_ / name
                f.write_text(body)
                f.chmod(0o755)
            old = os.environ["PATH"]
            os.environ["PATH"] = d
            try:
                names = [c["name"] for c in M.detect()["commands"]]
            finally:
                os.environ["PATH"] = old
            self.assertIn("ds-claude", names)
            self.assertNotIn("ds-claude.bak-1", names)
            self.assertNotIn("other", names)


if __name__ == "__main__":
    unittest.main()


def fake_cli(dirpath, name, body):
    f = pathlib.Path(dirpath, name)
    f.write_text("#!/bin/sh\n" + body)
    f.chmod(0o755)
    return str(f)


def make_task(root, title="t", run="run"):
    task = pathlib.Path(root, run, "A")
    task.mkdir(parents=True)
    (task / "prompt.md").write_text(f"# {title}\n")
    return task


class Pricing(unittest.TestCase):
    def test_price_for_ignores_context_suffix(self):
        self.assertIs(M.price_for(PRICES, "deepseek-flash[1m]"), PRICES["deepseek-flash"])
        self.assertIsNone(M.price_for(PRICES, "no-such-model"))

    def test_codex_long_context_rate_per_request(self):
        with tempfile.TemporaryDirectory() as d:
            home, task = pathlib.Path(d, "home"), pathlib.Path(d, "task")
            (home / "sessions").mkdir(parents=True)
            task.mkdir()
            (task / "log.txt").write_text("session id: s1\n")
            over = PRICES["gpt-6-astra"]["long_context_over"] + 1
            lines = [json.dumps({"payload": {"model": "gpt-6-astra"}}),
                     json.dumps({"payload": {"type": "token_count", "info": {
                         "total_token_usage": {"total_tokens": over + 10},
                         "last_token_usage": {"input_tokens": over, "cached_input_tokens": 0, "output_tokens": 10}}}})]
            (home / "sessions/rollout-x-s1.jsonl").write_text("\n".join(lines))
            u = M.usage_codex(str(task), str(home), PRICES, "metered")
            long = PRICES["gpt-6-astra"]["long"]
            self.assertAlmostEqual(u["usd"], round((over * long["input"] + 10 * long["output"]) / 1e6, 4), places=4)

    def test_codex_without_session_id_has_no_usage(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "log.txt").write_text("no header here\n")
            self.assertIsNone(M.usage_codex(d, d, PRICES, "metered"))

    def test_codex_billing_follows_login(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(M.codex_billing(d), "metered")  # no auth.json: assume an API key
            pathlib.Path(d, "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
            self.assertEqual(M.codex_billing(d), "subscription")
            pathlib.Path(d, "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
            self.assertEqual(M.codex_billing(d), "metered")

    def _claude_usage(self, result):
        with tempfile.TemporaryDirectory() as d:
            pathlib.Path(d, "stream.jsonl").write_text(result)
            started = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc)  # Saturday: off-peak
            return M.usage_claude(d, PRICES, "metered", started), pathlib.Path(d, "result.md").read_text()

    def test_claude_plain_json_output_is_read(self):
        u, text = self._claude_usage(json.dumps({"result": "hi", "modelUsage": {
            "deepseek-flash": {"inputTokens": 1000000, "outputTokens": 0, "cacheReadInputTokens": 0}}}))
        self.assertEqual(text, "hi")
        self.assertAlmostEqual(u["usd"], PRICES["deepseek-flash"]["input"], places=4)
        self.assertFalse(u["peak"])

    def test_claude_known_model_falls_back_to_claude_codes_cost(self):
        u, _ = self._claude_usage(json.dumps({"type": "result", "result": "", "modelUsage": {
            "some-claude-model": {"inputTokens": 10, "outputTokens": 10, "costUSD": 0.42, "costBasis": "list"}}}))
        self.assertEqual(u["usd"], 0.42)

    def test_claude_unpriced_model_has_no_cost(self):
        u, _ = self._claude_usage(json.dumps({"type": "result", "result": "", "modelUsage": {
            "mystery": {"inputTokens": 10, "outputTokens": 10, "costUSD": 9.9, "costBasis": "unknown"}}}))
        self.assertIsNone(u["usd"])
        self.assertEqual(u["input"], 10)

    def test_cached_input_counts_toward_input(self):
        u, _ = self._claude_usage(json.dumps({"type": "result", "result": "", "modelUsage": {
            "deepseek-flash": {"inputTokens": 100, "cacheReadInputTokens": 900, "cacheCreationInputTokens": 0, "outputTokens": 0}}}))
        self.assertEqual((u["input"], u["cached"]), (1000, 900))


class Runs(unittest.TestCase):
    def test_codex_worker_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            home = pathlib.Path(d, "codex-home")
            (home / "sessions/2026").mkdir(parents=True)
            (home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
            rollout = home / "sessions/2026/rollout-x-abc.jsonl"
            # The fake codex records its CODEX_HOME, writes the -o file, and leaves a rollout with usage.
            codex = fake_cli(d, "codex", f"""cat >/dev/null
out=""; while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
echo "session id: abc"
echo "$CODEX_HOME" > "{d}/seen-home"
printf '%s\\n' '{{"payload":{{"model":"gpt-6-astra"}}}}' '{{"payload":{{"type":"token_count","info":{{"total_token_usage":{{"total_tokens":110}},"last_token_usage":{{"input_tokens":100,"cached_input_tokens":0,"output_tokens":10}}}}}}}}' > "{rollout}"
echo "all done" > "$out"
""")
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"cx": {"type": "codex", "codexHome": str(home), "command": codex}}}
            self.assertEqual(M.run(cfg, "cx", str(task), d), 0)
            self.assertEqual((task / "status").read_text().strip(), "done")
            self.assertEqual((task / "result.md").read_text().strip(), "all done")
            self.assertEqual(pathlib.Path(d, "seen-home").read_text().strip(), str(home))
            u = json.loads((task / "usage").read_text())
            self.assertEqual((u["model"], u["input"], u["output"], u["billing"]), ("gpt-6-astra", 100, 10, "subscription"))
            self.assertIn("session id: abc", (task / "log.txt").read_text())

    def test_an_error_word_in_the_echoed_prompt_is_not_the_failure(self):
        # codex copies the prompt into its log; a brief that mentions ENOSPC must not read as a full disk
        with tempfile.TemporaryDirectory() as d:
            worker = fake_cli(d, "w", "cat >&2\necho 'ERROR: You hit your usage limit.' >&2\nexit 1\n")
            task = make_task(d)
            (task / "prompt.md").write_text("# t\n- Check the ENOSPC path and the error handling.\n")
            M.run({"runsDir": d, "workers": {"w": {"type": "claude", "command": worker}}}, "w", str(task), d)
            self.assertEqual((task / "status").read_text().strip(), "failed: ERROR: You hit your usage limit.")

    def test_worker_failure_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            bad = fake_cli(d, "bad", "cat >/dev/null\necho boom >&2\nexit 7\n")
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"b": {"type": "claude", "command": bad}}}
            self.assertEqual(M.run(cfg, "b", str(task), d), 7)
            self.assertEqual((task / "status").read_text().strip(), "failed: exit 7")
            self.assertEqual(M.read_meta(str(task))["exit"], "7")
            self.assertIn("boom", (task / "log.txt").read_text())

    def test_args_and_cwd_reach_the_worker(self):
        with tempfile.TemporaryDirectory() as d:
            work = pathlib.Path(d, "repo")
            work.mkdir()
            spy = fake_cli(d, "spy", f'cat >/dev/null\necho "$@" > "{d}/args"\npwd -P > "{d}/cwd"\n')
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"s": {"type": "claude", "command": spy, "args": ["--model", "x"]}}}
            M.run(cfg, "s", str(task), str(work))
            self.assertEqual(pathlib.Path(d, "args").read_text().split(),
                             ["-p", "--output-format", "stream-json", "--verbose", "--model", "x"])
            self.assertEqual(pathlib.Path(d, "cwd").read_text().strip(), os.path.realpath(work))

    def test_meta_records_session_and_pid(self):
        with tempfile.TemporaryDirectory() as d:
            ok = fake_cli(d, "ok", "cat >/dev/null\n")
            task = make_task(d)
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-1"
            try:
                M.run({"runsDir": d, "workers": {"o": {"type": "claude", "command": ok}}}, "o", str(task), d)
            finally:
                del os.environ["CLAUDE_CODE_SESSION_ID"]
            meta = M.read_meta(str(task))
            self.assertEqual(meta["session"], "sess-1")
            self.assertTrue(int(meta["pid"]) > 0)
            self.assertIn("finished", meta)

    def test_unknown_worker_and_type_exit(self):
        with tempfile.TemporaryDirectory() as d:
            task = make_task(d)
            with self.assertRaises(SystemExit):
                M.run({"runsDir": d, "workers": {}}, "ghost", str(task), d)
            with self.assertRaises(SystemExit):
                M.run({"runsDir": d, "workers": {"x": {"type": "cli"}}}, "x", str(task), d)


class Failures(unittest.TestCase):
    def test_enospc_says_disk_full_and_how_to_resume(self):
        with tempfile.TemporaryDirectory() as d:
            bad = fake_cli(d, "bad", "cat >/dev/null\necho \"Error: ENOSPC: no space left on device, write\" >&2\n"
                                     "echo 'error: later noise' >&2\nexit 1\n")
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"b": {"type": "claude", "command": bad}}}
            self.assertEqual(M.run(cfg, "b", str(task), d), 1)
            self.assertRegex((task / "status").read_text(), r"^failed: disk full \(ENOSPC\), [\d.]+G free on /")
            meta = M.read_meta(str(task))
            self.assertEqual(meta["last_error"], "Error: ENOSPC: no space left on device, write")
            self.assertEqual(meta["log_path"], os.path.abspath(task / "log.txt"))
            self.assertIn(f" run b {os.path.abspath(task)} {d}", (task / "result.md").read_text())

    def test_failed_status_names_the_last_error_without_secrets(self):
        with tempfile.TemporaryDirectory() as d:
            cx = fake_cli(d, "codex", "cat >/dev/null\necho 'Traceback (most recent call last):'\n"
                                      "echo 'ValueError: bad key sk-abc123XYZ for Bearer eyJhbGciOi'\necho done.\nexit 2\n")
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"cx": {"type": "codex", "codexHome": d, "command": cx}}}
            self.assertEqual(M.run(cfg, "cx", str(task), d), 2)
            self.assertEqual((task / "status").read_text().strip(), "failed: ValueError: bad key sk-*** for Bearer ***")
            self.assertEqual(M.read_meta(str(task))["log_path"], os.path.abspath(task / "log.txt"))

    def test_claude_error_result_is_the_reason(self):
        with tempfile.TemporaryDirectory() as d:
            bad = fake_cli(d, "bad", "cat >/dev/null\n"
                                     "echo '{\"type\":\"result\",\"is_error\":true,\"result\":\"API Error: 429 rate limit exceeded\"}'\nexit 1\n")
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"b": {"type": "claude", "command": bad}}}
            self.assertEqual(M.run(cfg, "b", str(task), d), 1)
            self.assertEqual((task / "status").read_text().strip(), "failed: API Error: 429 rate limit exceeded")
            self.assertEqual(M.read_meta(str(task))["log_path"], os.path.abspath(task / "stream.jsonl"))

    def test_reason_is_short_and_redacted(self):
        self.assertEqual(len(M.short("error " * 100)), 160)
        self.assertEqual(M.redact("token=abc ghp_XYZ123 xoxb-1-2 api_key: k"), "token=*** ghp_*** xoxb-*** api_key: ***")


class WorkerReport(unittest.TestCase):
    def launch(self, d, report, check=None, exit_code=0):
        worker = fake_cli(d, "w", f"cat >/dev/null\ncat > report.json <<'EOF'\n{json.dumps({'type': 'result', 'result': report})}\nEOF\n"
                                  f"cat report.json\nrm report.json\nexit {exit_code}\n")
        task = make_task(d)
        return M.run({"runsDir": d, "workers": {"w": {"type": "claude", "command": worker}}}, "w", str(task), d, check=check), task

    def test_a_blocked_worker_fails_without_the_check_and_cancels_dependents(self):
        for word in ("BLOCKED", "NEEDS_CONTEXT"):
            with tempfile.TemporaryDirectory() as d:
                marker = pathlib.Path(d, "check-ran")
                rc, task = self.launch(d, f"\nStatus: {word}\nNo schema was given.", check=f"touch {marker}")
                self.assertEqual(rc, 1)
                self.assertFalse(marker.exists())
                self.assertEqual((task / "status").read_text().strip(), f"failed: worker reported {word}")
                meta = M.read_meta(str(task))
                self.assertEqual(meta["check"], f"skipped: the worker reported {word}")
                self.assertEqual(meta["last_error"], f"worker reported {word}")
                self.assertEqual(meta["log_path"], os.path.abspath(task / "result.md"))
                self.assertEqual(M.dep_state(str(task.parent / "B"), ["A"]), "broken: A failed")

    def test_only_the_reports_first_line_counts(self):
        with tempfile.TemporaryDirectory() as d:
            rc, task = self.launch(d, "Status: DONE\nThe brief said to report:\nStatus: BLOCKED\n", check="true")
            self.assertEqual((rc, (task / "status").read_text().strip()), (0, "done"))
            self.assertEqual(M.read_meta(str(task))["check"], "pass")

    def test_a_decorated_status_line_counts_and_prose_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            for text, want in (("**Status: BLOCKED**\n", "BLOCKED"), ("Status: BLOCKED by nothing, all fine\n", None),
                               ("Status: DONE_WITH_CONCERNS\n", None), ("", None)):
                pathlib.Path(d, "result.md").write_text(text)
                self.assertEqual(M.reported_stop(d), want, text)

    def test_the_check_records_when_it_waits_and_when_it_starts(self):
        with tempfile.TemporaryDirectory() as d:
            rc, task = self.launch(d, "Status: DONE", check="true")
            keys = [line.split("=")[0] for line in (task / "meta").read_text().splitlines()]
            self.assertLess(keys.index("worker_exited"), keys.index("check_waiting"))
            self.assertLess(keys.index("check_waiting"), keys.index("check_started"))


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = pathlib.Path(self.tmp.name, "run")
        for name, value in (("QUEUE_POLL_S", 0.01), ("WATCH_GRACE_S", 0.05)):
            patcher = unittest.mock.patch.object(M, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, tid, status="running", kind="codex", silent=3600, pid=None, extra=""):
        t = self.run_dir / tid
        t.mkdir(parents=True)
        (t / "status").write_text(status + "\n")
        started = datetime.datetime.fromtimestamp(time.time() - 7200).strftime("%Y-%m-%d %H:%M:%S")
        (t / "meta").write_text(f"worker=w1\ntype={kind}\npid={pid or os.getpid()}\nstarted={started}\n{extra}")
        log = t / ("stream.jsonl" if kind == "claude" else "log.txt")
        log.write_text("first line\nlast line token=abc\n")
        os.utime(log, (time.time() - silent,) * 2)
        return t

    def watch(self, stall_min=10):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(M.watch(str(self.run_dir), stall_min), 0)
        return out.getvalue()

    def finish_soon(self, t):
        timer = threading.Timer(0.2, lambda: (t / "status").write_text("done\n"))
        timer.start()
        self.addCleanup(timer.join)

    def test_a_silent_worker_is_reported_once_until_it_speaks_and_stops_again(self):
        t = self.task("a", kind="claude")
        out = self.watch()
        self.assertRegex(out, r"stalled: a on w1, silent for 60m\d\ds")
        self.assertIn(os.path.abspath(t / "stream.jsonl"), out)
        self.assertIn("last line token=***", out)
        self.assertEqual((t / "status").read_text().strip(), "running")  # nothing was stopped
        self.finish_soon(t)
        self.assertTrue(self.watch().startswith("all tasks finished"))  # the same silence is not news
        (t / "status").write_text("running\n")
        os.utime(t / "stream.jsonl", (time.time() - 1800,) * 2)
        self.assertIn("stalled: a", self.watch())

    def test_quiet_phases_queued_tasks_and_dead_runners_are_not_stalls(self):
        check = self.task("check", extra="worker_exited=x\ncheck_started=x\n")
        self.task("queued", status="queued, w1 runs 3 of 3")
        self.task("dead", pid=999999)
        self.task("fresh", silent=5)
        for name in ("queued", "fresh"):
            self.finish_soon(self.run_dir / name)
        self.finish_soon(check)
        out = self.watch()
        self.assertNotIn("stalled", out)
        self.assertIn("dead: failed: lost", out)

    def test_it_waits_for_a_launched_task_then_gives_up_on_one_never_started(self):
        (self.run_dir / "never").mkdir(parents=True)
        self.assertIn("never: not started", self.watch())

    def test_the_cli_needs_no_config_and_reads_the_threshold(self):
        import subprocess
        self.task("a", silent=120)
        r = subprocess.run([sys.executable, str(ROOT / "skills/muster/muster.py"), "watch", str(self.run_dir), "--stall-min", "1"],
                           env=dict(os.environ, MUSTER_CONFIG="/nonexistent/config.json"), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stalled: a", r.stdout)


class MoreGuards(unittest.TestCase):
    def test_only_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            real = pathlib.Path(d, "work-repo")
            real.mkdir()
            link = pathlib.Path(d, "elsewhere")
            link.symlink_to(real)
            cfg = {"only": {"w": [str(pathlib.Path(os.path.realpath(d), "work-*"))]}}
            self.assertTrue(M.allowed_here(cfg, "w", str(link)))
            self.assertTrue(M.allowed_here(cfg, "free", "/anywhere"))  # not fenced

    def test_per_worker_cap_beats_type_cap(self):
        with tempfile.TemporaryDirectory() as d:
            ok = fake_cli(d, "ok", "cat >/dev/null\n")
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": ok}}, "caps": {"claude": 0, "w": 1}}
            self.assertEqual(M.run(cfg, "w", str(task), d), 0)  # type cap 0 would refuse; the worker's own cap wins


class Clean(unittest.TestCase):
    def test_lists_old_runs_worktrees_and_branches_and_deletes_nothing(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            repo = pathlib.Path(d, "repo")
            repo.mkdir()
            g = lambda *a, cwd=repo: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                                    check=True, capture_output=True)
            g("init", "-q", "-b", "main")
            (repo / "a.txt").write_text("a\n")
            g("add", ".")
            g("commit", "-qm", "base")
            done_wt, open_wt = pathlib.Path(d, "repo-done"), pathlib.Path(d, "repo-open")
            g("worktree", "add", "-q", str(done_wt), "-b", "muster/done")
            (done_wt / "b.txt").write_text("b\n")
            g("add", ".", cwd=done_wt)
            g("commit", "-qm", "done work", cwd=done_wt)
            (repo / "other.txt").write_text("o\n")
            g("add", ".")
            g("commit", "-qm", "unrelated work on main")
            g("cherry-pick", "muster/done")  # merged by content, as a rebase or cherry-pick merge leaves it
            self.assertNotEqual(subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", "muster/done", "main"]).returncode, 0)
            g("worktree", "add", "-q", str(open_wt), "-b", "muster/open")
            (open_wt / "c.txt").write_text("c\n")
            g("add", ".", cwd=open_wt)
            g("commit", "-qm", "open work", cwd=open_wt)
            g("branch", "muster/old-merged", "main~1")
            g("branch", "muster/old-merged-2", "main~1")
            runs = pathlib.Path(d, "runs")
            for name, cwd, age in (("20260101-0000-old", repo, 40), ("20261117-0000-new", done_wt, 0)):
                t = runs / name / "task-a"
                t.mkdir(parents=True)
                (t / "status").write_text("done\n")
                (t / "meta").write_text(f"cwd={cwd}\n")
                for f in (t / "status", t / "meta", t, t.parent):
                    os.utime(f, (time.time() - age * 86400,) * 2)
            out = M.clean({"runsDir": str(runs)}, days=14)
            self.assertIn("20260101-0000-old", out)
            self.assertNotIn("20261117-0000-new", out)
            self.assertRegex(out, r"worktree .*repo-done \[muster/done\]: merged, .*   git -C .* worktree remove")
            self.assertRegex(out, r"worktree .*repo-open \[muster/open\]: 1 commits not in main, .*# keep or check first")
            self.assertIn("all 2 merged branches without a worktree", out)
            # A branch whose own merge commit carries work main lacks is not merged, though `git cherry` shows nothing.
            g("checkout", "-q", "-b", "muster/with-merge", "main~1")
            g("checkout", "-q", "-b", "side", "main~1")
            (repo / "side.txt").write_text("s\n")
            g("add", ".")
            g("commit", "-qm", "side")
            g("checkout", "-q", "muster/with-merge")
            g("merge", "-q", "--no-ff", "-m", "merge side", "side")
            g("branch", "-D", "side")
            g("checkout", "-q", "main")
            self.assertRegex(M.clean({"runsDir": str(runs)}, days=14), r"branch muster/with-merge: \d+ commits not in main")
            self.assertTrue(done_wt.is_dir() and (runs / "20260101-0000-old").is_dir())  # nothing deleted
            self.assertIn("muster/old-merged", subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True).stdout)


class Cli(unittest.TestCase):
    def run_cli(self, cfg, *args):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "config.json")
            p.write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))
            return subprocess.run([sys.executable, str(ROOT / "skills/muster/muster.py"), *args],
                                  env=dict(os.environ, MUSTER_CONFIG=str(p)), capture_output=True, text=True)

    def test_doctor_exit_codes(self):
        good = {"workers": {"c": {"type": "claude", "command": sys.executable}}, "routing": [{"match": "*", "workers": ["c"]}]}
        r = self.run_cli(good, "doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(r.stdout.startswith("ok:"))
        r = self.run_cli({"workers": {}}, "doctor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("workers: none configured", r.stdout)

    def test_config_allows_comments(self):
        text = ('{ // workers\n  "workers": {"c": {"type": "claude", "command": "%s"}}, // trailing\n'
                '  "routing": [{"match": "*", "workers": ["c"], "preamble": ["see https://x.test/a//b"]}]\n}' % sys.executable)
        r = self.run_cli(text, "doctor")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_readme_example_config_parses(self):
        readme = (ROOT / "README.md").read_text()
        block = re.search(r'```jsonc?\n(\{\n  "runsDir".*?\n\})\n```', readme, re.S)
        self.assertIsNotNone(block, "README config example not found")
        r = self.run_cli(block[1], "workers")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("codex-work", r.stdout)

    def test_broken_json_names_the_line(self):
        r = self.run_cli('{\n  "workers": {},\n  "caps": {"codex": 3,}\n}', "doctor")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not valid JSON", r.stderr)
        self.assertIn("line 3", r.stderr)

    def test_workers_lists_names(self):
        r = self.run_cli({"workers": {"alpha": {"type": "claude", "command": "x"}}}, "workers")
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.startswith("alpha\tclaude"))

    def test_missing_config_explains_itself(self):
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "skills/muster/muster.py"), "doctor"],
                           env=dict(os.environ, MUSTER_CONFIG="/nonexistent/config.json"), capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no config", r.stderr)

    def test_detect_runs_without_config(self):
        import subprocess
        r = subprocess.run([sys.executable, str(ROOT / "skills/muster/muster.py"), "detect"],
                           env=dict(os.environ, MUSTER_CONFIG="/nonexistent/config.json"), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(json.loads(r.stdout)["config_exists"])


class TaskTier(unittest.TestCase):
    def test_tier_args_follow_the_workers_own_and_a_name_key_beats_the_type(self):
        with tempfile.TemporaryDirectory() as d:
            spy = fake_cli(d, "spy", f'cat >/dev/null\necho "$@" > "{d}/args"\n')
            cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": spy, "args": ["--model", "x"]}},
                   "tiers": {"standard": {"args": {"claude": ["--effort", "low"], "w": ["--effort", "high"]}}}}
            task = make_task(d)
            self.assertEqual(M.run(cfg, "w", str(task), d, tier="standard"), 0)
            self.assertEqual(pathlib.Path(d, "args").read_text().split(),
                             ["-p", "--output-format", "stream-json", "--verbose", "--model", "x",
                              "--effort", "high"])

            cx = fake_cli(d, "cx", f'cat >/dev/null\necho "$@" > "{d}/cx-args"\n')
            home = pathlib.Path(d, "codex-home")
            home.mkdir()
            (home / "auth.json").write_text("{}")
            cfg = {"runsDir": d, "workers": {"c": {"type": "codex", "codexHome": str(home), "command": cx,
                                                   "args": ["-m", "gpt-6-astra"]}},
                   "tiers": {"high": {"args": {"codex": ["-c", 'model_reasoning_effort="xhigh"']}}}}
            task = make_task(d, run="second")
            self.assertEqual(M.run(cfg, "c", str(task), d, tier="high"), 0)
            self.assertEqual(pathlib.Path(d, "cx-args").read_text().split()[-5:],
                             ["-m", "gpt-6-astra", "-c", 'model_reasoning_effort="xhigh"', "-"])

    def test_prefer_reorders_without_adding_workers_or_bypassing_only(self):
        with tempfile.TemporaryDirectory() as d:
            ok = fake_cli(d, "ok", "cat >/dev/null\n")
            cfg = {"runsDir": d, "tiers": {"low": {"prefer": ["b", "c"]}},
                   "workers": {n: {"type": "claude", "command": ok} for n in ("a", "b", "c")}}
            task = make_task(d)
            self.assertEqual(M.run(cfg, "a,b", str(task), d, tier="low"), 0)
            self.assertEqual(M.read_meta(str(task))["workers"], "b,a")  # c is not added

            ordered = make_task(d, run="second")
            self.assertEqual(M.run(cfg, "a,c,b", str(ordered), d, tier="low"), 0)
            self.assertEqual(M.read_meta(str(ordered))["workers"], "c,b,a")  # preferred keep their order

            cfg["only"] = {"b": ["/nowhere/*"]}
            fenced = make_task(d, run="third")
            self.assertEqual(M.run(cfg, "a,b", str(fenced), d, tier="low"), 0)
            self.assertEqual(M.read_meta(str(fenced))["workers"], "a")  # prefer cannot un-fence b

    def test_a_bad_or_unconfigured_tier_exits_before_writing_meta(self):
        with tempfile.TemporaryDirectory() as d:
            ok = fake_cli(d, "ok", "cat >/dev/null\n")
            cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": ok}},
                   "tiers": {"low": {}}}
            task = make_task(d)
            for name, want in (("huge", "--tier huge: expected one of low, standard, high"),
                               ("high", "--tier high: no tiers.high")):
                with self.assertRaises(SystemExit) as raised:
                    M.run(cfg, "w", str(task), d, tier=name)
                self.assertIn(want, str(raised.exception))
            self.assertFalse((task / "meta").exists())

    def test_meta_records_the_tier_or_an_empty_one_and_no_tier_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            spy = fake_cli(d, "spy", f'cat >/dev/null\necho "$@" > "{d}/args"\n')
            cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": spy, "args": ["--model", "x"]}},
                   "tiers": {"low": {"args": {"claude": ["--effort", "low"]}}}}
            task = make_task(d)
            self.assertEqual(M.run(cfg, "w", str(task), d), 0)
            self.assertEqual(M.read_meta(str(task))["tier"], "")
            self.assertEqual(pathlib.Path(d, "args").read_text().split(),
                             ["-p", "--output-format", "stream-json", "--verbose", "--model", "x"])
            task = make_task(d, run="second")
            self.assertEqual(M.run(cfg, "w", str(task), d, tier="low"), 0)
            self.assertEqual(M.read_meta(str(task))["tier"], "low")


class TierDoctor(unittest.TestCase):
    def test_doctor_reports_each_bad_tier_shape(self):
        worker = {"type": "claude", "command": sys.executable}
        probs = "\n".join(M.doctor({"workers": {"w": worker},
                                    "tiers": {"huge": {}, "low": {"preferred": [], "prefer": ["ghost"],
                                                                  "args": {"nope": [], "w": "x"}}}}))
        for needle in ("tiers.huge: unknown tier", "tiers.low.preferred: unknown key",
                       "tiers.low.prefer: expected a list of known worker names",
                       "tiers.low.args.nope: not a worker name or type",
                       "tiers.low.args.w: expected a list of strings"):
            self.assertIn(needle, probs)
        for cfg, needle in (({"tiers": [1]}, "tiers: expected an object"),
                            ({"tiers": {"low": "x"}}, "tiers.low: expected an object"),
                            ({"tiers": {"low": {"args": ["x"]}}}, "tiers.low.args: expected an object"),
                            ({"tiers": {"low": {"prefer": "w"}}}, "tiers.low.prefer: expected"),
                            ({"tiers": {"low": {"when": ["x"]}}}, "tiers.low.when: expected a string"),
                            ({"routing": [{"match": "*", "workers": ["w"], "tiers": []}]}, "*).tiers: expected an object"),
                            ({"routing": [{"match": "*", "workers": ["w"], "tiers": {"huge": {}}}]},
                             "*).tiers.huge: unknown tier"),
                            ({"routing": [{"match": "*", "workers": ["w"], "tiers": {"low": {"args": {}}}}]},
                             "*).tiers.low: expected { when }"),
                            ({"routing": [{"match": "*", "workers": ["w"], "tiers": {"low": {"when": 1}}}]},
                             "*).tiers.low.when: expected a string")):
            self.assertIn(needle, "\n".join(M.doctor({"workers": {"w": worker}, **cfg})))

    def test_doctor_accepts_tiers_and_note_keys(self):
        cfg = {"workers": {"w": {"type": "claude", "command": sys.executable}},
               "tiers": {"_note": "tiers are optional", "low": {"_why": "small", "when": "docs and typos only",
                                                                "prefer": ["w"], "args": {"w": ["-x"]}}},
               "routing": [{"match": "*", "workers": ["w"],
                            "tiers": {"_note": "x", "high": {"when": "any change", "_why": "payments"}}}]}
        self.assertEqual(M.doctor(cfg), [])


class FailureKind(unittest.TestCase):
    def run_fake(self, d, body, check=None):
        worker = fake_cli(d, "w", body)
        task = make_task(d)
        rc = M.run({"runsDir": d, "workers": {"w": {"type": "claude", "command": worker}}},
                   "w", str(task), d, check=check)
        return rc, task

    def test_a_usage_limit_error_is_a_provider_failure(self):
        with tempfile.TemporaryDirectory() as d:
            rc, task = self.run_fake(d, "cat >/dev/null\necho 'Error: usage limit reached' >&2\nexit 1\n")
            self.assertEqual(rc, 1)
            self.assertEqual(M.read_meta(str(task))["failure"], "provider")

    def test_any_other_worker_exit_is_an_infra_failure_not_a_task_failure(self):
        for body in ("echo 'ValueError: bad input' >&2\nexit 2", "echo 'ENOSPC: no space left on device' >&2\nexit 1",
                     "echo 'error: stream disconnected before completion' >&2\nexit 1",
                     "echo 'Error: 502 Bad Gateway' >&2\nexit 1", "exit 137"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as d:
                rc, task = self.run_fake(d, "cat >/dev/null\n" + body + "\n")
                self.assertNotEqual(rc, 0)
                self.assertEqual(M.read_meta(str(task))["failure"], "infra")

    def test_a_failed_check_is_a_task_failure_and_a_blocked_report_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            rc, task = self.run_fake(d, "cat >/dev/null\n", check="exit 3")
            self.assertEqual(rc, 3)
            self.assertEqual(M.read_meta(str(task))["failure"], "task")
        for status in ("BLOCKED", "NEEDS_CONTEXT"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                rc, task = self.run_fake(d, "cat >/dev/null\n"
                                            f"echo '{{\"type\":\"result\",\"result\":\"Status: {status}\"}}'\n")
                self.assertEqual(rc, 1)
                self.assertEqual(M.read_meta(str(task))["failure"], "blocked")

    def test_canceled_tasks_have_no_failure_key(self):
        with tempfile.TemporaryDirectory() as d:
            ok = fake_cli(d, "ok", "cat >/dev/null\n")
            dep = pathlib.Path(d, "run", "A")
            dep.mkdir(parents=True)
            (dep / "status").write_text("failed: exit 1\n")
            task = pathlib.Path(d, "run", "B")
            task.mkdir()
            (task / "prompt.md").write_text("# t\n")
            self.assertEqual(M.run({"runsDir": d, "workers": {"w": {"type": "claude", "command": ok}}},
                                   "w", str(task), d, after=["A"]), 5)
            self.assertNotIn("failure", M.read_meta(str(task)))

    def test_the_provider_pattern_covers_provider_errors_only(self):
        for text in ("usage limit reached", "Quota exceeded", "rate limit", "too many requests", "HTTP 429",
                     "401 Unauthorized", "status 403", "529 overloaded", "billing problem", "no credits left",
                     "access token expired", "unauthorized", "authentication failed", "codex_login",
                     "error code: 429", "STATUS =403", "HTTP401", "403 Forbidden", "401 Unauthorised", "429 too many"):
            self.assertTrue(M.PROVIDER.search(text), text)
        for text in ("ValueError: bad input", "2 tests failed", "ENOSPC: no space left on device",
                     "AssertionError: expected 401, got 200", "line 403 of file", "took 529 ms", "status....429"):
            self.assertFalse(M.PROVIDER.search(text), text)


class Stats(unittest.TestCase):
    def add(self, root, run, tid, *, tier="low", worker="w", model="m", status="done",
            started="2026-11-18 10:00:00", finished="2026-11-18 10:10:00", changes="1 files +1 -0",
            sha="sha", review=None, usage=None, retry_of="", failure=""):
        t = pathlib.Path(root, run, tid)
        t.mkdir(parents=True)
        lines = [f"workers={worker or 'w'}", f"tier={tier}", f"model={model}",
                 f"retry_of={retry_of}"]
        if changes is not None:
            lines.append(f"changes={changes}")
        if worker:
            lines += [f"worker={worker}", "type=claude"]
        if sha:
            lines.append(f"changes_sha256={sha}")
        if started:
            lines += [f"started={started}", f"finished={finished}"]
        if failure:
            lines.append(f"failure={failure}")
        (t / "meta").write_text("\n".join(lines) + "\n")
        (t / "status").write_text(status + "\n")
        review = f"Verdict: Approved\nchanges-sha256: {sha}\n" if review is None and sha else review
        if review is not None:
            (t / "review.md").write_text(review)
        if usage is not None:
            (t / "usage").write_text(json.dumps(usage))
        return t

    @staticmethod
    def usage(inp, out, usd, billing="metered", model="m"):
        return {"model": model, "input": inp, "output": out, "usd": usd, "billing": billing}

    @staticmethod
    def table(out):
        lines = out.splitlines()
        head = lines[0].split("\t")
        return [dict(zip(head, line.split("\t"))) for line in lines[1:] if "\t" in line]

    def rows(self, out):
        return {row["worker"]: row for row in self.table(out)}

    def test_first_pass_fix_round_and_cross_run_retry(self):
        with tempfile.TemporaryDirectory() as d:
            r1, r2 = "20261117-1200-one", "20261118-0900-two"
            self.add(d, r1, "first", worker="w1", usage=self.usage(100, 20, 1.0))
            self.add(d, r1, "fixed", worker="w2", sha="fx1",
                     review="Verdict: Needs fixes\nchanges-sha256: fx1\n")
            self.add(d, r1, "fixed-r2", worker="w2", sha="fx2", retry_of=f"{r1}/fixed",
                     started="2026-11-18 10:20:00", finished="2026-11-18 10:30:00",
                     review="Verdict: Approved\nchanges-sha256: fx2\n")
            self.add(d, r1, "cross", worker="w3", tier="standard", sha="cx1",
                     review="Verdict: Needs fixes\nchanges-sha256: cx1\n")
            self.add(d, r2, "cross", worker="w4", tier="standard", sha="cx2", retry_of=f"{r1}/cross",
                     started="2026-11-18 11:20:00", finished="2026-11-18 11:30:00",
                     review="Verdict: Approved\nchanges-sha256: cx2\n")
            out = M.stats({"runsDir": d})
            rows = self.rows(out)
            self.assertEqual(sorted(rows), ["w1", "w2", "w3"])  # the run-2 attempt joins w3's work
            self.assertEqual((rows["w1"]["settled"], rows["w1"]["accepted"], rows["w1"]["first_pass"],
                              rows["w1"]["attempts_per_work"]), ("1", "1", "100%", "1.0"))
            self.assertEqual((rows["w2"]["settled"], rows["w2"]["accepted"], rows["w2"]["first_pass"],
                              rows["w2"]["attempts_per_work"], rows["w2"]["median_minutes"]),
                             ("1", "1", "0%", "2.0", "20.0"))
            self.assertEqual((rows["w3"]["settled"], rows["w3"]["accepted"], rows["w3"]["attempts_per_work"]),
                             ("1", "1", "2.0"))
            self.assertIn("API-equivalent", out)
            self.assertIn("small samples", out)

    def test_provider_failure_or_interruption_then_success_is_first_pass_and_cost_counts(self):
        for failure, reason in (("provider", "usage limit"), ("interrupted", "interrupted (SIGTERM)"),
                                ("infra", "stream disconnected")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                run = "20261118-1000-run"
                self.add(d, run, "A", status=f"failed: {reason}", failure=failure, sha="", review="",
                         usage=self.usage(10, 5, 2.0))
                self.add(d, run, "A-r2", sha="s2", retry_of=f"{run}/A", started="2026-11-18 10:20:00",
                         finished="2026-11-18 10:30:00", usage=self.usage(20, 5, 1.0),
                         review="Verdict: Approved\nchanges-sha256: s2\n")
                row = self.table(M.stats({"runsDir": d}))[0]
                self.assertEqual((row["settled"], row["accepted"], row["first_pass"], row["attempts_per_work"]),
                                 ("1", "1", "100%", "1.0"))
                self.assertEqual((row["tokens_per_accepted"], row["usd_per_accepted"]), ("40", "3.00"))

    def test_a_canceled_attempt_or_one_that_never_started_does_not_change_the_result(self):
        with tempfile.TemporaryDirectory() as d:
            run = "20261118-1100-run"
            self.add(d, run, "A", sha="a1", usage=self.usage(10, 0, 1.0))
            self.add(d, run, "A-r2", status="canceled: not needed", sha="", review="", retry_of=f"{run}/A",
                     started="2026-11-18 11:20:00", usage=self.usage(50, 0, 9.0))
            self.add(d, run, "A-r3", status="queued", worker="", sha="", review="", retry_of=f"{run}/A",
                     started="")
            row = self.table(M.stats({"runsDir": d}))[0]
            self.assertEqual((row["settled"], row["accepted"], row["first_pass"], row["attempts_per_work"]),
                             ("1", "1", "100%", "1.0"))
            self.assertEqual((row["tokens_per_accepted"], row["usd_per_accepted"]), ("10", "1.00"))

    def test_a_stale_review_is_not_accepted_and_a_read_only_needs_fixes_is(self):
        with tempfile.TemporaryDirectory() as d:
            run = "20261118-1200-run"
            self.add(d, run, "stale", worker="stale", sha="current",
                     review="Verdict: Approved\nchanges-sha256: earlier\n")
            self.add(d, run, "read-only", worker="read-only", tier="standard", changes="0 files +0 -0",
                     sha="ro", review="Verdict: Needs fixes\nchanges-sha256: ro\n")
            rows = self.rows(M.stats({"runsDir": d}))
            self.assertEqual((rows["stale"]["accepted"], rows["stale"]["first_pass"],
                              rows["stale"]["tokens_per_accepted"], rows["stale"]["usd_per_accepted"]),
                             ("0", "-", "-", "-"))
            self.assertEqual((rows["stale"]["settled"], rows["stale"]["open"]), ("0", "1"))
            self.assertEqual((rows["read-only"]["accepted"], rows["read-only"]["first_pass"]), ("1", "100%"))

    def test_usd_per_accepted_divides_all_attempts_cost_by_accepted_work(self):
        with tempfile.TemporaryDirectory() as d:
            run = "20261118-1300-run"
            self.add(d, run, "good", sha="g", usage=self.usage(10, 5, 3.0))
            self.add(d, run, "bad", status="failed: ValueError", failure="task", changes=None, sha="", review="",
                     usage=self.usage(20, 5, 1.0))
            row = self.table(M.stats({"runsDir": d}))[0]
            self.assertEqual((row["settled"], row["accepted"], row["attempts_per_work"],
                              row["tokens_per_accepted"], row["usd_per_accepted"]),
                             ("2", "1", "1.0", "40", "4.00"))

    def test_legacy_work_is_excluded_from_rows_and_counted_once_in_footer(self):
        with tempfile.TemporaryDirectory() as d:
            self.add(d, "run", "accepted", usage=self.usage(10, 5, 1.0))
            before = M.stats({"runsDir": d})
            self.assertNotIn("before reviews were recorded", before)
            self.add(d, "run", "old", changes=None, sha="", usage=self.usage(90, 10, 8.0))
            self.add(d, "run", "retry", worker="other")
            self.add(d, "run", "retry-r2", worker="other", changes=None, sha="")
            after = M.stats({"runsDir": d})
            self.assertEqual(self.table(after), self.table(before))
            self.assertIn("2 pieces of work from before reviews were recorded are left out.", after.splitlines())

    def test_unreviewed_retry_changes_only_the_open_count(self):
        with tempfile.TemporaryDirectory() as d:
            self.add(d, "run", "accepted", usage=self.usage(10, 5, 1.0))
            before = self.table(M.stats({"runsDir": d}))
            self.add(d, "run", "pending", usage=self.usage(90, 10, 8.0, "subscription"))
            self.add(d, "run", "pending-r2", sha="", finished="2026-11-18 12:00:00")
            self.assertEqual(self.table(M.stats({"runsDir": d})), [dict(before[0], open="1")])

    def test_only_open_work_has_no_rates_or_billing(self):
        with tempfile.TemporaryDirectory() as d:
            for status in ("queued", "running"):
                self.add(d, "run", status, status=status, changes=None, sha="")
            self.add(d, "run", "needs-fixes", review="Verdict: Needs fixes\nchanges-sha256: sha\n",
                     usage=self.usage(10, 5, 1.0))
            row = self.table(M.stats({"runsDir": d}))[0]
            self.assertEqual((row["settled"], row["accepted"], row["open"]), ("0", "0", "3"))
            for column in ("first_pass", "attempts_per_work", "median_minutes", "tokens_per_accepted",
                           "usd_per_accepted", "billing"):
                self.assertEqual(row[column], "-", column)

    def test_work_waiting_for_a_rerun_after_a_failure_outside_the_task_is_open(self):
        for failure in M.NOT_TASK:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as d:
                self.add(d, "run", "task", status="failed: x", failure=failure, sha="", review="")
                row = self.table(M.stats({"runsDir": d}))[0]
                self.assertEqual((row["settled"], row["open"]), ("0", "1"))
        with tempfile.TemporaryDirectory() as d:
            self.add(d, "run", "task", status="failed: done check failed", failure="task", sha="", review="")
            row = self.table(M.stats({"runsDir": d}))[0]
            self.assertEqual((row["settled"], row["accepted"], row["open"]), ("1", "0", "0"))

    def test_partially_priced_settled_attempts_show_a_lower_bound(self):
        with tempfile.TemporaryDirectory() as d:
            self.add(d, "run", "task", status="failed: usage limit", failure="provider")
            self.add(d, "run", "task-r2", usage=self.usage(10, 5, 3.0))
            self.assertEqual(self.table(M.stats({"runsDir": d}))[0]["usd_per_accepted"], "≥3.00")

    def test_days_filters_runs_by_folder_date(self):
        with tempfile.TemporaryDirectory() as d:
            today = datetime.date.today()
            old = (today - datetime.timedelta(days=10)).strftime("%Y%m%d") + "-0000-old"
            new = today.strftime("%Y%m%d") + "-0000-new"
            self.add(d, old, "A", worker="old")
            self.add(d, new, "B", worker="new")
            self.assertEqual(sorted(self.rows(M.stats({"runsDir": d}, days=5))), ["new"])
            self.assertEqual(sorted(self.rows(M.stats({"runsDir": d}, days=1))), ["new"])
            self.assertEqual(sorted(self.rows(M.stats({"runsDir": d}))), ["new", "old"])

    def test_a_retry_of_loop_and_malformed_files_do_not_crash(self):
        with tempfile.TemporaryDirectory() as d:
            run = "20261118-1400-run"
            self.add(d, run, "a", sha="a", retry_of=f"{run}/b")
            self.add(d, run, "b", sha="b", retry_of=f"{run}/a")
            junk = pathlib.Path(d, run, "junk")
            junk.mkdir()
            (junk / "meta").write_text("not meta\nretry_of=missing/task\nstarted=not a time\n")
            (junk / "status").write_text("done\n")
            (junk / "usage").write_text("{not json")
            (junk / "review.md").write_text("Verdict: Approved\n")
            odd = pathlib.Path(d, run, "odd")
            odd.mkdir()
            (odd / "meta").write_text("worker=w\ntier=low\nstarted=2026-11-18 10:00:00\nchanges=1 files +1 -0\n")
            (odd / "status").write_text("done\n")
            (odd / "review.md").write_text("Verdict: Approved\n")
            (odd / "usage").write_text(json.dumps({"model": [], "input": [], "output": {}, "usd": "x",
                                                   "billing": []}))
            pathlib.Path(d, run, "empty").mkdir()
            out = M.stats({"runsDir": d})
            self.assertEqual(out.splitlines()[0], "tier\tworker\tmodel\tsettled\taccepted\topen\tfirst_pass\t"
                                                  "attempts_per_work\tmedian_minutes\ttokens_per_accepted\t"
                                                  "usd_per_accepted\tbilling")
            # a and b share a row, odd has malformed usage, and junk predates recorded changes.
            self.assertEqual(len(self.table(out)), 2)
            self.assertEqual(self.table(out)[0]["usd_per_accepted"], "-")
            self.assertEqual(self.table(out)[1]["settled"], "2")
