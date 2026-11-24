"""Full-screen view of Muster's recorded work."""
import curses
import datetime
import statistics
import unicodedata

from muster import stats_groups, stats_summary

TABS = ("Overview", "Repos", "Workers")
RANGES = ("All time", "Last 7 days", "Last 30 days")
HINT = "←/→ switch tab · tab range · ↑/↓ scroll · esc close"
SHADES = "·░▒▓█"
SAMPLE_NOTE = "* fewer than 30 settled: small sample"
DAY = datetime.timedelta(days=1)
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in text)


def clip(line, width):
    result = []
    for text, style in line:
        if width <= 0:
            break
        if not text.isprintable():
            text = "".join(c if c.isprintable() else " " for c in text)
        size = cell_width(text)
        if size > width:
            end = 0
            for char in text:
                size = cell_width(char)
                if size > width:
                    break
                width -= size
                end += 1
            if end:
                result.append((text[:end], style))
            break
        if text:
            result.append((text, style))
        width -= size
    return result


def compact(value):
    for unit, suffix in ((10**9, "b"), (10**6, "m"), (1000, "k")):
        if value >= unit:
            return f"{value / unit:.1f}".removesuffix(".0") + suffix
    return f"{value:.0f}"


def heatmap(model, width):
    labels = model["labels"]
    count = max(0, (width - 4) // 2)
    weeks = model["weeks"][-count:] if count else []
    months = ""
    for col, week in enumerate(weeks):
        if week["starts_month"] or (col == 0 and (len(weeks) == 1 or week["first_label"])):
            label_start = min(2 * col, max(0, width - 7))  # Keep the last three-letter label on screen.
            months = months.ljust(label_start) + week["month"]
    lines = [[("    " + months, "muted")]]
    for weekday, label in enumerate(labels["weekdays"]):
        row = [(f"{label:4}", "muted")]
        for week in weeks:
            level = model["days"].get(week["days"][weekday], 0)
            row.append((labels["shades"][level] * 2, f"heat{level}"))
        lines.append(row)
    lines.append([(labels["less"] + " ", "muted"),
                  *[(char + " ", f"heat{i}") for i, char in enumerate(labels["shades"][1:], 1)],
                  (labels["more"], "muted")])
    return lines


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
        references = [(9000, "the original UNIX v6 kernel")]
    else:
        value = summary["tokens_in"] + summary["tokens_out"]
        verb, unit = "used", "tokens"
        references = [(800000, "War and Peace")]
    size, title = max(references, key=lambda ref: value // ref[0])
    multiple = int(value // size)
    if multiple:
        text = f"Workers {verb} ~{multiple}× the {unit} of {title} (~{compact(size)} {unit})"
        short = f"~{multiple}× {title} (~{compact(size)} {unit})"
    else:
        size, title = min(references)
        text = f"Workers {verb} {compact(value)} of the ~{compact(size)} {unit} in {title}."
        short = f"{compact(value)} / ~{compact(size)} {unit} of {title}"
    return text, short


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
    fun, short = comparison(g)
    tables = {"Repos": table_view(records, ("repo", "type")),
              "Workers": table_view(records, ("tier", "worker", "model"))}
    small = any(table["has_small"] for table in tables.values())
    return {"name": name, "metrics": metrics, "usage": usage + " · " + changed, "types": " · ".join(rates) or None,
            "fun": fun, "fun_short": short, "tables": tables, "sample_note": SAMPLE_NOTE if small else None}


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
        "medium": labels + ["Settled", "First pass", "Back", "Fixed", "Block", "Check", "$/ok"],
        "short": labels + ["Set", "First pass", "Back", "Fix", "Blk", "Chk", "$/ok"],
        "keys": len(keys), "rows": rows, "has_small": has_small,
    }


def view(records, legacy, today):
    """Build the shared, JSON-serialisable snapshot for the terminal and the pane."""
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
    return {"version": 1, "today": today.isoformat(), "days": days, "weeks": weeks, "ranges": ranges, "labels": labels,
            "legacy_note": f"{legacy} pieces from before reviews were recorded are left out." if legacy else None}


def overview(selected, width):
    metrics = selected["metrics"]
    left_width = max(cell_width(f"{label} {value}") for label, value in metrics[::2]) + 3
    right_width = max(cell_width(f"{label} {value}") for label, value in metrics[1::2])
    split = min(left_width, max(1, width - right_width))
    lines = []
    for i in range(0, len(metrics), 2):
        label, value = metrics[i]
        left = [(label + " ", "plain"), (value, "accent")]
        if i + 1 < len(metrics):
            label2, value2 = metrics[i + 1]
            left = clip(left, split - 1)
            used = sum(cell_width(text) for text, _ in left)
            left.extend([(" " * (split - used) + label2 + " ", "plain"), (value2, "accent")])
        lines.append(left)
    usage = selected["usage"]
    for line in ([usage] if len(usage) <= width else usage.rsplit(" · ", 1)):
        lines.append([(line, "muted")])
    if selected["types"]:
        lines.append([(selected["types"], "plain")])
    fun = selected["fun"]
    lines.append([(fun if len(fun) <= width else selected["fun_short"], "link")])
    return lines


def table(data, width):
    keys = data["keys"]
    rows = []
    for row in data["rows"]:
        cells = [[(value, "plain")] for value in row["cells"]]
        bar = row["bar"]
        cells[keys + 1] = [(row["cells"][keys + 1], "muted" if row["small"] else "plain"),
                           (" " + "█" * bar + "·" * (10 - bar), "accent")]
        rows.append(cells)

    # Shorter headers first: names are what tells the rows apart, so they are cut last.
    for names in (data["columns"], data["medium"], data["short"]):
        header = [[(name, "muted")] for name in names]
        widths = [min(width, max(sum(cell_width(text) for text, _ in cell) for cell in cells))
                  for cells in zip(header, *rows)]
        if sum(widths) + len(widths) - 1 <= width:
            break
    excess = sum(widths) + len(widths) - 1 - width
    while excess > 0:
        col = max(range(keys), key=widths.__getitem__)
        if widths[col] <= 3:
            break
        widths[col] -= 1
        excess -= 1

    def columns_line(cells):
        result = []
        for i, (cell, size) in enumerate(zip(cells, widths)):
            trimmed = clip(cell, size)
            if i < keys and size >= 3 and cell_width(cell[0][0]) > size:
                name, style = cell[0]
                prefix = clip(cell, size // 2)
                suffix = clip([(name[::-1], style)], size - size // 2 - 1)
                trimmed = prefix + [("…", style)] + [(s[::-1], style) for s, _ in suffix]
            pad = " " * (size - sum(cell_width(text) for text, _ in trimmed))
            if i < keys:
                result.extend(trimmed + [(pad, "plain")])
            else:
                result.extend([(pad, "plain")] + trimmed)  # Numbers line up on the right.
            if i < len(cells) - 1:
                result.append((" ", "plain"))
        return result

    return columns_line(header), [columns_line(row) for row in rows]


def _screen(model, *, tab, range_name, width, height, scroll=0):
    if height <= 0:
        return [], 0
    tabs = []
    ranges = []
    for names, selected, segments, separator, on, off in (
            (model["labels"]["tabs"], tab, tabs, "   ", "tab_selected", "tab"),
            ([r["name"] for r in model["ranges"]], range_name, ranges, " · ", "accent_bold", "muted")):
        for name in names:
            if segments:
                segments.append((separator, "muted"))
            segments.append((name, on if name == selected else off))

    selected = next(r for r in model["ranges"] if r["name"] == range_name)
    head = [tabs]
    if tab == "Overview":
        head.extend(heatmap(model, width))
    head.append(ranges)
    foot = [[(HINT, "muted")]]
    if tab == "Overview":
        body = overview(selected, width)
        if model["legacy_note"] and height >= 4:
            foot.insert(0, [(model["legacy_note"], "muted")])
        if height < len(head) + len(foot):
            head = [tabs, ranges] + head[1:-1]
        spare = height - len(head) - len(body) - len(foot)
        if spare > 0:
            head.append([])
        if spare > 1:
            head.insert(1, [])
        if spare > 2:
            body.insert(8, [])  # Separate the KPI block from usage and the type comparison.
        scroll = 0
    else:
        data = selected["tables"][tab]
        header, body = table(data, width)
        if height >= 16:
            head.append([])
            head.insert(1, [])
        head.append(header)
        if data["has_small"] and height >= len(head) + 3:
            foot.insert(0, [(selected["sample_note"], "muted")])
        room = max(0, height - len(head) - len(foot))
        scroll = min(max(0, scroll), max(0, len(body) - room))
    available = max(0, height - len(foot))
    lines = (head + body[scroll:])[:available]
    lines += [[] for _ in range(available - len(lines))]
    return [clip(line, width) for line in lines + foot], scroll


def render(records, legacy, *, tab, range_name, width, height, today, scroll=0):
    """Return screen rows as (text, style) segments, clipped to the viewport."""
    lines, _ = _screen(view(records, legacy, today), tab=tab, range_name=range_name, width=width,
                       height=height, scroll=scroll)
    return lines


def terminal_styles():
    accent = curses.A_BOLD
    muted = curses.A_DIM
    link = curses.A_BOLD
    selected = curses.A_REVERSE
    if curses.has_colors() and curses.COLORS >= 8:
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        rich = curses.COLORS >= 256
        curses.init_pair(1, 208 if rich else curses.COLOR_RED, background)
        curses.init_pair(2, 12 if rich else curses.COLOR_BLUE, background)
        accent, link = curses.color_pair(1), curses.color_pair(2)
        if rich:
            curses.init_pair(3, 8, background)
            curses.init_pair(4, curses.COLOR_BLACK, 147)
            muted, selected = curses.color_pair(3), curses.color_pair(4)
    styles = {"plain": 0, "muted": muted, "accent": accent, "accent_bold": accent | curses.A_BOLD,
              "link": link, "tab": 0, "tab_selected": selected, "heat0": muted}
    styles.update({f"heat{level}": accent for level in range(1, 5)})
    return styles


def read_key(screen):
    key = screen.getch()
    if key != 27:
        return key
    # Some terminals send normal-mode arrows even after curses enables application mode.
    screen.nodelay(True)
    try:
        if screen.getch() == ord("["):
            arrows = {ord("A"): curses.KEY_UP, ord("B"): curses.KEY_DOWN,
                      ord("C"): curses.KEY_RIGHT, ord("D"): curses.KEY_LEFT}
            return arrows.get(screen.getch(), -1)
        return 27
    finally:
        screen.nodelay(False)


def run(records, legacy):
    def loop(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass  # Some terminals have a fixed cursor.
        curses.set_escdelay(100)
        styles = terminal_styles()
        italic = getattr(curses, "A_ITALIC", 0) if curses.has_colors() else 0
        model = None
        tab = range_index = scroll = 0
        while True:
            today = datetime.date.today()
            if model is None or model["today"] != today.isoformat():
                model = view(records, legacy, today)
            height, width = screen.getmaxyx()
            lines, scroll = _screen(model, tab=TABS[tab], range_name=RANGES[range_index],
                                    width=width, height=height, scroll=scroll)
            screen.erase()
            for y, line in enumerate(lines):
                x = 0
                for text, style in line:
                    attr = styles[style] | (italic if y == height - 1 else 0)
                    end = x + cell_width(text)
                    if y == height - 1 and end == width:
                        try:
                            screen.addstr(y, x, text, attr)
                        except curses.error:
                            pass  # ncurses wrote the last cell but could not advance the cursor.
                    else:
                        screen.addstr(y, x, text, attr)
                    x = end
            screen.refresh()
            key = read_key(screen)
            if key in (27, ord("q")):
                return
            if key in (curses.KEY_RIGHT, ord("l"), curses.KEY_LEFT, ord("h")):
                delta = 1 if key in (curses.KEY_RIGHT, ord("l")) else -1
                tab = (tab + delta) % len(TABS)
                scroll = 0
            elif key == ord("\t"):
                range_index = (range_index + 1) % len(RANGES)
                scroll = 0
            elif key in (curses.KEY_DOWN, ord("j")):
                scroll += 1
            elif key in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            # KEY_RESIZE also starts a fresh layout with the new dimensions.

    curses.wrapper(loop)
