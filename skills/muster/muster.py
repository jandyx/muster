#!/usr/bin/env python3
"""Muster worker runner: run one task on one configured worker and record what happened.

    muster.py run <worker[,worker…]> <task-dir> <cwd> [--after A,B] [--priority N] [--check CMD]
                                              queue until deps are done and one of the workers has a free slot
                                              (first in the list wins a tie), then run prompt.md on it; then run
                                              CMD (sh -c, in cwd) and fail the task if it fails;
                                              exit 3 refused (`only`), 5 dependency failed, 1 the worker reported
                                              BLOCKED or NEEDS_CONTEXT
    muster.py cancel <task-dir> [reason]      stop a queued or running task; status becomes "canceled: reason"
    muster.py watch <run-dir> [--stall-min N] wait until every task has finished or a running worker's log has been
                                              silent for N minutes (default 10), and say which; stops nothing
    muster.py usage <task-dir>                (re)compute <task-dir>/usage for a finished task
    muster.py clean [--days N]                list what could be removed (runs untouched for N days, default 14;
                                              task worktrees; muster/* branches) with the command for each; deletes
                                              nothing
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
import hashlib
import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
CONFIG = os.path.expanduser(os.environ.get("MUSTER_CONFIG", "~/.muster/config.json"))
REPO_PRICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "prices.json")
USER_PRICES = os.path.expanduser("~/.muster/prices.json")
# Task ids are directory names and triple as the pane label, `--after` name and worktree branch suffix, so they
# follow the usual slug rule (DNS-1123 label, same as k8s resource names and Docker tags).
TASK_ID = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def expand(p):
    return os.path.expanduser(os.path.expandvars(p))


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# A string, or a `//` comment up to the end of the line. Comments go; strings stay, so `"https://…"` survives.
# hooks/register.tsx strips comments with the same pattern.
JSON_COMMENT = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*')


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        sys.exit(f"muster: no config at {CONFIG} (copy examples/config.example.json there)")
    except (OSError, ValueError) as e:  # unreadable, a directory, not UTF-8
        sys.exit(f"muster: cannot read {CONFIG}: {e}")
    try:
        cfg = json.loads(JSON_COMMENT.sub(lambda m: m[0] if m[0][0] == '"' else "", text))
    except ValueError as e:
        sys.exit(f"muster: {CONFIG} is not valid JSON: {e}")
    if not isinstance(cfg, dict):
        sys.exit(f"muster: {CONFIG} must hold a JSON object")
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


def record_model(task, kind):
    """meta `model` and `effort`, from the worker's own log: codex prints `model:` and `reasoning effort:`
    in a header after its banner (MCP errors can precede it, and no banner means no header); a claude worker names its model in the stream's init event."""
    found = {}
    try:
        if kind == "codex":
            text = read_text(os.path.join(task, "log.txt"))
            banner = re.search(r"^OpenAI Codex", text, re.M)
            head = text[banner.start():][:8192] if banner else ""
            for key, label in (("model", "model"), ("effort", "reasoning effort")):
                hit = re.search(rf"^{label}: (\S+)", head, re.M)
                if hit:
                    found[key] = hit[1]
        else:
            with open(os.path.join(task, "stream.jsonl"), errors="replace") as f:
                for _, line in zip(range(200), f):
                    o = json.loads(line) if line.startswith("{") else {}
                    if o.get("type") == "system" and o.get("subtype") == "init" and isinstance(o.get("model"), str):
                        found["model"] = o["model"]
                        break
    except (OSError, ValueError):
        pass
    if found:
        append_meta(task, **found)


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


def mount_of(path):
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


ERROR_LINE = re.compile(r"ENOSPC|no space left on device|error|panic|traceback|quota|rate.?limit", re.I)
ENOSPC = re.compile(r"ENOSPC|no space left on device", re.I)
SECRET = re.compile(r"(?i)\b(bearer\s+|(?:api[_-]?key|token|secret|password)\s*[=:]\s*)\S+"
                    r"|\b(sk-|xox[a-z]?-|ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_\-]+")


def redact(text):
    return SECRET.sub(lambda m: (m.group(1) or m.group(2)) + "***", text)


