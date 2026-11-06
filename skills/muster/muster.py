#!/usr/bin/env python3
"""Muster worker runner: run one task on one configured worker and record what happened.

    muster.py run <worker[,worker…]> <task-dir> <cwd> [--after A,B] [--priority N]
                                              queue until deps are done and one of the workers has a free slot
                                              (first in the list wins a tie), then run prompt.md on it
    muster.py cancel <task-dir> [reason]      stop a queued or running task; status becomes "canceled: reason"
    muster.py usage <task-dir>                (re)compute <task-dir>/usage for a finished task
    muster.py workers                         list configured workers
    muster.py detect                          JSON: Codex homes and claude-like CLIs found on this machine
    muster.py doctor                          check the config; exit 1 when something is wrong

Config: ~/.muster/config.json (MUSTER_CONFIG overrides). Prices: prices.json at the repo
root, overridden key by key by ~/.muster/prices.json. Standard library only.
"""
import datetime
import fcntl
import fnmatch
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CONFIG = os.path.expanduser(os.environ.get("MUSTER_CONFIG", "~/.muster/config.json"))
REPO_PRICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "prices.json")
USER_PRICES = os.path.expanduser("~/.muster/prices.json")


def expand(p):
    return os.path.expanduser(os.path.expandvars(p))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_config():
    cfg = load_json(CONFIG, None)
    if cfg is None:
        sys.exit(f"muster: no config at {CONFIG} (copy examples/config.example.json there)")
    return cfg


def load_prices():
    prices = load_json(REPO_PRICES, {})
    prices.update(load_json(USER_PRICES, {}))
    return prices


def read_text(path, limit=None):
    with open(path, "rb") as f:
        return f.read(limit).decode("utf-8", "replace") if limit else f.read().decode("utf-8", "replace")


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_meta(task):
    try:
        text = read_text(os.path.join(task, "meta"))
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def append_meta(task, **kv):
    with open(os.path.join(task, "meta"), "a") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


# ---------- pricing ----------

def price_for(prices, model):
    return prices.get(re.sub(r"\[.*?\]$", "", model or ""))  # deepseek-flash[1m] -> deepseek-flash


def is_peak(p, when_utc):
    # shortcut: the task's start time picks peak/off-peak for the whole task; public holidays are ignored.
    if "peak" not in p or when_utc.weekday() >= 5:
        return False
    return any(a <= when_utc.hour < b for a, b in p.get("peak_utc_weekdays", []))


def cost(rate, inp, cached, out):
    return ((inp - cached) * rate["input"] + cached * rate["cached"] + out * rate["output"]) / 1e6


def codex_billing(home):
    # A ChatGPT login is a subscription (no per-token bill); an API key is metered.
    auth = load_json(os.path.join(home, "auth.json"), {})
    return "subscription" if auth.get("auth_mode") == "chatgpt" else "metered"


def usage_codex(task, home, prices, billing):
    log = read_text(os.path.join(task, "log.txt"))
    m = re.search(r"^session id: (\S+)", log, re.M)
    files = glob.glob(os.path.join(home, "sessions", "**", f"rollout-*{m.group(1)}.jsonl"), recursive=True) if m else []
    if not files:
        return None
    model, prev, p, usd = "?", None, None, 0.0
    tot = dict(input=0, cached=0, output=0, reasoning=0)
    for line in read_text(files[0]).splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        pl = d.get("payload") or {}
        if isinstance(pl.get("model"), str):
            model, p = pl["model"], price_for(prices, pl["model"])
        info = pl.get("info") if pl.get("type") == "token_count" else None
        if not info:
            continue
        total = info["total_token_usage"]["total_tokens"]
        if total == prev:  # the same count emitted twice
            continue
        prev = total
        last = info["last_token_usage"]
        inp, cached, out = last["input_tokens"], last["cached_input_tokens"], last["output_tokens"]
        tot["input"] += inp
        tot["cached"] += cached
        tot["output"] += out
        tot["reasoning"] += last.get("reasoning_output_tokens", 0)
        if p:
            rate = p["long"] if "long" in p and inp > p.get("long_context_over", 1 << 62) else p
            usd += cost(rate, inp, cached, out)
    return dict(model=model, **tot, usd=round(usd, 4) if p else None, billing=billing)


