"""Self-check for skills/muster/muster.py: usage math and a full run with a fake claude worker.
Run: python3 -m unittest discover -s tests"""
import datetime, importlib.util, json, os, pathlib, stat, sys, tempfile, unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("muster", ROOT / "skills/muster/muster.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)
PRICES = json.loads((ROOT / "prices.json").read_text())


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
        cfg = {"workers": {"a": {"type": "codex", "codexHome": "/nonexistent", "command": sys.executable},
                           "b": {"type": "cli"}},
               "routing": [{"match": "~/x/*", "workers": ["a", "ghost"]}],
               "only": {"nobody": ["*"]}, "caps": {"a": 0}}
        probs = "\n".join(M.doctor(cfg))
        for needle in ("workers.a.codexHome", "workers.b.type", "unknown worker 'ghost'",
                       "no '*' fallback", "only.nobody", "caps.a"):
            self.assertIn(needle, probs)

    def test_doctor_passes_a_good_config(self):
        cfg = {"workers": {"c": {"type": "claude", "command": sys.executable}},
               "routing": [{"match": "*", "workers": ["c"]}], "caps": {"claude": 2}}
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


def make_task(root, title="t"):
    task = pathlib.Path(root, "run", "A")
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
    def test_disk_low_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as d:
            task = make_task(d)
            ok = fake_cli(d, "ok", "cat >/dev/null\n")
            cfg = {"runsDir": d, "minFreeGB": 1e9, "workers": {"w": {"type": "claude", "command": ok}}}
            self.assertEqual(M.run(cfg, "w", str(task), d), 4)
            status = (task / "status").read_text().strip()
            self.assertRegex(status, r"^failed: disk low \([\d.]+G free on /.*, need 1e\+09G\)$")
            self.assertEqual(M.read_meta(str(task))["last_error"], status[len("failed: "):])
            cfg["workers"]["w"]["minFreeGB"] = 0  # a worker's own threshold beats the global one
            self.assertEqual(M.run(cfg, "w", str(task), d), 0)

    def test_disk_is_checked_again_when_a_queued_task_starts(self):
        with tempfile.TemporaryDirectory() as d:
            task = make_task(d)
            cfg = {"runsDir": d, "workers": {"w": {"type": "claude", "command": fake_cli(d, "ok", "cat >/dev/null\n")}}}
            calls = []
            real = M.free_gb
            M.free_gb = lambda p: (calls.append(p), 100 if len(calls) <= 2 else 1)[1]  # fills up while queued
            try:
                self.assertEqual(M.run(cfg, "w", str(task), d), 4)
            finally:
                M.free_gb = real
            self.assertEqual(len(calls), 3)
            self.assertNotIn("worker", M.read_meta(str(task)))

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

    def test_doctor_checks_min_free_gb(self):
        probs = M.doctor({"minFreeGB": "10", "workers": {"w": {"type": "claude", "command": sys.executable, "minFreeGB": -1}}})
        self.assertEqual([p.split(":")[0] for p in probs], ["minFreeGB", "workers.w.minFreeGB"])


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


class Cli(unittest.TestCase):
    def run_cli(self, cfg, *args):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, "config.json")
            p.write_text(json.dumps(cfg))
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
