#!/usr/bin/env python3
"""Muster worker runner: run one task on one configured worker and record what happened.

    muster.py run <worker[,worker…]> <task-dir> <cwd> [--after A,B] [--priority N] [--check CMD] [--retry-of TASK-DIR]
                                              [--tier low|standard|high]
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
    muster.py stats [--days N] [--since D] [--until D] [--by FIELDS | --json]
                                              group work by repo, type, tier, worker or model; filter by run
                                              folder age and first attempt's start date or time. Use --json
                                              for the stats screen's view model. Reads only.
    muster.py workers                         list configured workers and their Codex quota
    muster.py detect                          JSON: Codex homes and claude-like CLIs found on this machine
    muster.py doctor                          check the config; exit 1 when something is wrong

Config: ~/.muster/config.json (MUSTER_CONFIG overrides). Prices: prices.json at the repo
root, overridden key by key by ~/.muster/prices.json. Standard library only.
"""
import datetime
import fcntl
import fnmatch
import functools
import glob
import hashlib
import json
import os
import re
import shutil
import shlex
import signal
import statistics
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
TIERS = ("low", "standard", "high")
# Failure kinds that say nothing about the task: rerun at the same tier, and do not count the attempt as a round.
NOT_TASK = ("provider", "infra", "interrupted")


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


def tier_config(cfg, name):
    """The settings for `--tier name`; exits when the name is unknown or the config has no such tier."""
    if name not in TIERS:
        sys.exit(f"muster: --tier {name}: expected one of {', '.join(TIERS)}")
    tiers = cfg.get("tiers") or {}
    if name not in tiers:
        sys.exit(f"muster: --tier {name}: no tiers.{name} in {CONFIG}")
    if not isinstance(tiers[name], dict):
        sys.exit(f"muster: --tier {name}: tiers.{name} is not an object; run doctor")
    return tiers[name]


def tier_args(tier, name, kind):
    """A tier's extra arguments for this worker: the worker's own key wins over its type's, as caps do."""
    args = (tier or {}).get("args") or {}
    return args.get(name, args.get(kind, []))


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


def codex_rollouts(home, session_id=""):
    return glob.iglob(os.path.join(home, "sessions", "**", f"rollout-*{session_id}.jsonl"), recursive=True)


@functools.lru_cache(maxsize=64)
def rollout_quota(path, mtime_ns, size):
    limit = 512 * 1024
    with open(path, "rb") as f:
        f.seek(max(0, size - limit))
        lines = f.read(limit).splitlines()
    for line in reversed(lines):
        if b'"rate_limits"' not in line:
            continue
        try:
            pending = [json.loads(line)]
        except (ValueError, RecursionError):
            continue
        while pending:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node)
            if not isinstance(node, dict):
                continue
            limits = node.get("rate_limits")
            if not isinstance(limits, dict):
                pending.extend(node.values())
                continue
            windows = [w for w in (limits.get("primary"), limits.get("secondary"))
                       if isinstance(w, dict) and type(w.get("used_percent")) in (int, float)
                       and 0 <= w["used_percent"] <= sys.float_info.max
                       and type(w.get("resets_at")) in (float, int)]
            if windows:
                window = max(windows, key=lambda w: (w["used_percent"], w["resets_at"]))
                try:
                    datetime.datetime.fromtimestamp(window["resets_at"])
                except (ValueError, OverflowError, OSError):
                    continue
                used = window["used_percent"]
                return dict(used_percent=used, resets_at=window["resets_at"],
                            reached=limits.get("rate_limit_reached_type") is not None or used >= 100,
                            plan=limits.get("plan_type"), observed=mtime_ns / 1_000_000_000)
    return None


def codex_quota(home):
    newest = None
    for path in codex_rollouts(home):
        try:
            stat = os.stat(path)
        except OSError:
            continue
        if newest is None or stat.st_mtime_ns > newest[1]:
            newest = (path, stat.st_mtime_ns, stat.st_size)
    try:
        snapshot = rollout_quota(*newest) if newest else None
    except OSError:
        return None
    return snapshot if snapshot and snapshot["resets_at"] > time.time() else None


def usage_codex(task, home, prices, billing):
    log = read_text(os.path.join(task, "log.txt"))
    m = re.search(r"^session id: (\S+)", log, re.M)
    path = next(codex_rollouts(home, m.group(1)), None) if m else None
    if not path:
        return None
    model, prev, p, usd = "?", None, None, 0.0
    tot = dict(input=0, cached=0, output=0, reasoning=0)
    for line in read_text(path).splitlines():
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