def usage_claude(task, prices, billing, started_utc):
    # Reads `claude -p --output-format stream-json` (last "result" line) or plain json output.
    d = None
    for line in read_text(os.path.join(task, "stream.jsonl")).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") == "result" or (d is None and "modelUsage" in o):
            d = o
    if d is None:
        return None
    with open(os.path.join(task, "result.md"), "w") as f:
        f.write(d.get("result") or "")
    tot = dict(input=0, cached=0, output=0, reasoning=0)
    usd, priced, models, peak = 0.0, True, [], False
    for model, u in (d.get("modelUsage") or {}).items():
        models.append(model)
        cached = u.get("cacheReadInputTokens", 0)
        inp = u.get("inputTokens", 0) + u.get("cacheCreationInputTokens", 0) + cached
        out = u.get("outputTokens", 0)
        tot["input"] += inp
        tot["cached"] += cached
        tot["output"] += out
        tot["reasoning"] += u.get("thinkingTokens", 0)
        p = price_for(prices, model)
        if p:
            pk = is_peak(p, started_utc)
            peak = peak or pk
            usd += cost(p["peak"] if pk else p, inp, cached, out)
        elif u.get("costBasis") not in (None, "unknown") and u.get("costUSD") is not None:
            usd += u["costUSD"]  # Claude Code priced a model it knows
        else:
            priced = False
    return dict(model=",".join(models) or "?", **tot, usd=round(usd, 4) if priced else None,
                billing=billing, peak=peak, is_error=bool(d.get("is_error")))


def write_usage(task, cfg):
    meta = read_meta(task)
    worker = cfg["workers"].get(meta.get("worker", ""), {})
    prices = load_prices()
    started = datetime.datetime.strptime(meta["started"], "%Y-%m-%d %H:%M:%S").astimezone(datetime.timezone.utc)
    if meta.get("type") == "codex":
        home = expand(worker.get("codexHome", "~/.codex"))
        u = usage_codex(task, home, prices, worker.get("billing") or codex_billing(home))
    else:
        u = usage_claude(task, prices, worker.get("billing", "metered"), started)
    if u:
        with open(os.path.join(task, "usage"), "w") as f:
            json.dump(u, f)
    return u


# ---------- guards ----------

DEFAULT_CAPS = {"codex": 3, "claude": 5}


def allowed_here(cfg, name, cwd):
    # `only`: a fenced worker runs only where one of its globs matches the cwd (as given or resolved).
    globs = (cfg.get("only") or {}).get(name)
    if not globs:
        return True
    paths = {os.path.abspath(cwd), os.path.realpath(cwd)}
    pats = [os.path.abspath(expand(g)) for g in globs]
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, pat + "/*") for p in paths for pat in pats)


def pid_alive(pid):
    if pid <= 0:  # no pid recorded; kill(0) would signal our own process group and look alive
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def running_on(cfg, name):
    # Tasks of this worker still running: status says so and the runner's pid is alive.
    runs = expand(cfg.get("runsDir", "~/.muster/runs"))
    n = 0
    for meta in glob.glob(os.path.join(runs, "*", "*", "meta")):
        task = os.path.dirname(meta)
        m = read_meta(task)
        if m.get("worker") != name or "finished" in m:
            continue
        try:
            status = read_text(os.path.join(task, "status")).strip()
        except OSError:
            continue
        if status == "running" and pid_alive(int(m.get("pid", "0") or 0)):
            n += 1
    return n


def live(m, status):
    # A queued or running task whose runner process is still there.
    return status.startswith(("queued", "running")) and "finished" not in m and pid_alive(int(m.get("pid", "0") or 0))


def dep_state(task, after):
    """'ready' when every task in `after` (sibling ids) is done, 'wait: …' while one is not, 'broken: …' when one never will be."""
    waiting = []
    for dep in after:
        d = os.path.join(os.path.dirname(task), dep)
        try:
            status = read_text(os.path.join(d, "status")).strip()
        except OSError:
            status = ""
        if status == "done":
            continue
        if status.startswith(("failed", "canceled")):
            return f"broken: {dep} {status.split(':')[0]}"
        if status.startswith(("queued", "running")) and not live(read_meta(d), status):
            return f"broken: {dep} lost"
        waiting.append(dep)
    return f"wait: {', '.join(waiting)}" if waiting else "ready"


