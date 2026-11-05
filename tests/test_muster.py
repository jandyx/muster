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
        return {"runsDir": self.d, "workers": {"w": {"type": "claude", "command": "/bin/true"}}, **extra}

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
        self.assertEqual(M.run(self.cfg(caps={"w": 1}), "w", str(self.task), str(self.repo)), 4)
        self.assertIn("1 of 1", (self.task / "status").read_text())


if __name__ == "__main__":
    unittest.main()