def tail_lines(path, size=65536):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - size))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def last_error(paths):
    """(line, path): the last line that looks like an error in the tails of `paths`, first path first;
    an ENOSPC line anywhere wins. stream-json lines count through their error text only."""
    hits = []
    for path in paths:
        for line in tail_lines(path):
            line = line.strip()
            if line.startswith("{"):
                try:
                    o = json.loads(line)
                except ValueError:
                    o = {}
                line = str(o.get("result") or o.get("error") or "") if isinstance(o, dict) and (o.get("is_error") or o.get("error")) else ""
            if ERROR_LINE.search(line):
                hits.append((re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s+", "", line), path))  # drop codex log timestamps
    disk = [h for h in hits if ENOSPC.search(h[0])]
    if disk:
        return disk[-1]
    for path in paths:
        mine = [h for h in hits if h[1] == path]
        if mine:
            return mine[-1]
    return None, None


def short(text, n=160):
    text = redact(re.sub(r"\s+", " ", text)).strip()
    return text if len(text) <= n else text[:n - 1] + "…"


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


KEYS = {"": {"runsDir", "workers", "routing", "only", "caps", "preamble"},
        "worker": {"type", "command", "args", "codexHome", "billing"},
        "route": {"match", "workers", "preamble"}}


def doctor(cfg):
    """Problems with the config, one line each; empty when it is usable."""
    probs = []

    def strings(v):
        return v is None or (isinstance(v, list) and all(isinstance(x, str) for x in v))

    def unknown(where, d, kind):
        # A typo such as "preambel" would otherwise be ignored without a word. Keys starting with "_" are notes.
        for k in d:
            if k not in KEYS[kind] and not k.startswith("_"):
                probs.append(f"{where}{k}: unknown key (expected one of {', '.join(sorted(KEYS[kind]))})")

    unknown("", cfg, "")
    if not isinstance(cfg.get("runsDir", ""), str):
        probs.append("runsDir: expected a path string")
    workers = cfg.get("workers") or {}
    if not isinstance(workers, dict):
        probs.append("workers: expected an object of name -> worker")
        workers = {}
    if not workers:
        probs.append("workers: none configured")
    for name, w in list(workers.items()):
        if not isinstance(w, dict):
            probs.append(f"workers.{name}: expected an object")
            del workers[name]
            continue
        unknown(f"workers.{name}.", w, "worker")
        if "args" in w and not (isinstance(w["args"], list) and strings(w["args"])):
            probs.append(f"workers.{name}.args: expected a list of strings")
        if w.get("billing") not in (None, "metered", "subscription"):
            probs.append(f"workers.{name}.billing: {w['billing']!r}, expected 'metered' or 'subscription'")
        for key in ("command", "codexHome"):
            if key in w and not isinstance(w[key], str):
                probs.append(f"workers.{name}.{key}: expected a path string")
                w = {k: v for k, v in w.items() if k != key}
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
    if not strings(cfg.get("preamble")):
        probs.append("preamble: expected a list of strings")
    routing = cfg.get("routing") or []
    if not isinstance(routing, list) or not all(isinstance(r, dict) for r in routing):
        probs.append("routing: expected a list of { match, workers } objects")
        routing = []
    for i, r in enumerate(routing):
        unknown(f"routing[{i}].", r, "route")
        if not isinstance(r.get("match"), str):
            probs.append(f"routing[{i}].match: expected a glob string")
        if not r.get("workers") or not strings(r.get("workers")):
            probs.append(f"routing[{i}] ({r.get('match')}).workers: expected a non-empty list of worker names")
            continue
        for w in r["workers"]:
            if w not in workers:
                probs.append(f"routing[{i}] ({r.get('match')}): unknown worker {w!r}")
        if not strings(r.get("preamble")):
            probs.append(f"routing[{i}] ({r.get('match')}).preamble: expected a list of strings")
    if routing and not any(r.get("match") == "*" for r in routing):
        probs.append("routing: no '*' fallback entry; tasks outside every match have no default worker")
    only, caps = cfg.get("only") or {}, cfg.get("caps") or {}
    if not isinstance(only, dict):
        probs.append("only: expected an object of worker -> globs")
        only = {}
    if not isinstance(caps, dict):
        probs.append("caps: expected an object of worker or type -> number")
        caps = {}
    for w, globs in only.items():
        if w not in workers:
            probs.append(f"only.{w}: unknown worker")
        if not globs or not strings(globs):
            probs.append(f"only.{w}: expected a non-empty list of globs")
    for k, v in caps.items():
        if k not in workers and k not in ("codex", "claude"):
            probs.append(f"caps.{k}: not a worker name or type")
        elif not isinstance(v, int) or isinstance(v, bool) or v < 1:
            probs.append(f"caps.{k}: {v!r}, expected a whole number of at least 1")
    return probs


# ---------- clean (lists, never deletes) ----------

def du_mb(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout.split()
    return int(out[0]) // 1024 if out and out[0].isdigit() else 0


def trunk_of(repo):
    """The branch work lands on: origin's HEAD, else main or master."""
    ref = (git(repo, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD") or "").strip()
    if ref:
        return ref
    for b in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{b}") is not None:
            return b
    return None


def unmerged(repo, trunk, branch):
    """Commits of branch with no equivalent in trunk (by patch, so rebased and cherry-picked work counts as merged),
    or None when git cannot tell. `git cherry` skips merge commits, so a branch with merges of its own counts as
    merged only when trunk already contains it."""
    if git(repo, "merge-base", "--is-ancestor", branch, trunk, ok=(0, 1)) is None:
        return None
    if git(repo, "merge-base", "--is-ancestor", branch, trunk) is not None:
        return 0
    merges = git(repo, "rev-list", "--merges", f"{trunk}..{branch}")
    out = git(repo, "cherry", trunk, branch)
    if merges is None or out is None:
        return None
    return sum(1 for line in out.splitlines() if line.startswith("+")) + len(merges.split())


def clean(cfg, days=14):
    """What could be removed: runs untouched for `days`, task worktrees and muster/* branches, each with the command
    that would remove it. Deletes nothing; the user picks."""
    runs = expand(cfg.get("runsDir", "~/.muster/runs"))
    cutoff, lines = time.time() - days * 86400, []
    old, total, repos, task_cwds = [], 0, {}, set()
    for name in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        d = os.path.join(runs, name)
        if not os.path.isdir(d):
            continue
        tasks = [t for t in sorted(glob.glob(os.path.join(d, "*"))) if os.path.isdir(t)]
        busy = False
        for t in tasks:
            m = read_meta(t)
            st = os.path.join(t, "status")
            busy = busy or live(m, read_text(st).strip() if os.path.isfile(st) else "")
            cwd = m.get("cwd")
            if cwd and os.path.isdir(cwd):
                task_cwds.add(os.path.realpath(cwd))
                common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
                if common:
                    repos.setdefault(os.path.dirname(common.strip().rstrip("/")), set())
        size = du_mb(d)
        total += size
        newest = max((os.path.getmtime(f) for f in glob.glob(os.path.join(d, "**"), recursive=True)), default=0)
        if newest < cutoff and not busy:
            old.append((name, int((time.time() - newest) // 86400), size, len(tasks)))
    lines.append(f"Runs in {runs}: {total} MB in all. Untouched for {days}+ days, none running:")
    lines += [f"  {n}  {age}d  {mb} MB  {k} tasks   rm -rf {shlex.quote(os.path.join(runs, n))}" for n, age, mb, k in old] \
        or ["  none"]
    for repo in sorted(repos):
        trunk = trunk_of(repo)
        lines.append(f"\n{repo} (trunk {trunk or 'unknown'}):")
        listed, seen_branches, merged = [], set(), []
        porcelain = git(repo, "worktree", "list", "--porcelain") or ""
        for block in porcelain.strip().split("\n\n")[1:]:  # the first one is the main checkout
            kv = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            path, branch = kv.get("worktree", ""), kv.get("branch", "").removeprefix("refs/heads/")
            if not (branch.startswith("muster/") or os.path.realpath(path) in task_cwds):
                continue
            seen_branches.add(branch)
            if not os.path.isdir(path):
                listed.append(f"  worktree {path}: folder is gone   git -C {shlex.quote(repo)} worktree prune")
                continue
            dirty = len((git(path, "status", "--porcelain") or "").splitlines())
            n = unmerged(repo, trunk, branch) if trunk and branch else None
            state = ("merged" if n == 0 else f"{n} commits not in {trunk}" if n else "merge state unknown") + \
                (f", {dirty} uncommitted changes" if dirty else "")
            cmd = f"git -C {shlex.quote(repo)} worktree remove {shlex.quote(path)}" + \
                (f" && git -C {shlex.quote(repo)} branch -D {shlex.quote(branch)}" if n == 0 and branch else "")
            listed.append(f"  worktree {path} [{branch or 'detached'}]: {state}, {du_mb(path)} MB   "
                          + (cmd if n == 0 and not dirty else f"# keep or check first: {cmd}"))
        refs = git(repo, "for-each-ref", "--format=%(refname:short)\t%(committerdate:unix)", "refs/heads/muster/") or ""
        for line in refs.splitlines():
            branch, when = line.split("\t")
            if branch in seen_branches:
                continue
            n = unmerged(repo, trunk, branch) if trunk else None
            age = int((time.time() - int(when)) // 86400)
            cmd = f"git -C {shlex.quote(repo)} branch -D {shlex.quote(branch)}"
            if n == 0:
                merged.append(branch)
            listed.append(f"  branch {branch}: " + ("merged" if n == 0 else f"{n} commits not in {trunk}" if n else
                                                     "merge state unknown") + f", last commit {age}d ago   "
                          + (cmd if n == 0 else f"# keep or check first: {cmd}"))
        lines += listed or ["  nothing to clean"]
        if len(merged) > 1:
            lines.append(f"  all {len(merged)} merged branches without a worktree:   git -C {shlex.quote(repo)} branch -D "
                         + " ".join(shlex.quote(b) for b in merged))
    lines.append("\nNothing was deleted. Lines starting with a command are safe to run; '# keep or check first' ones "
                 "hold work that is not in the trunk or not committed. Snapshot objects from changes.diff need no "
                 "cleanup: git's gc drops them.")
    return "\n".join(lines)


# ---------- running ----------

QUEUE_POLL_S = 2.0
WATCH_GRACE_S = 30.0  # how long `watch` waits for a launched task's runner to write its first status
_child = None  # the worker process, once started; one task per runner process


CHANGES_MAX = 5 * 1024 * 1024  # changes.diff is cut here; meta says so and how to see the rest


def git(cwd, *args, ok=(0,), env=None, timeout=120, input=None):
    """stdout of a git command in cwd, or None when cwd is not a repo or git fails. No optional locks, so it never
    races a worker's own git commands for index.lock."""
    try:
        r = subprocess.run(["git", "-C", cwd, "-c", "color.ui=never", *args], capture_output=True, text=True,
                           errors="replace", timeout=timeout, input=input,
                           env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env or {})})
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode in ok else None


def snapshot(top):
    """Tree id of the whole working tree as it is now: tracked and untracked files, minus ignored ones. Built in a
    throwaway copy of the index, so the real index and anything the worker staged stay as they are. It writes blob
    objects, which git's gc drops once nothing refers to them."""
    real = git(top, "rev-parse", "--path-format=absolute", "--git-path", "index")
    if real is None:
        return None
    fd, tmp = tempfile.mkstemp(prefix="muster-index-")
    os.close(fd)
    try:
        if os.path.isfile(real.strip()):
            shutil.copy2(real.strip(), tmp)  # keeps the stat cache and its timestamp, so only changed files are hashed
        else:
            os.unlink(tmp)  # a repo with no index yet: git starts an empty one
        env = {"GIT_INDEX_FILE": tmp}
        # assume-unchanged entries would hide real edits from `add -A`; clear the flag in the copy only.
        # skip-worktree stays: sparse checkout uses it for files that are not on disk at all.
        hidden = [e[2:] for e in (git(top, "ls-files", "-v", "-z", env=env) or "").split("\0") if e[:1].islower()]
        if hidden and git(top, "update-index", "--no-assume-unchanged", "-z", "--stdin", env=env,
                          input="\0".join(hidden)) is None:
            return None
        if git(top, "add", "-A", env=env, timeout=600) is None:
            return None
        tree = git(top, "write-tree", env=env)
        return tree.strip() if tree else None
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def mark_start(task, cwd):
    """Before the worker starts: snapshot the working tree, so changes.diff later holds only what this worker did,
    not edits that were already uncommitted (the user's, a dependency's, an earlier round's)."""
    top = git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        append_meta(task, changes="unavailable: cwd is not in a git repo")
        return
    top = top.strip()
    base = snapshot(top)
    if base is None:
        append_meta(task, changes="unavailable: git could not snapshot the working tree")
        return
    head = (git(top, "rev-parse", "--verify", "-q", "HEAD") or "").strip()
    branch = (git(top, "symbolic-ref", "-q", "--short", "HEAD") or "").strip()
    append_meta(task, base=base, base_head=head, branch=branch, repo=top)


def overlapping(task, top):
    """Other tasks that worked in the same repo while this one ran: their edits land in this task's changes.diff too,
    because the snapshot covers the whole repo."""
    me, runs = read_meta(task), os.path.dirname(os.path.dirname(os.path.abspath(task)))
    start, end = me.get("started", ""), now()
    out = []
    for other in sorted(glob.glob(os.path.join(runs, "*", "*", "meta"))):
        d = os.path.dirname(other)
        if os.path.samefile(d, task):
            continue
        m = read_meta(d)
        where = m.get("repo") or os.path.realpath(m.get("cwd", "/nonexistent"))
        same = where == top or where.startswith(top.rstrip("/") + "/")
        if same and m.get("started") and m["started"] <= end and m.get("finished", end) >= start:
            out.append(f"{os.path.basename(os.path.dirname(d))}/{os.path.basename(d)}")
    return out


# Paths that hold tests: a change to one can make the done check pass without the work being right.
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|specs?|Tests)/|[._-](test|spec)s?\.[^/]+$|(^|/)test_[^/]+$")
# A check run through a tool also depends on that tool's recipe file.
CHECK_FILES = {"make": "Makefile", "npm": "package.json", "pnpm": "package.json", "yarn": "package.json",
               "bun": "package.json", "cargo": "Cargo.toml", "swift": "Package.swift"}


def tests_touched(names, check, top=""):
    """Changed files that are tests, or that the --check command names or runs through."""
    try:
        words = shlex.split(check or "")
    except ValueError:
        words = (check or "").split()
    named = {os.path.relpath(w, top) if top and os.path.isabs(w) else w.removeprefix("./")
             for w in words if "/" in w or "." in w} | {CHECK_FILES[w] for w in words if w in CHECK_FILES}
    return [n for n in names if TEST_PATH.search(n) or any(n == w or n.endswith("/" + w) for w in named)]


def write_changes(task, check=None):
    """After the worker exits: changes.diff, everything it changed since the start snapshot (commits, uncommitted
    edits and new files, paths from the repo root), so the reviewer reads the real code, not the worker's account."""
    m = read_meta(task)
    base, top = m.get("base"), m.get("repo")
    if not base or not top:
        return
    end = snapshot(top)
    numstat = end and git(top, "diff", "--no-color", "--numstat", base, end)
    if numstat is None:
        append_meta(task, changes="unavailable: git could not diff the working tree")
        return
    # Streamed to the file and stopped at the cap, so a huge diff never sits in memory whole.
    path, cut = os.path.join(task, "changes.diff"), False
    sha = hashlib.sha256()
    proc = subprocess.Popen(["git", "-C", top, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base, end],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    with open(path, "wb") as out:
        size = 0
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            if size + len(chunk) > CHANGES_MAX:
                chunk, cut = chunk[:CHANGES_MAX - size], True
            out.write(chunk)
            sha.update(chunk)
            size += len(chunk)
            if cut:
                proc.kill()
                break
        proc.stdout.close()
        failed = proc.wait() != 0 and not cut
    if failed:
        os.unlink(path)
        append_meta(task, changes="unavailable: git diff failed")
        return
    with open(path, "ab") as out:
        if cut:
            note = (f"\n… cut at {CHANGES_MAX // 1048576} MB. Full diff: "
                    f"git -C {shlex.quote(top)} diff {base} {end}\n").encode()
            out.write(note)
            sha.update(note)
    rows = [line.split("\t") for line in numstat.splitlines() if line]
    plus = sum(int(r[0]) for r in rows if r[0].isdigit())
    minus = sum(int(r[1]) for r in rows if len(r) > 1 and r[1].isdigit())
    head = (git(top, "rev-parse", "--verify", "-q", "HEAD") or "").strip()
    branch = (git(top, "symbolic-ref", "-q", "--short", "HEAD") or "").strip()
    # After a rebase this range also holds the upstream commits the worker rebased onto.
    commits = (git(top, "rev-list", "--max-count=100", f"{m['base_head']}..{head}") or "").split() \
        if m.get("base_head") and head else []
    extra = {"branch": branch, "branch_start": m.get("branch", "")} if branch != m.get("branch", "") else {}
    # The snapshot holds a submodule as its commit; edits not committed inside it are not in the diff.
    gitlinks = [line.split("\t", 1)[1] for line in (git(top, "ls-files", "-s") or "").splitlines() if line.startswith("160000 ")]
    dirty = [p for p in gitlinks if (git(os.path.join(top, p), "status", "--porcelain") or "").strip()]
    if dirty:
        extra["changes_missing"] = "uncommitted edits inside submodules " + ",".join(dirty[:10])
    names = (git(top, "diff", "--name-only", "--no-renames", "-z", base, end) or "").split("\0")
    touched = tests_touched([n for n in names if n], check, top)
    if touched:
        extra["tests_touched"] = ",".join(touched[:20]) + (f",+{len(touched) - 20} more" if len(touched) > 20 else "")
    shared = overlapping(task, top)
    if shared:
        extra["shared_with"] = ",".join(shared[:10]) + (f",+{len(shared) - 10} more" if len(shared) > 10 else "")
    append_meta(task, changes=f"{len(rows)} files +{plus} -{minus}" + (" (cut)" if cut else ""),
                changes_sha256=sha.hexdigest(), head=head, commits=" ".join(commits), **extra)


def run_check(task, cwd, check, env, runs):
    """The done check, run by the runner itself after the worker: its exit code is recorded, not claimed. One check
    at a time on this machine (a lock in runsDir), so tasks that finish together do not pile their test runs up."""
    os.makedirs(runs, exist_ok=True)
    with open(os.path.join(task, "check.txt"), "w") as out, open(os.path.join(runs, ".check.lock"), "a") as lock:
        out.write(f"$ {check}\n")
        out.flush()
        append_meta(task, check_waiting=now())
        fcntl.flock(lock, fcntl.LOCK_EX)  # released when the file closes, also if the runner dies
        append_meta(task, check_started=now())
        rc = supervise(task, ["/bin/sh", "-c", check], stdout=out, stderr=subprocess.STDOUT, cwd=cwd, env=env)
    append_meta(task, check="pass" if rc == 0 else f"fail (signal {-rc})" if rc < 0 else f"fail (exit {rc})")
    return rc


# The report's first line, decorated or not, as the pane reads a review's `Verdict:` line.
REPORT_STOP = re.compile(r"\W*Status\W*:\W*(BLOCKED|NEEDS_CONTEXT)\W*", re.I)


def reported_stop(task):
    """BLOCKED or NEEDS_CONTEXT when the first non-empty line of result.md says the worker could not finish, else None.
    Only that line counts, so a prompt quoted further down cannot trigger it."""
    try:
        lines = read_text(os.path.join(task, "result.md")).splitlines()
    except OSError:
        return None
    hit = REPORT_STOP.fullmatch(next((line for line in lines if line.strip()), ""))
    return hit[1].upper() if hit else None


def finish(task, rc, status):
    append_meta(task, finished=now(), exit=rc)
    with open(os.path.join(task, "status"), "w") as f:
        f.write(status + "\n")


def write_status(task, status):
    with open(os.path.join(task, "status"), "w") as f:
        f.write(status + "\n")


def descendants(pid):
    """Every process below pid, children first, from `pgrep -P`; empty when pgrep is missing."""
    found, todo = [], [pid]
    while todo:
        try:
            out = subprocess.run(["pgrep", "-P", str(todo.pop())], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            break
        kids = [int(k) for k in out.split()]
        found += kids
        todo += kids
    return found


def on_stop(task):
    """If the runner is told to stop, queued or running, stop the worker and still record a final status:
    `canceled: …` when `muster.py cancel` asked for it, `failed: interrupted` otherwise."""
    def stop(signum, _frame):
        if _child is not None:
            # Stop the whole tree the worker or check started (a test runner, a `sleep &`), not just the shell on top.
            tree = [_child.pid] + descendants(_child.pid)
            for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
                for pid in tree:
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        pass
                try:
                    _child.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
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


def stalled_log(task, m, stall_min):
    """(log path, mtime) when this running task's worker has been silent for stall_min minutes and that silence has
    not been reported yet. After the worker exits (saving results, done check) the log is meant to be quiet."""
    if "worker_exited" in m or "started" not in m:
        return None
    log = os.path.join(task, "stream.jsonl" if m.get("type") == "claude" else "log.txt")
    try:
        mtime = os.path.getmtime(log)
    except OSError:
        return None  # the worker has not started yet
    started = datetime.datetime.strptime(m["started"], "%Y-%m-%d %H:%M:%S").timestamp()
    if mtime < started or time.time() - mtime < stall_min * 60 or m.get("stall_reported") == str(mtime):
        return None  # an older round's log counts as not started
    return log, mtime


def watch(run_dir, stall_min=10):
    """Wait for something the main agent must act on: a silent worker (one block per stall, each reported once through
    meta's `stall_reported`) or the end of the run. Reports only: it never stops or changes a task."""
    if not os.path.isdir(run_dir):
        sys.exit(f"muster: no run folder {run_dir}")
    grace = time.time() + WATCH_GRACE_S
    while True:
        tasks, waiting, stalls = [], False, []
        for task in sorted(d for d in glob.glob(os.path.join(run_dir, "*")) if os.path.isdir(d)):
            m = read_meta(task)
            try:
                status = read_text(os.path.join(task, "status")).strip()
            except OSError:
                status = ""
            if live(m, status):
                waiting = True
                hit = stalled_log(task, m, stall_min) if status == "running" else None
                if hit:
                    stalls.append((task, m, *hit))
            elif not status and (time.time() < grace or pid_alive(int(m.get("pid", "0") or 0))):
                waiting = True  # launched a moment ago, or caught while the runner rewrites `status`
            tasks.append((task, "failed: lost" if status.startswith(("queued", "running")) and not live(m, status) else status))
        for task, m, log, mtime in stalls:
            silent = int(time.time() - mtime)
            print(f"stalled: {os.path.basename(task)} on {m.get('worker', '?')}, silent for {silent // 60}m{silent % 60:02d}s")
            print(f"log: {os.path.abspath(log)}")
            for line in [ln for ln in tail_lines(log) if ln.strip()][-5:]:
                print(f"  {short(line)}")
            sys.stdout.flush()
            append_meta(task, stall_reported=mtime)
        if stalls:
            return 0
        if not waiting:
            print("all tasks finished")
            for task, status in tasks:
                print(f"{os.path.basename(task)}: {short(status) or 'not started'}")
            return 0
        time.sleep(QUEUE_POLL_S)


def run(cfg, names, task, cwd, after=(), priority=0, check=None):
    # `names`: one worker or several ("a,b"); the task waits for whichever frees a slot first.
    names = [n for n in (names.split(",") if isinstance(names, str) else names) if n]
    for name in names:
        if name not in cfg["workers"]:
            sys.exit(f"muster: unknown worker {name!r} (known: {', '.join(cfg['workers'])})")
    prompt = os.path.join(task, "prompt.md")
    if not os.path.isfile(prompt) or not os.path.isdir(cwd):
        sys.exit(f"muster: need {prompt} and an existing cwd {cwd}")
    tid = os.path.basename(os.path.abspath(task))
    if not TASK_ID.match(tid) or len(tid) > 63:
        # Warn, not exit: only the naming convention is wrong, and callers outside the skill may pass old-style ids.
        print(f"muster: task id {tid!r} should be a kebab-case slug "
              f"(^[a-z][a-z0-9]*(-[a-z0-9]+)*$, max 63 chars); see SKILL.md", file=sys.stderr)
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

    try:
        mark_start(task, cwd)
    except Exception as e:  # a missing diff must not cost the task; meta says why
        append_meta(task, changes=f"unavailable: {short(str(e), 80)}")
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
    append_meta(task, worker_exited=now())
    try:
        write_changes(task, check)
    except Exception as e:  # best effort, like usage; meta says why there is no diff
        append_meta(task, changes=f"unavailable: {short(str(e), 80)}")
    record_model(task, kind)
    try:  # before the check, so a check that is interrupted cannot cost the worker's usage and result
        write_usage(task, cfg)
    except Exception as e:  # usage is best effort; the task result matters more
        with open(os.path.join(task, "log.txt"), "a") as log:
            log.write(f"\nmuster: usage not recorded: {e}\n")
    stopped = reported_stop(task) if rc == 0 else None
    if rc != 0 and check:
        append_meta(task, check="skipped: the worker failed")
    if stopped:
        reason = f"worker reported {stopped}"
        if check:
            append_meta(task, check=f"skipped: the worker reported {stopped}")
        append_meta(task, last_error=reason, log_path=os.path.abspath(os.path.join(task, "result.md")))
        finish(task, 1, f"failed: {reason}")
        return 1
    if rc == 0 and check:
        crc = run_check(task, cwd, check, env, expand(cfg.get("runsDir", "~/.muster/runs")))
        if crc != 0:
            line, _ = last_error([os.path.join(task, "check.txt")])
            reason = f"done check failed (exit {crc})" + (f": {short(line, 100)}" if line else "")
            append_meta(task, last_error=reason, log_path=os.path.abspath(os.path.join(task, "check.txt")))
            finish(task, crc, f"failed: {reason}")
            return crc
    if rc == 0:
        finish(task, 0, "done")
        return 0
    main_log = os.path.join(task, "stream.jsonl" if kind == "claude" else "log.txt")
    logs = [main_log] + ([os.path.join(task, "log.txt")] if kind == "claude" else [])
    line, where = last_error(logs)
    reason = short(line) if line else f"exit {rc}"
    status = f"failed: {reason}"
    if line and ENOSPC.search(line):
        status = f"failed: disk full (ENOSPC), {free_gb(cwd):.1f}G free on {mount_of(cwd)}"
        with open(os.path.join(task, "result.md"), "a") as f:
            f.write(f"\n\nDisk full (ENOSPC). To resume: free space, then rerun this task dir; the worker starts over "
                    f"from prompt.md in the same cwd, so add what is already done to prompt.md first:\n\n"
                    "    " + shlex.join(["python3", os.path.abspath(__file__), "run", ",".join(names), os.path.abspath(task), cwd]) + "\n")
    append_meta(task, last_error=reason, log_path=os.path.abspath(where or main_log))
    finish(task, rc, status)
    return rc


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "detect":  # works before any config exists
        print(json.dumps(detect(), indent=2))
        return
    if cmd == "watch" and (len(args) == 1 or (len(args) == 3 and args[1] == "--stall-min")):  # reads only the run folder
        sys.exit(watch(args[0], float(args[2]) if len(args) == 3 else 10))
    cfg = load_config()
    if cmd == "doctor":
        probs = doctor(cfg)
        print("\n".join(probs) if probs else f"ok: {len(cfg['workers'])} workers, config {CONFIG}")
        sys.exit(1 if probs else 0)
    if cmd == "run" and len(args) >= 3:
        opts = dict(zip(args[3::2], args[4::2]))
        if len(args) % 2 == 0 or set(opts) - {"--after", "--priority", "--check"}:
            sys.exit(__doc__)
        after = [a for a in opts.get("--after", "").split(",") if a]
        sys.exit(run(cfg, *args[:3], after=after, priority=int(opts.get("--priority", "0")),
                     check=opts.get("--check") or None))
    if cmd == "cancel" and len(args) in (1, 2):
        sys.exit(cancel(args[0], args[1] if len(args) == 2 else ""))
    if cmd == "usage" and len(args) == 1:
        print(json.dumps(write_usage(args[0], cfg)))
        return
    if cmd == "clean" and len(args) in (0, 2) and (not args or args[0] == "--days"):
        print(clean(cfg, int(args[1]) if args else 14))
        return
    if cmd == "workers":
        for name, w in cfg["workers"].items():
            print(f"{name}\t{w['type']}\t{w.get('codexHome') or w.get('command', '')}")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