def queue_head(cfg, name):
    # Queued tasks that may start on this worker and whose deps are done, in start order:
    # higher priority first, then first queued.
    runs = expand(cfg.get("runsDir", "~/.muster/runs"))
    ready = []
    for meta in glob.glob(os.path.join(runs, "*", "*", "meta")):
        task = os.path.dirname(meta)
        m = read_meta(task)
        if name not in m.get("workers", "").split(","):
            continue
        try:
            status = read_text(os.path.join(task, "status")).strip()
        except OSError:
            continue
        after = [a for a in m.get("after", "").split(",") if a]
        if status.startswith("queued") and live(m, status) and dep_state(task, after) == "ready":
            ready.append((-int(m.get("priority", "0") or 0), m.get("queued", ""), os.path.realpath(task)))
    return [t for _, _, t in sorted(ready)]


def cap_of(cfg, name):
    caps, kind = cfg.get("caps") or {}, cfg["workers"][name]["type"]
    return int(caps.get(name, caps.get(kind, DEFAULT_CAPS.get(kind, 3))))


def refuse(task, why, code):
    with open(os.path.join(task, "status"), "w") as f:
        f.write(f"failed: refused, {why}\n")
    print(f"muster: refused: {why}", file=sys.stderr)
    return code


# ---------- setup helpers ----------

def detect():
    """What an agent needs to propose a config: Codex homes and claude-like commands on PATH or in ~/.local/bin."""
    homes = []
    for home in [os.environ.get("CODEX_HOME"), "~/.codex", *sorted(glob.glob(expand("~/.codex-homes/*")))]:
        if not home:
            continue
        home = expand(home)
        if os.path.isfile(os.path.join(home, "auth.json")) or os.path.isfile(os.path.join(home, "config.toml")):
            homes.append({"codexHome": home.replace(HOME, "~", 1), "billing": codex_billing(home),
                          "logged_in": os.path.isfile(os.path.join(home, "auth.json"))})
    seen, commands = set(), []
    dirs = os.environ.get("PATH", "").split(os.pathsep) + [expand("~/.local/bin")]
    for d in dirs:
        for f in sorted(glob.glob(os.path.join(d, "*"))):
            name = os.path.basename(f)
            if name in seen or not os.access(f, os.X_OK) or os.path.isdir(f):
                continue
            if name in ("codex", "claude"):
                kind = name
            elif "." in name:
                continue  # backups such as tool.bak-2026...
            else:
                # A small script that sets ANTHROPIC_BASE_URL and execs a claude binary: Claude Code on another backend.
                try:
                    head = read_text(f, 16384) if os.path.getsize(f) < 65536 else ""
                except OSError:
                    head = ""
                if not ("ANTHROPIC_BASE_URL" in head and re.search(r"\bclaude\b", head)):
                    continue
                kind = "claude-wrapper"
            seen.add(name)
            commands.append({"name": name, "kind": kind, "path": f.replace(HOME, "~", 1)})
    return {"codex_homes": homes, "commands": commands, "config": CONFIG.replace(HOME, "~", 1),
            "config_exists": os.path.isfile(CONFIG)}