def usage_agy(task, prices, billing, model):
    # Reads `agy --output-format stream-json`: the final {"event": "result"} line. agy's input_tokens leave out cache reads.
    d = None
    for line in read_text(os.path.join(task, "stream.jsonl")).splitlines():
        try:
            o = json.loads(line) if line.startswith("{") else {}
        except ValueError:
            continue
        if o.get("event") == "result" and isinstance(o.get("result"), dict):
            d = o["result"]
    if d is None:
        return None
    with open(os.path.join(task, "result.md"), "w") as f:
        f.write(d.get("response") or "")
    u = d.get("usage") or {}
    cached = u.get("cache_read_tokens", 0)
    tot = dict(input=u.get("input_tokens", 0) + cached, cached=cached, output=u.get("output_tokens", 0),
               reasoning=u.get("thinking_tokens", 0))
    p = price_for(prices, model)
    return dict(model=model or "?", **tot, usd=round(cost(p, tot["input"], cached, tot["output"]), 4) if p else None,
                billing=billing, peak=False, is_error=d.get("status") != "SUCCESS")


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
    elif meta.get("type") == "agy":
        u = usage_agy(task, prices, worker.get("billing", "subscription"), meta.get("model", ""))
    else:
        u = usage_claude(task, prices, worker.get("billing", "metered"), started)
    if u:
        with open(os.path.join(task, "usage"), "w") as f:
            json.dump(u, f)
    return u


# ---------- guards ----------

DEFAULT_CAPS = {"codex": 3, "claude": 5, "agy": 3}
TYPES = ("codex", "claude", "agy")
STREAM_TYPES = ("claude", "agy")  # workers that write stream.jsonl


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


ENGLISH_MONTHS = {name: number for number, month in enumerate(
    "january february march april may june july august september october november december".split(), 1)
    for name in (month, month[:3])}


def quota_retry_at(line, now):
    match = re.search(r"\btry again at\s+(?:([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,\s+(\d{4})\s+)?"
                      r"(\d{1,2}):(\d{2})\s+([AP]M)\b", line, re.I)
    if not match:
        return None
    month, day, year, hour, minute, period = match.groups()
    if not 1 <= int(hour) <= 12:
        return None
    try:
        local_time = datetime.time(int(hour) % 12 + (12 if period.upper() == "PM" else 0), int(minute))
        if month:
            number = ENGLISH_MONTHS.get(month.lower())
            if number is None:
                return None
            date = datetime.date(int(year), number, int(day))
            return datetime.datetime.combine(date, local_time).timestamp()
        retry = datetime.datetime.combine(now.date(), local_time)
        if retry <= now:
            retry += datetime.timedelta(days=1)
        return retry.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def quota_records(runs):
    records = load_json(os.path.join(runs, ".quota.json"), {})
    if not isinstance(records, dict):
        return {}
    current = time.time()
    return {name: row for name, row in records.items() if isinstance(row, dict)
            and isinstance(row.get("until"), (int, float)) and row["until"] > current}


