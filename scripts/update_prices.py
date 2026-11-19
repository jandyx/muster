#!/usr/bin/env python3
"""Refresh prices.json from LiteLLM's community price list, the source ccusage also uses.

    scripts/update_prices.py [--dry-run] [model …]

Without models it refreshes every entry already in prices.json; named models are added or refreshed.
Prints what changed. A model LiteLLM does not price, or prices in a shape prices.json cannot hold, is
reported and left as it is. Standard library only, like the runner.
"""
import datetime
import json
import os
import re
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "prices.json")
M = 1e6


def per_million(v):
    return round(v * M, 6)


def rates(e, suffix=""):
    inp = e[f"input_cost_per_token{suffix}"]
    return {"input": per_million(inp),
            "cached": per_million(e.get(f"cache_read_input_token_cost{suffix}", inp)),
            "output": per_million(e[f"output_cost_per_token{suffix}"])}


def hours(span):
    """'04:00-06:00' -> {4, 5}; '10:00-00:00' runs to midnight, '00:00-00:00' is the whole day."""
    hit = re.fullmatch(r"(\d\d):00-(\d\d):00", span)
    if not hit:
        raise ValueError(f"off-peak window {span!r} is not on whole hours")
    a, b = int(hit[1]), int(hit[2]) or 24
    return set(range(a, b))


def peak_ranges(windows):
    """LiteLLM lists off-peak windows (ISO weekdays, 1 = Monday); prices.json lists peak hours on weekdays and
    treats weekends as off-peak. Returns [[start, end], …] or raises when the windows do not fit that shape."""
    off = {d: set() for d in range(1, 8)}
    for w in windows:
        spans = w["hours_utc"] if isinstance(w["hours_utc"], list) else [w["hours_utc"]]
        for d in w["weekdays"]:
            for s in spans:
                off[d] |= hours(s)
    peaks = {d: set(range(24)) - off[d] for d in off}
    if peaks[6] or peaks[7] or any(peaks[d] != peaks[1] for d in range(2, 6)):
        raise ValueError("peak hours differ between weekdays, or fall on a weekend")
    out = []
    for h in sorted(peaks[1]):
        if out and out[-1][1] == h:
            out[-1][1] = h + 1
        else:
            out.append([h, h + 1])
    return out


def convert(e):
    """One LiteLLM entry -> one prices.json entry (USD per 1M tokens)."""
    entry = rates(e)
    long = [k for k in e if re.fullmatch(r"input_cost_per_token_above_(\d+)k_tokens", k)]
    if len(long) > 1:
        raise ValueError("more than one long-context threshold")
    if long:
        k = re.search(r"\d+", long[0])[0]
        entry["long_context_over"] = int(k) * 1000
        entry["long"] = rates(e, f"_above_{k}k_tokens")
    off = e.get("off_peak_pricing")
    if off:
        # LiteLLM's base price is the peak price; prices.json keeps the off-peak price as the base.
        entry = {**rates(off), "peak": entry, "peak_utc_weekdays": peak_ranges(off["windows"])}
    return entry


def main(argv):
    dry = "--dry-run" in argv
    names = [a for a in argv if a != "--dry-run"]
    with open(PRICES) as f:
        prices = json.load(f)
    with urllib.request.urlopen(URL, timeout=60) as r:
        litellm = json.load(r)
    changed = failed = 0
    for name in names or [k for k in prices if not k.startswith("_")]:
        e = litellm.get(name)
        try:
            if not e or "input_cost_per_token" not in e:
                raise ValueError("not priced in LiteLLM")
            new = convert(e)
        except (KeyError, ValueError) as err:
            print(f"{name}: kept, {err}")
            failed += 1
            continue
        if prices.get(name) != new:
            print(f"{name}: {json.dumps(prices.get(name))} -> {json.dumps(new)}")
            prices[name] = new
            changed += 1
    if changed and not dry:
        today = datetime.date.today().isoformat()
        prices["_note"] = (f"USD per 1M tokens, refreshed {today} by scripts/update_prices.py from LiteLLM's "
                           f"model_prices_and_context_window.json (OpenAI and DeepSeek entries cite their official "
                           f"pricing pages). Prices change: override any entry in ~/.muster/prices.json.")
        with open(PRICES, "w") as f:
            json.dump(prices, f, indent=2, ensure_ascii=False)
            f.write("\n")
    print(f"{changed} changed, {failed} kept" + (" (dry run)" if dry else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