def doctor(cfg):
    """Problems with the config, one line each; empty when it is usable."""
    probs = []
    workers = cfg.get("workers") or {}
    if not workers:
        probs.append("workers: none configured")
    for name, w in workers.items():
        kind = w.get("type")
        if kind not in ("codex", "claude"):
            probs.append(f"workers.{name}.type: {kind!r}, expected 'codex' or 'claude'")
            continue
        cmd = expand(w.get("command", kind))
        if shutil.which(cmd) is None:
            probs.append(f"workers.{name}.command: {cmd} not found or not executable")
        if kind == "codex":
            home = expand(w.get("codexHome", "~/.codex"))
            if not os.path.isfile(os.path.join(home, "auth.json")):
                probs.append(f"workers.{name}.codexHome: no auth.json in {home} (log in with CODEX_HOME={home} codex login)")
    for i, r in enumerate(cfg.get("routing") or []):
        for w in r.get("workers", []):
            if w not in workers:
                probs.append(f"routing[{i}] ({r.get('match')}): unknown worker {w!r}")
    if cfg.get("routing") and not any(r.get("match") == "*" for r in cfg["routing"]):
        probs.append("routing: no '*' fallback entry; tasks outside every match have no default worker")
    for w in (cfg.get("only") or {}):
        if w not in workers:
            probs.append(f"only.{w}: unknown worker")
    for k, v in (cfg.get("caps") or {}).items():
        if k not in workers and k not in ("codex", "claude"):
            probs.append(f"caps.{k}: not a worker name or type")
        elif not isinstance(v, int) or v < 1:
            probs.append(f"caps.{k}: {v!r}, expected a whole number of at least 1")
    return probs


# ---------- running ----------

QUEUE_POLL_S = 2.0
_child = None  # the worker process, once started; one task per runner process


def finish(task, rc, status):
    append_meta(task, finished=now(), exit=rc)
    with open(os.path.join(task, "status"), "w") as f:
        f.write(status + "\n")


def write_status(task, status):
    with open(os.path.join(task, "status"), "w") as f:
        f.write(status + "\n")


def on_stop(task):
    """If the runner is told to stop, queued or running, stop the worker and still record a final status:
    `canceled: …` when `muster.py cancel` asked for it, `failed: interrupted` otherwise."""
    def stop(signum, _frame):
        if _child is not None:
            _child.terminate()
            try:
                _child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _child.kill()
        reason = read_meta(task).get("canceled")
        if reason is None:
            finish(task, 128 + signum, f"failed: interrupted ({signal.Signals(signum).name})")
        else:
            finish(task, 128 + signum, f"canceled: {reason}" if reason else "canceled")
        sys.exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop)


def supervise(task, cmd, **popen):
    global _child
    _child = subprocess.Popen(cmd, **popen)
    return _child.wait()


def cancel(task, reason=""):
    """Cancel a task that has not finished: signal its runner, or mark it when no runner is there."""
    m = read_meta(task)
    if "finished" in m:
        print(f"muster: {task} already finished", file=sys.stderr)
        return 1
    try:
        status = read_text(os.path.join(task, "status")).strip()
    except OSError:
        status = ""
    append_meta(task, canceled=reason)
    if live(m, status):
        os.kill(int(m["pid"]), signal.SIGTERM)
        for _ in range(150):  # the runner gives the worker 10s to stop before it kills it
            if "finished" in read_meta(task):
                return 0
            time.sleep(0.1)
    finish(task, 0, f"canceled: {reason}" if reason else "canceled")
    return 0