def record_quota(runs, name, worker, line):
    current = datetime.datetime.now()
    until = quota_retry_at(line, current)
    if until is None or until <= current.timestamp():
        snapshot, _ = worker_quota(worker, {})
        # A rate limit (429, "too many requests", a per-minute quota) passes in moments; a spent quota does not.
        brief = not EXHAUSTED.search(line) or re.search(r"per.?minute|per.?second", line, re.I)
        until = snapshot["resets_at"] if snapshot else current.timestamp() + (RATE_SKIP_S if brief else QUOTA_SKIP_S)
    with open(os.path.join(runs, ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = quota_records(runs)
        if until > time.time():
            records[name] = {"until": until, "reason": short(line)}
        path = os.path.join(runs, ".quota.json")
        with open(path + ".tmp", "w") as f:
            json.dump(records, f)
        os.replace(path + ".tmp", path)


def worker_quota(worker, record):
    """(Codex usage snapshot or None, epoch until which the worker is out of quota or 0). Any worker type can carry a
    record from a failed run; only Codex also has a live snapshot to read."""
    snapshot = codex_quota(expand(worker.get("codexHome", "~/.codex"))) if worker["type"] == "codex" else None
    until = record.get("until", 0)
    if snapshot and snapshot["reached"]:
        until = max(until, snapshot["resets_at"])
    return snapshot, until if until > time.time() else 0


def mount_of(path):
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        path = os.path.dirname(path)
    return path


def free_gb(path):
    return shutil.disk_usage(path).free / 1e9


ERROR_LINE = re.compile(r"ENOSPC|no space left on device|error|panic|traceback|quota|usage limit|rate.?limit", re.I)
# A failure of the provider, not of the task: rerunning on another worker at the same tier is the right response.
PROVIDER = re.compile(r"usage limit|quota|rate.?limit|too many requests|"
                      r"\b(?:HTTP|status|code)\W{0,3}(?:429|401|403|529)\b|"
                      r"\b(?:429|401|403|529)\s+(?:too many|unauthori|forbidden|overloaded)|"
                      r"overloaded|billing|credits|access token|unauthorized|authentication|codex_login|"
                      r"resource.?exhausted", re.I)
# Of those, the ones that say the account is out for a while, whatever the worker type: skip it until it is back.
QUOTA = re.compile(r"usage limit|quota|rate.?limit|too many requests|\b429\b|resource.?exhausted", re.I)
EXHAUSTED = re.compile(r"usage limit|quota", re.I)  # out for hours; the rest of QUOTA is a rate limit
QUOTA_SKIP_S, RATE_SKIP_S = 30 * 60, 2 * 60  # how long to skip a worker whose error names no time
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


def last_error(paths, echoed=()):
    """(line, path): the last line that looks like an error in the tails of `paths`, first path first;
    an ENOSPC line anywhere wins. stream-json lines count through their error text only. Lines in `echoed`
    (the prompt, which codex copies into its log) are skipped: a brief that mentions an error is not one."""
    echoed = {e.strip() for e in echoed if e.strip()}
    hits = []
    for path in paths:
        for line in tail_lines(path):
            line = line.strip()
            flagged = False  # a stream-json error result is an error whatever its words ("usage limit reached")
            if line.startswith("{"):
                try:
                    o = json.loads(line)
                except ValueError:
                    o = {}
                flagged = isinstance(o, dict) and bool(o.get("is_error") or o.get("error"))
                line = str(o.get("result") or o.get("error") or "") if flagged else ""
            if line and (flagged or ERROR_LINE.search(line)) and line not in echoed:
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
            if name in TYPES:
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


KEYS = {"": {"runsDir", "workers", "routing", "only", "caps", "tiers", "preamble"},
        "worker": {"type", "command", "args", "codexHome", "billing", "role"},
        "route": {"match", "workers", "preamble", "tiers"}}


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
        if "role" in w and not (isinstance(w["role"], str) and w["role"].strip()):
            probs.append(f"workers.{name}.role: expected a sentence")
        for key in ("command", "codexHome"):
            if key in w and not isinstance(w[key], str):
                probs.append(f"workers.{name}.{key}: expected a path string")
                w = {k: v for k, v in w.items() if k != key}
        kind = w.get("type")
        if kind not in TYPES:
            probs.append(f"workers.{name}.type: {kind!r}, expected one of {', '.join(TYPES)}")
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
        # A route may only restate when each tier fits these repos; workers and arguments stay global.
        rt = r.get("tiers")
        if rt is not None and not isinstance(rt, dict):
            probs.append(f"routing[{i}] ({r.get('match')}).tiers: expected an object of tier -> {{ when }}")
        for name, t in (rt if isinstance(rt, dict) else {}).items():
            where = f"routing[{i}] ({r.get('match')}).tiers.{name}"
            if name.startswith("_"):
                continue
            if name not in TIERS:
                probs.append(f"{where}: unknown tier (expected one of {', '.join(TIERS)})")
            elif not isinstance(t, dict) or set(k for k in t if not k.startswith("_")) - {"when"}:
                probs.append(f"{where}: expected {{ when }}")
            elif not isinstance(t.get("when", ""), str):
                probs.append(f"{where}.when: expected a string")
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
        if k not in workers and k not in TYPES:
            probs.append(f"caps.{k}: not a worker name or type")
        elif not isinstance(v, int) or isinstance(v, bool) or v < 1:
            probs.append(f"caps.{k}: {v!r}, expected a whole number of at least 1")
    tiers = cfg.get("tiers") or {}
    if not isinstance(tiers, dict):
        probs.append("tiers: expected an object of tier -> settings")
        tiers = {}
    for name, t in tiers.items():
        if name.startswith("_"):
            continue
        if name not in TIERS:
            probs.append(f"tiers.{name}: unknown tier (expected one of {', '.join(TIERS)})")
            continue
        if not isinstance(t, dict):
            probs.append(f"tiers.{name}: expected an object")
            continue
        for k in t:
            if k not in ("prefer", "args", "when") and not k.startswith("_"):
                probs.append(f"tiers.{name}.{k}: unknown key (expected args, prefer, when)")
        if not isinstance(t.get("when", ""), str):
            probs.append(f"tiers.{name}.when: expected a string")
        prefer = t.get("prefer")
        if prefer is not None and not (isinstance(prefer, list)
                                       and all(isinstance(w, str) and w in workers for w in prefer)):
            probs.append(f"tiers.{name}.prefer: expected a list of known worker names")
        args = t.get("args")
        if args is not None and not isinstance(args, dict):
            probs.append(f"tiers.{name}.args: expected an object of worker or type -> arguments")
        elif args:
            for k, v in args.items():
                if k not in workers and k not in TYPES:
                    probs.append(f"tiers.{name}.args.{k}: not a worker name or type")
                if not (isinstance(v, list) and strings(v)):
                    probs.append(f"tiers.{name}.args.{k}: expected a list of strings")
            # Codex refuses `-m` twice, so a tier's model flag breaks a Codex worker that already pins one.
            for wname, w in workers.items():
                if not isinstance(w, dict) or w.get("type") != "codex":
                    continue
                extra = args.get(wname, args.get(w.get("type"), []))
                if isinstance(extra, list) and isinstance(w.get("args"), list) and \
                        {"-m", "--model"} & set(extra) and {"-m", "--model"} & set(w["args"]):
                    probs.append(f"tiers.{name}.args: adds -m to worker {wname}, which already sets it; "
                                 f"Codex refuses it twice (use -c model=\"...\" or drop one)")
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


# ---------- stats ----------

VERDICT = re.compile(r"^\W*Verdict\W*:\W*(.*?)\W*$", re.I | re.M)
DEFAULT_STATS_FIELDS = ("tier", "worker", "model")


def stats_bound(value, *, until=False):
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}( [0-9]{2}:[0-9]{2})?", value):
        raise ValueError(value)
    bound = datetime.datetime.strptime(value, "%Y-%m-%d" if len(value) == 10 else "%Y-%m-%d %H:%M")
    if until and len(value) == 10:  # started is recorded to the second; include the entire day.
        return datetime.datetime.combine(bound.date(), datetime.time.max)
    return bound


def main_checkout(path):
    common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        return os.path.dirname(os.path.normpath(common.rstrip("\n")))


def resolve_stats_repos(attempts):
    names = {}
    resolved = set()
    missing = []
    for a in attempts:
        meta = a["meta"]
        path = meta.get("repo") or meta.get("cwd")
        if path:
            if meta.get("main_repo"):
                name = os.path.basename(os.path.normpath(meta["main_repo"]))
                a["repo_name"] = names[path] = name
                resolved.add(name)
            else:
                missing.append((a, path))

    for _, path in missing:
        if path in names:
            continue
        if not os.path.isdir(path):
            names[path] = None
            continue
        checkout = main_checkout(path)
        if checkout:
            names[path] = os.path.basename(checkout)
            resolved.add(names[path])
        else:
            names[path] = os.path.basename(os.path.normpath(path))
    for a, path in missing:
        if names[path] is None:
            base = os.path.basename(os.path.normpath(path))
            names[path] = max((n for n in resolved if base.startswith(n + "-")), key=len, default=base)
        a["repo_name"] = names[path]


def stats_billing(kinds):
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed" if kinds else "-"


def stats_records(cfg, days=None, since=None, until=None, *, resolve_repos=True):
    """Read one record per piece of work and count pieces that predate reviews.
    Attempts of one piece of work are joined as the pane joins them (`retry_of`, then `x`/`x-r2` naming)."""
    runs = expand(cfg.get("runsDir", "~/.muster/runs"))
    today = datetime.date.today()

    def text(path):
        try:
            return read_text(path)
        except OSError:
            return ""

    def when(value):
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None

    def number(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    attempts = {}
    for run in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        d = os.path.join(runs, run)
        if not os.path.isdir(d):
            continue
        if days is not None:
            hit = re.match(r"(\d{8})", run)
            try:
                date = datetime.datetime.strptime(hit[1], "%Y%m%d").date() if hit else None
            except ValueError:
                date = None
            if date is None or (today - date).days >= days:
                continue
        for tid in sorted(os.listdir(d)):
            t = os.path.join(d, tid)
            if not os.path.isdir(t):
                continue
            meta = read_meta(t)
            status = text(os.path.join(t, "status")).strip()
            usage = load_json(os.path.join(t, "usage"), None)
            key = f"{run}/{tid}"
            attempts[key] = {
                "key": key, "id": tid, "run": run, "meta": meta, "status": status,
                "usage": usage if isinstance(usage, dict) else None,
                "review": text(os.path.join(t, "review.md")),
                "started": when(meta.get("started")), "finished": when(meta.get("finished")),
                # A canceled attempt or one that never started is not part of the record.
                "counted": not status.startswith("canceled") and bool(meta.get("started")),
            }

    def trace(a, seen):
        parent = attempts.get(a["meta"].get("retry_of", ""))
        if parent and parent["key"] not in seen:  # `seen` stops a hand-edited retry_of loop
            work, rnd = trace(parent, seen | {parent["key"]})
            return work, rnd + 1
        hit = re.match(r"^(.*)-r(\d+)$", a["id"])
        return f"{a['run']}/{hit[1] if hit else a['id']}", int(hit[2]) if hit else 1

    works = {}
    for a in attempts.values():
        work, a["round"] = trace(a, {a["key"]})
        works.setdefault(work, []).append(a)

    def last_verdict(a):
        verdicts = VERDICT.findall(a["review"])
        return (verdicts[-1] if verdicts else "").lower()

    def accepted(a):
        # The pane's rules: the last Verdict line counts, and only when review.md names the current diff.
        if a["status"] != "done" or "changes" not in a["meta"]:
            return False
        verdict = last_verdict(a)
        m = a["meta"]
        covers = not m.get("changes_sha256") or m["changes_sha256"] in a["review"]
        fixes = verdict == "needs fixes" and covers
        return (verdict in ("approved", "user override") and covers) or \
            (fixes and (m.get("changes") or "").startswith("0 files"))

    if resolve_repos:
        resolve_stats_repos(attempts.values())
    records = []
    legacy = 0
    for items in works.values():
        counted = [a for a in items if a["counted"]]
        if not counted:
            continue
        first = min(counted, key=lambda a: (a["started"] or datetime.datetime.min, a["round"]))
        started = first["started"]
        if since and (started is None or started < since):
            continue
        if until and (started is None or started >= until):
            continue
        newest = max(counted, key=lambda a: (a["round"], a["started"] or datetime.datetime.min))
        if newest["status"] == "done" and "changes" not in newest["meta"]:
            legacy += 1
            continue
        meta = first["meta"]
        fields = {field: meta.get(field) for field in ("tier", "worker", "type", "model")}
        fields["repo"] = first.get("repo_name")
        if not fields["model"]:
            fields["model"] = (first["usage"] or {}).get("model")
        if not fields["type"]:
            workers = cfg.get("workers")
            worker = workers.get(meta.get("worker")) if isinstance(workers, dict) else None
            fields["type"] = worker.get("type") if isinstance(worker, dict) else None
        record = {k: v if isinstance(v, str) and v else "-" for k, v in fields.items()}
        rounds = sum(a["meta"].get("failure") not in NOT_TASK for a in counted)
        ok = accepted(newest)
        # An outside failure leaves the work open until its rerun.
        settled = ok or (newest["status"].startswith("failed:") and newest["meta"].get("failure") not in NOT_TASK)
        claude_fixed = settled and any(re.search(r"^Fixed by:[^\S\n]*Claude", a["review"], re.I | re.M)
                                       for a in counted)
        sent_back = settled and (bool(re.search(r"^Fixed in:", newest["review"], re.M)) or any(
            re.match(r"[1-9]\d* files?\b", a["meta"].get("changes", "")) and last_verdict(a) == "needs fixes"
            for a in counted))

        costs, minutes, billing = [], [], set()
        tokens_in = tokens_out = lines_added = lines_removed = 0
        for a in counted:
            u = a["usage"] or {}
            tokens_in += number(u.get("input"))
            tokens_out += number(u.get("output"))
            if isinstance(u.get("usd"), (int, float)) and not isinstance(u.get("usd"), bool):
                costs.append(u["usd"])
            if u.get("billing") in ("metered", "subscription"):
                billing.add(u["billing"])
            if a["started"] and a["finished"]:
                minutes.append((a["finished"] - a["started"]).total_seconds() / 60)
            changes = re.match(r"\d+ files? \+(\d+) -(\d+)\b", a["meta"].get("changes", ""))
            if changes:
                lines_added += int(changes[1])
                lines_removed += int(changes[2])

        record.update(
            started=started, settled=settled, accepted=ok, open=not settled,
            first_pass=ok and rounds == 1 and not claude_fixed and "Fixed in:" not in newest["review"],
            attempts=rounds, outside_failures=len(counted) - rounds, needs_fixes=sent_back, claude_fixed=claude_fixed,
            blocked=settled and any(a["meta"].get("failure") == "blocked" for a in counted),
            check_failed=settled and any(a["status"].startswith("failed: done check failed") for a in counted),
            tokens_in=tokens_in, tokens_out=tokens_out, lines_added=lines_added, lines_removed=lines_removed,
            usd=sum(costs) if costs else None, attempt_costs=costs, priced_partially=0 < len(costs) < len(counted),
            minutes=sum(minutes) if minutes else None, billing=stats_billing(billing),
        )
        records.append(record)
    return records, legacy


def stats_summary(records):
    """Rates and usage describe settled work; activity and outside failures include open work."""
    settled = [r for r in records if r["settled"]]
    totals = {field: sum(r[field] for r in records) for field in (
        "settled", "accepted", "open", "first_pass", "blocked", "check_failed", "needs_fixes",
        "claude_fixed", "outside_failures", "lines_added", "lines_removed")}
    for field in ("attempts", "tokens_in", "tokens_out"):
        totals[field] = sum(r[field] for r in settled)
    priced = [r for r in settled if r["usd"] is not None]
    # Preserve the table's attempt-by-attempt rounding at half-cent boundaries.
    usd = 0.0
    for r in priced:
        for cost in r.get("attempt_costs", (r["usd"],)):
            usd += cost
    minutes = [r["minutes"] for r in settled if r["minutes"] is not None]
    totals.update(
        usd=usd if priced else None,
        priced_partially=bool(priced) and (len(priced) < len(settled) or any(r["priced_partially"] for r in priced)),
        median_minutes=statistics.median(minutes) if minutes else None,
        billing=stats_billing({r["billing"] for r in settled if r["billing"] != "-"}),
    )
    return totals


def stats_groups(records, by):
    groups = {}
    for r in records:
        key = tuple(r[field] for field in by)
        groups.setdefault(key, []).append(r)
    order = {"low": 0, "standard": 1, "high": 2, "-": 3}

    def sort_key(key):
        if "tier" in by:
            i = by.index("tier")
            return (order.get(key[i], 4), *key[:i], *key[i + 1:])
        return key

    for key in sorted(groups, key=sort_key):
        yield key, groups[key]


def stats(cfg, days=None, since=None, until=None, by=DEFAULT_STATS_FIELDS):
    records, legacy = stats_records(cfg, days, since, until, resolve_repos="repo" in by)
    header = (*by, "settled", "accepted", "open", "first_pass", "attempts_per_work",
              "blocked", "check_failed", "needs_fixes", "claude_fixed", "outside_failures",
              "median_minutes", "tokens_per_accepted", "usd_per_accepted", "billing")
    lines = ["\t".join(header)]
    small_sample = False
    for key, group in stats_groups(records, by):
        g = stats_summary(group)
        small_sample |= g["settled"] < 30
        ok = g["accepted"]
        usd = "-"
        if ok and g["usd"] is not None:
            usd = ("≥" if g["priced_partially"] else "") + f"{g['usd'] / ok:.2f}"
        lines.append("\t".join([
            *key, str(g["settled"]), str(ok), str(g["open"]),
            f"{100 * g['first_pass'] / ok:.0f}%" if ok else "-",
            f"{g['attempts'] / g['settled']:.1f}" if g["settled"] else "-",
            str(g["blocked"]), str(g["check_failed"]), str(g["needs_fixes"]), str(g["claude_fixed"]),
            str(g["outside_failures"]), f"{g['median_minutes']:.1f}" if g["median_minutes"] is not None else "-",
            str(round((g["tokens_in"] + g["tokens_out"]) / ok)) if ok else "-",
            usd, g["billing"],
        ]))
    lines.append("Subscription dollars are API-equivalent, not money paid.")
    if legacy:
        lines.append(f"{legacy} pieces of work from before reviews were recorded are left out.")
    if small_sample:
        lines.append("Rows with fewer than 30 settled pieces of work are small samples: only large differences are meaningful.")
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
    main = main_checkout(top)
    if main:
        append_meta(task, main_repo=main)


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
            append_meta(task, failure="interrupted")
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
    log = os.path.join(task, "stream.jsonl" if m.get("type") in STREAM_TYPES else "log.txt")
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


def retry_ref(task, target):
    """`<run>/<task>` of the attempt this task reruns, for the pane to count both as one piece of work."""
    old, new = os.path.realpath(target), os.path.realpath(task)
    if old == new or not os.path.isfile(os.path.join(old, "prompt.md")):
        sys.exit(f"muster: --retry-of {target}: not another task")
    if os.path.dirname(os.path.dirname(old)) != os.path.dirname(os.path.dirname(new)):
        sys.exit(f"muster: --retry-of {target}: not in the same runs folder as {task}")
    return f"{os.path.basename(os.path.dirname(old))}/{os.path.basename(old)}"


def run(cfg, names, task, cwd, after=(), priority=0, check=None, retry_of=None, tier=None):
    # `names`: one worker or several ("a,b"); the task waits for whichever frees a slot first.
    names = [n for n in (names.split(",") if isinstance(names, str) else names) if n]
    for name in names:
        if name not in cfg["workers"]:
            sys.exit(f"muster: unknown worker {name!r} (known: {', '.join(cfg['workers'])})")
    t = tier_config(cfg, tier) if tier else None  # before anything is written
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
    retried = retry_ref(task, retry_of) if retry_of else ""
    fenced = [n for n in names if not allowed_here(cfg, n, cwd)]
    names = [n for n in names if n not in fenced]
    if not names:
        limits = "; ".join(f"{n} to {cfg['only'][n]}" for n in fenced)
        return refuse(task, f"`only` limits {limits}; {cwd} is outside", 3)
    if t:
        # `prefer` moves listed workers to the front, after the fence; a stable sort adds nothing and keeps order.
        prefer = t.get("prefer") or []
        names = sorted(names, key=lambda n: n not in prefer)
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
                f"pid={os.getpid()}\nqueued={now()}\nafter={','.join(after)}\npriority={priority}\nretry_of={retried}\n"
                f"tier={tier or ''}\n")
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
            waiting, resets = [], []
            records = quota_records(runs)
            for n in names:
                _, until = worker_quota(cfg["workers"][n], records.get(n, {}))
                if until:
                    resets.append(until)
                    waiting.append(f"{n} out of quota until {datetime.datetime.fromtimestamp(until):%H:%M}")
                    continue
                if deps != "ready":
                    break
                busy, cap = running_on(cfg, n), cap_of(cfg, n)
                if busy < cap and os.path.realpath(task) in queue_head(cfg, n)[:cap - busy]:
                    name = n
                    break
                waiting.append(f"{n} runs {busy} of {cap}")
            if name is not None:
                append_meta(task, worker=name, type=cfg["workers"][name]["type"], started=now())
                write_status(task, "running")
                break
            if deps == "ready" and len(resets) == len(names):
                earliest = datetime.datetime.fromtimestamp(min(resets))
                status = f"failed: every worker is out of quota (earliest back {earliest:%Y-%m-%d %H:%M})"
                append_meta(task, failure="provider", last_error=status)
                finish(task, 1, status)
                return 1
            status = f"queued, {', '.join(waiting)}" if deps == "ready" else f"queued, waiting for {deps[6:]}"
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
    extra = [expand(a) for a in worker.get("args", [])] + [expand(a) for a in tier_args(t, name, kind)]
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
        elif kind == "agy":
            # agy reads no prompt from stdin: `-p -` is taken as the prompt itself. It names no model in its stream.
            model = next((b for a, b in reversed(list(zip(extra, extra[1:]))) if a == "--model"), "")  # the last one wins
            if model:
                append_meta(task, model=model)
            cmd = [expand(worker.get("command", "agy")), "--output-format", "stream-json",
                   "--dangerously-skip-permissions", *extra, "--print=" + read_text(prompt)]
            with open(os.path.join(task, "stream.jsonl"), "w") as out:
                rc = supervise(task, cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=log, cwd=cwd, env=env)
        else:
            sys.exit(f"muster: worker {name!r} has unknown type {kind!r} ({', '.join(TYPES)})")
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
        append_meta(task, last_error=reason, log_path=os.path.abspath(os.path.join(task, "result.md")),
                    failure="blocked")
        finish(task, 1, f"failed: {reason}")
        return 1
    if rc == 0 and check:
        crc = run_check(task, cwd, check, env, expand(cfg.get("runsDir", "~/.muster/runs")))
        if crc != 0:
            line, _ = last_error([os.path.join(task, "check.txt")])
            reason = f"done check failed (exit {crc})" + (f": {short(line, 100)}" if line else "")
            append_meta(task, last_error=reason, log_path=os.path.abspath(os.path.join(task, "check.txt")),
                        failure="task")
            finish(task, crc, f"failed: {reason}")
            return crc
    if rc == 0:
        finish(task, 0, "done")
        return 0
    main_log = os.path.join(task, "stream.jsonl" if kind in STREAM_TYPES else "log.txt")
    logs = [main_log] + ([os.path.join(task, "log.txt")] if kind in STREAM_TYPES else [])
    line, where = last_error(logs, read_text(prompt).splitlines())
    reason = short(line) if line else f"exit {rc}"
    status = f"failed: {reason}"
    if line and ENOSPC.search(line):
        status = f"failed: disk full (ENOSPC), {free_gb(cwd):.1f}G free on {mount_of(cwd)}"
        rerun = ["python3", os.path.abspath(__file__), "run", ",".join(names), os.path.abspath(task), cwd]
        with open(os.path.join(task, "result.md"), "a") as f:
            f.write(f"\n\nDisk full (ENOSPC). To resume: free space, then rerun this task dir; the worker starts over "
                    f"from prompt.md in the same cwd, so add what is already done to prompt.md first:\n\n"
                    "    " + shlex.join(rerun + (["--tier", tier] if tier else [])) + "\n")
    # A worker that exits non-zero failed itself (disk, network, server error, crash), not the task: a task
    # failure shows as a failed check, a BLOCKED report or a review.
    failure = "provider" if line and PROVIDER.search(line) else "infra"
    if failure == "provider" and QUOTA.search(line):
        try:
            record_quota(runs, name, worker, line)
        except OSError as e:
            print(f"muster: could not record quota for {name}: {e}", file=sys.stderr)
    append_meta(task, last_error=reason, log_path=os.path.abspath(where or main_log), failure=failure)
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
    if cmd == "stats":
        opts = {}
        args = iter(args)
        for opt in args:
            if opt in opts or opt not in ("--json", "--days", "--since", "--until", "--by"):
                sys.exit(__doc__)
            opts[opt] = True if opt == "--json" else next(args, None)
            if opts[opt] is None:
                sys.exit(__doc__)
        by = opts["--by"].split(",") if "--by" in opts else DEFAULT_STATS_FIELDS
        if set(by) - {"repo", "type", *DEFAULT_STATS_FIELDS} or ("--json" in opts and "--by" in opts):
            sys.exit(__doc__)
        try:
            days = int(opts["--days"]) if "--days" in opts else None
            since = stats_bound(opts.get("--since"))
            until = stats_bound(opts.get("--until"), until=True)
        except ValueError:
            sys.exit(__doc__)
        if "--json" in opts:
            import stats_view
            records, legacy = stats_records(load_config(), days, since, until)
            print(json.dumps(stats_view.view(records, legacy, datetime.date.today()), ensure_ascii=False))
        else:
            print(stats(load_config(), days, since, until, by))
        return
    cfg = load_config()
    if cmd == "doctor":
        probs = doctor(cfg)
        print("\n".join(probs) if probs else f"ok: {len(cfg['workers'])} workers, config {CONFIG}")
        sys.exit(1 if probs else 0)
    if cmd == "run" and len(args) >= 3:
        opts = dict(zip(args[3::2], args[4::2]))
        if len(args) % 2 == 0 or set(opts) - {"--after", "--priority", "--check", "--retry-of", "--tier"}:
            sys.exit(__doc__)
        after = [a for a in opts.get("--after", "").split(",") if a]
        sys.exit(run(cfg, *args[:3], after=after, priority=int(opts.get("--priority", "0")),
                     check=opts.get("--check") or None, retry_of=opts.get("--retry-of") or None,
                     tier=opts.get("--tier") or None))
    if cmd == "cancel" and len(args) in (1, 2):
        sys.exit(cancel(args[0], args[1] if len(args) == 2 else ""))
    if cmd == "usage" and len(args) == 1:
        print(json.dumps(write_usage(args[0], cfg)))
        return
    if cmd == "clean" and len(args) in (0, 2) and (not args or args[0] == "--days"):
        print(clean(cfg, int(args[1]) if args else 14))
        return
    if cmd == "workers":
        records = quota_records(expand(cfg.get("runsDir", "~/.muster/runs")))
        for name, w in cfg["workers"].items():
            snapshot, until = worker_quota(w, records.get(name, {}))
            quota = "-"
            if until:
                quota = f"OUT until {datetime.datetime.fromtimestamp(until):%m-%d %H:%M}"
            elif snapshot:
                reset = datetime.datetime.fromtimestamp(snapshot["resets_at"])
                quota = f"{snapshot['used_percent']:g}% · resets {reset:%m-%d %H:%M}"
                if snapshot["plan"]:
                    quota += f" · {snapshot['plan']}"
            print(f"{name}\t{w['type']}\t{w.get('codexHome') or w.get('command', '')}\t{quota}")
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
