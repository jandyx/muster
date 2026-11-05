#!/usr/bin/env python3
"""Muster worker runner: run one task on one configured worker and record what happened.

    muster.py run <worker> <task-dir> <cwd>   run prompt.md, write status/meta/log/result/usage
    muster.py usage <task-dir>                (re)compute <task-dir>/usage for a finished task
    muster.py workers                         list configured workers

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
import subprocess
import sys

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


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_meta(task):
    try:
        text = open(os.path.join(task, "meta")).read()
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
    log = open(os.path.join(task, "log.txt"), errors="replace").read()
    m = re.search(r"^session id: (\S+)", log, re.M)
    files = glob.glob(os.path.join(home, "sessions", "**", f"rollout-*{m.group(1)}.jsonl"), recursive=True) if m else []
    if not files:
        return None
    model, prev, p, usd = "?", None, None, 0.0
    tot = dict(input=0, cached=0, output=0, reasoning=0)
    for line in open(files[0], errors="replace"):
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
    for line in open(os.path.join(task, "stream.jsonl"), errors="replace"):
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
            status = open(os.path.join(task, "status")).read().strip()
        except OSError:
            continue
        if status == "running" and pid_alive(int(m.get("pid", "0") or 0)):
            n += 1
    return n


def refuse(task, why, code):
    with open(os.path.join(task, "status"), "w") as f:
        f.write(f"failed: refused, {why}\n")
    print(f"muster: refused: {why}", file=sys.stderr)
    return code


# ---------- running ----------

def run(cfg, name, task, cwd):
    worker = cfg["workers"].get(name)
    if worker is None:
        sys.exit(f"muster: unknown worker {name!r} (known: {', '.join(cfg['workers'])})")
    prompt = os.path.join(task, "prompt.md")
    if not os.path.isfile(prompt) or not os.path.isdir(cwd):
        sys.exit(f"muster: need {prompt} and an existing cwd {cwd}")
    kind = worker["type"]
    if not allowed_here(cfg, name, cwd):
        return refuse(task, f"worker {name} is limited to {cfg['only'][name]} by `only`; {cwd} is outside", 3)
    cap = int((cfg.get("caps") or {}).get(name, (cfg.get("caps") or {}).get(kind, DEFAULT_CAPS.get(kind, 3))))
    runs = expand(cfg.get("runsDir", "~/.muster/runs"))
    os.makedirs(runs, exist_ok=True)
    # The lock makes count-then-claim atomic, so tasks launched together cannot all slip under the cap.
    with open(os.path.join(runs, ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        busy = running_on(cfg, name)
        if busy >= cap:
            return refuse(task, f"worker {name} already runs {busy} of {cap} tasks (caps); launch this one when a slot frees", 4)
        with open(os.path.join(task, "meta"), "w") as f:
            f.write(f"worker={name}\ntype={kind}\ncwd={cwd}\nsession={os.environ.get('CLAUDE_CODE_SESSION_ID', '')}\n"
                    f"pid={os.getpid()}\nstarted={now()}\n")
        with open(os.path.join(task, "status"), "w") as f:
            f.write("running\n")

    env = dict(os.environ)
    extra = [expand(a) for a in worker.get("args", [])]
    with open(prompt) as stdin, open(os.path.join(task, "log.txt"), "w") as log:
        if kind == "codex":
            env["CODEX_HOME"] = expand(worker.get("codexHome", "~/.codex"))
            cmd = [expand(worker.get("command", "codex")), "exec", "--dangerously-bypass-approvals-and-sandbox",
                   "--skip-git-repo-check", "-C", cwd, "-o", os.path.join(task, "result.md"), *extra, "-"]
            rc = subprocess.call(cmd, stdin=stdin, stdout=log, stderr=subprocess.STDOUT, cwd=cwd, env=env)
        elif kind == "claude":
            cmd = [expand(worker.get("command", "claude")), "-p", "--output-format", "stream-json", "--verbose", *extra]
            with open(os.path.join(task, "stream.jsonl"), "w") as out:
                rc = subprocess.call(cmd, stdin=stdin, stdout=out, stderr=log, cwd=cwd, env=env)
        else:
            sys.exit(f"muster: worker {name!r} has unknown type {kind!r} (codex or claude)")
    try:
        write_usage(task, cfg)
    except Exception as e:  # usage is best effort; the task result matters more
        with open(os.path.join(task, "log.txt"), "a") as log:
            log.write(f"\nmuster: usage not recorded: {e}\n")
    append_meta(task, finished=now(), exit=rc)
    with open(os.path.join(task, "status"), "w") as f:
        f.write("done\n" if rc == 0 else f"failed: exit {rc}\n")
    return rc


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    cfg = load_config()
    if cmd == "run" and len(args) == 3:
        sys.exit(run(cfg, *args))
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