def run(cfg, names, task, cwd, after=(), priority=0):
    # `names`: one worker or several ("a,b"); the task waits for whichever frees a slot first.
    names = [n for n in (names.split(",") if isinstance(names, str) else names) if n]
    for name in names:
        if name not in cfg["workers"]:
            sys.exit(f"muster: unknown worker {name!r} (known: {', '.join(cfg['workers'])})")
    prompt = os.path.join(task, "prompt.md")
    if not os.path.isfile(prompt) or not os.path.isdir(cwd):
        sys.exit(f"muster: need {prompt} and an existing cwd {cwd}")
    for dep in after:
        if not os.path.isdir(os.path.join(os.path.dirname(task), dep)):
            sys.exit(f"muster: --after {dep}: no task {dep} next to {task}")
    fenced = [n for n in names if not allowed_here(cfg, n, cwd)]
    names = [n for n in names if n not in fenced]
    if not names:
        limits = "; ".join(f"{n} to {cfg['only'][n]}" for n in fenced)
        return refuse(task, f"`only` limits {limits}; {cwd} is outside", 3)
    try:
        if read_text(os.path.join(task, "status")).startswith("canceled"):
            return 0  # canceled before its runner started
    except OSError:
        pass
    runs = expand(cfg.get("runsDir", "~/.muster/runs"))
    os.makedirs(runs, exist_ok=True)
    # `worker` and `type` are written when the task starts; until then `workers` lists where it may go.
    with open(os.path.join(task, "meta"), "w") as f:
        f.write(f"workers={','.join(names)}\ncwd={cwd}\nsession={os.environ.get('CLAUDE_CODE_SESSION_ID', '')}\n"
                f"pid={os.getpid()}\nqueued={now()}\nafter={','.join(after)}\npriority={priority}\n")
    write_status(task, "queued")
    on_stop(task)
    shown, name = "queued", None
    while name is None:
        # The lock makes count-then-claim atomic, so queued tasks cannot all slip under a cap at once.
        with open(os.path.join(runs, ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            deps = dep_state(task, after)
            if deps.startswith("broken"):
                finish(task, 5, f"canceled: {deps[8:]}")
                return 5
            if deps == "ready":
                full = []
                for n in names:
                    busy, cap = running_on(cfg, n), cap_of(cfg, n)
                    if busy < cap and os.path.realpath(task) in queue_head(cfg, n)[:cap - busy]:
                        name = n
                        break
                    full.append(f"{n} runs {busy} of {cap}")
                if name is not None:
                    append_meta(task, worker=name, type=cfg["workers"][name]["type"], started=now())
                    write_status(task, "running")
                    break
                status = f"queued, {', '.join(full)}"
            else:
                status = f"queued, waiting for {deps[6:]}"
            if status != shown:
                write_status(task, status)
                shown = status
        time.sleep(QUEUE_POLL_S)

    worker = cfg["workers"][name]
    kind = worker["type"]
    env = dict(os.environ)
    extra = [expand(a) for a in worker.get("args", [])]
    with open(prompt) as stdin, open(os.path.join(task, "log.txt"), "w") as log:
        if kind == "codex":
            env["CODEX_HOME"] = expand(worker.get("codexHome", "~/.codex"))
            cmd = [expand(worker.get("command", "codex")), "exec", "--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check", "-C", cwd, "-o", os.path.join(task, "result.md"), *extra, "-"]
            rc = supervise(task, cmd, stdin=stdin, stdout=log, stderr=subprocess.STDOUT, cwd=cwd, env=env)
        elif kind == "claude":
            cmd = [expand(worker.get("command", "claude")), "-p", "--output-format", "stream-json", "--verbose", *extra]
            with open(os.path.join(task, "stream.jsonl"), "w") as out:
                rc = supervise(task, cmd, stdin=stdin, stdout=out, stderr=log, cwd=cwd, env=env)
        else:
            sys.exit(f"muster: worker {name!r} has unknown type {kind!r} (codex or claude)")
    try:
        write_usage(task, cfg)
    except Exception as e:  # usage is best effort; the task result matters more
        with open(os.path.join(task, "log.txt"), "a") as log:
            log.write(f"\nmuster: usage not recorded: {e}\n")
    finish(task, rc, "done" if rc == 0 else f"failed: exit {rc}")
    return rc


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "detect":  # works before any config exists
        print(json.dumps(detect(), indent=2))
        return
    cfg = load_config()
    if cmd == "doctor":
        probs = doctor(cfg)
        print("\n".join(probs) if probs else f"ok: {len(cfg['workers'])} workers, config {CONFIG}")
        sys.exit(1 if probs else 0)
    if cmd == "run" and len(args) >= 3:
        opts = dict(zip(args[3::2], args[4::2]))
        if len(args) % 2 == 0 or set(opts) - {"--after", "--priority"}:
            sys.exit(__doc__)
        after = [a for a in opts.get("--after", "").split(",") if a]
        sys.exit(run(cfg, *args[:3], after=after, priority=int(opts.get("--priority", "0"))))
    if cmd == "cancel" and len(args) in (1, 2):
        sys.exit(cancel(args[0], args[1] if len(args) == 2 else ""))
    if cmd == "usage" and len(args) == 1:
        print(json.dumps(write_usage(args[0], cfg)))
        return
    if cmd == "workers":
        for name, w in cfg["workers"].items():
            print(f"{name}\t{w['type']}\t{w.get('codexHome') or w.get('command', '')}")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
