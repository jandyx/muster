"""Build the JSON stats view that /muster-stats draws in Claude Code."""
import datetime
import statistics

from muster import stats_groups, stats_summary

TABS = ("Overview", "Repos", "Workers")
RANGES = ("All time", "Last 7 days", "Last 30 days")
SHADES = "·░▒▓█"
SAMPLE_NOTE = "* fewer than 30 settled: small sample"
DAY = datetime.timedelta(days=1)
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def compact(value):
    for unit, suffix in ((10**9, "b"), (10**6, "m"), (1000, "k")):
        if value >= unit:
            return f"{value / unit:.1f}".removesuffix(".0") + suffix
    return f"{value:.0f}"


def first_pass(summary):
    accepted = summary["accepted"]
    return f"{100 * summary['first_pass'] / accepted:.0f}%" if accepted else "-"


def cost(summary, per_accepted=False):
    value = summary["usd"]
    if value is None or (per_accepted and not summary["accepted"]):
        return "-"
    prefix = "≥" if summary["priced_partially"] else ""
    if per_accepted:
        return f"{prefix}${value / summary['accepted']:.2f}"
    dollars = f"{value:.2f}" if 0 < value < 1 else f"{value:.0f}"
    return f"{prefix or '≈'}${dollars}"


def comparison(summary):
    added = summary["lines_added"]
    if added:
        value, verb, unit = added, "added", "lines"
        size, title = 9000, "the original UNIX v6 kernel"
    else:
        value = summary["tokens_in"] + summary["tokens_out"]
        verb, unit = "used", "tokens"
        size, title = 800000, "War and Peace"
    multiple = int(value // size)
    if multiple:
        return f"Workers {verb} ~{multiple}× the {unit} of {title} (~{compact(size)} {unit})"
    return f"Workers {verb} {compact(value)} of the ~{compact(size)} {unit} in {title}."


def range_view(name, records, counts, span, today):
    g = stats_summary(records)
    longest = current = streak = 0
    previous = None
    for date in sorted(counts):
        streak = streak + 1 if previous and date - previous == DAY else 1
        longest = max(longest, streak)
        if date in (today, today - DAY):
            current = streak
        previous = date
    types = list(stats_groups(records, ("type",)))
    favorite = max(types, key=lambda item: len(item[1]))[0][0] if types else "-"
    busiest = max(sorted(counts), key=counts.get).isoformat() if counts else "-"
    attempts = f"{g['attempts'] / g['settled']:.1f}" if g["settled"] else "-"
    minutes = f"{g['median_minutes']:.1f}" if g["median_minutes"] is not None else "-"
    metrics = [
        ["Pieces of work", f"{len(records)} ({g['settled']} settled/{g['open']} open)"],
        ["First pass", first_pass(g)],
        ["Accepted", str(g["accepted"])],
        ["Attempts per work", attempts],
        ["Sent back", str(g["needs_fixes"])],
        ["Claude fixed", str(g["claude_fixed"])],
        ["Blocked", str(g["blocked"])],
        ["Check failed", str(g["check_failed"])],
        ["Outside failures", str(g["outside_failures"])],
        ["Active days", f"{len(counts)}/{span}"],
        ["Busiest day", busiest],
        ["Longest streak", str(longest)],
        ["Favorite worker type", favorite],
        ["Current streak", str(current)],
        ["Median minutes", minutes],
    ]
    usage = f"Input {compact(g['tokens_in'])} · Output {compact(g['tokens_out'])} · Cost {cost(g)}"
    changed = f"Lines +{compact(g['lines_added'])} -{compact(g['lines_removed'])}"
    # Name the rate once so the type comparison reads as a single sentence.
    rates = []
    for (kind,), group in sorted(types, key=lambda item: item[0] != ("codex",)):
        totals = stats_summary(group)
        label = "" if rates else " first pass"
        rates.append(f"{kind} {first_pass(totals)}{label} (n={totals['accepted']})")
    tables = {"Repos": table_view(records, ("repo", "type")),
              "Workers": table_view(records, ("tier", "worker", "model"))}
    small = any(table["has_small"] for table in tables.values())
    return {"name": name, "metrics": metrics, "usage": usage + " · " + changed, "types": " · ".join(rates) or None,
            "fun": comparison(g), "tables": tables, "sample_note": SAMPLE_NOTE if small else None}


def table_view(records, keys):
    rows = []
    has_small = False
    for key, group in stats_groups(records, keys):
        g = stats_summary(group)
        small = g["settled"] < 30
        has_small |= small
        rate = first_pass(g).rjust(4) + ("*" if small else " ")
        bars = round(10 * g["first_pass"] / g["accepted"]) if g["accepted"] else 0
        cells = [*key, str(g["settled"]), rate]
        cells.extend(str(g[field]) for field in ("needs_fixes", "claude_fixed", "blocked", "check_failed"))
        cells.append(cost(g, per_accepted=True))
        rows.append({"cells": cells, "bar": bars, "small": small})
    labels = [key.title() for key in keys]
    return {
        "columns": labels + ["Settled", "First pass", "Sent back", "Claude fixed", "Blocked", "Check failed", "Cost/accepted"],
        "short": labels + ["Set", "First pass", "Back", "Fix", "Blk", "Chk", "$/ok"],
        "keys": len(keys), "rows": rows, "has_small": has_small,
    }


def view(records, legacy, today):
    counts = {}
    for record in records:
        if record["started"]:
            day = record["started"].date()
            if day <= today:
                counts[day] = counts.get(day, 0) + 1
    values = list(counts.values())
    cuts = statistics.quantiles(values, method="inclusive") if len(values) > 1 else []
    days = {day.isoformat(): 1 + sum(count > q for q in cuts) for day, count in counts.items()
            if day > today - 371 * DAY}
    monday = today - today.weekday() * DAY
    weeks = []
    for offset in reversed(range(53)):
        start = monday - offset * 7 * DAY
        end = min(today, start + 6 * DAY)
        weeks.append({"days": [(start + i * DAY).isoformat() for i in range(7)], "month": MONTHS[end.month - 1],
                      "starts_month": start.day == 1 or start.month != end.month,
                      "first_label": (end + 7 * DAY).month == end.month})
    span = (today - min(counts)).days + 1 if counts else 0
    ranges = [range_view(RANGES[0], records, counts, span, today)]
    for name, span in zip(RANGES[1:], (7, 30)):
        since = today - span * DAY
        recent = [r for r in records if r["started"] and since < r["started"].date() <= today]
        ranges.append(range_view(name, recent, {day: n for day, n in counts.items() if day > since}, span, today))
    labels = {"tabs": list(TABS), "weekdays": ["Mon", "", "Wed", "", "Fri", "", ""],
              "less": "Less", "more": "More", "shades": SHADES,
              "pane_hint": "tab/shift+tab select · ↑/↓ scroll · esc to prompt"}
    return {"version": 2, "today": today.isoformat(), "days": days, "weeks": weeks, "ranges": ranges, "labels": labels,
            "legacy_note": f"{legacy} pieces from before reviews were recorded are left out." if legacy else None}
