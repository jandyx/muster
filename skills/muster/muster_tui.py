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


def heatmap(counts, width, today):
    monday = today - today.weekday() * DAY
    weeks = min(53, max(0, (width - 4) // 2))  # Fill the width with up to a year, empty weeks included.
    mondays = [monday - i * 7 * DAY for i in reversed(range(weeks))]
    labels = ""
    for col, date in enumerate(mondays):
        end = min(today, date + 6 * DAY)
        starts_month = date.day == 1 or date.month != end.month
        if starts_month or (col == 0 and (weeks == 1 or (end + 7 * DAY).month == end.month)):
            label_start = min(2 * col, max(0, width - 7))  # Keep the last three-letter label on screen.
            labels = labels.ljust(label_start) + MONTHS[end.month - 1]
    lines = [[("    " + labels, "muted")]]
    values = list(counts.values())
    cuts = statistics.quantiles(values, method="inclusive") if len(values) > 1 else []
    for weekday, label in enumerate(("Mon", "", "Wed", "", "Fri", "", "")):
        row = [(f"{label:4}", "muted")]
        for date in mondays:
            count = counts.get(date + weekday * DAY, 0)
            level = 1 + sum(count > q for q in cuts) if count else 0
            row.append((SHADES[level] * 2, f"heat{level}"))
        lines.append(row)
    lines.append([("Less ", "muted"), *[(char + " ", f"heat{i}") for i, char in enumerate(SHADES[1:], 1)],
                  ("More", "muted")])
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


def comparison(summary, width):
    added = summary["lines_added"]
    if added:
        value, verb, unit = added, "added", "lines"
        references = [(10000, "200 pages of code at 50 lines per page"), (9000, "the original UNIX v6 kernel")]
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
    return text if len(text) <= width else short


def overview(records, counts, span, today, width):
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
        ("Pieces of work", f"{len(records)} ({g['settled']} settled/{g['open']} open)", "First pass", first_pass(g)),
        ("Accepted", g["accepted"], "Attempts per work", attempts),
        ("Sent back", g["needs_fixes"], "Claude fixed", g["claude_fixed"]),
        ("Blocked", g["blocked"], "Check failed", g["check_failed"]),
        ("Outside failures", g["outside_failures"], "Active days", f"{len(counts)}/{span}"),
        ("Busiest day", busiest, "Longest streak", longest),
        ("Favorite worker type", favorite, "Current streak", current),
        ("Median minutes", minutes, "", ""),
    ]
    left_width = max(cell_width(f"{label} {value}") for label, value, _, _ in metrics) + 3
    right_width = max(cell_width(f"{label} {value}") for _, _, label, value in metrics)
    split = min(left_width, max(1, width - right_width))
    lines = []
    for label, value, label2, value2 in metrics:
        left = [(label + " ", "plain"), (str(value), "accent")]
        if label2:
            left = clip(left, split - 1)
            used = sum(cell_width(text) for text, _ in left)
            left.extend([(" " * (split - used) + label2 + " ", "plain"), (str(value2), "accent")])
        lines.append(left)
    usage = f"Input {compact(g['tokens_in'])} · Output {compact(g['tokens_out'])} · Cost {cost(g)}"
    changed = f"Lines +{compact(g['lines_added'])} -{compact(g['lines_removed'])}"
    if len(usage) + 3 + len(changed) <= width:
        lines.append([(usage + " · " + changed, "muted")])
    else:
        lines.extend([[(usage, "muted")], [(changed, "muted")]])
    # Name the rate once so the type comparison reads as a single sentence.
    rates = []
    for (kind,), group in sorted(types, key=lambda item: item[0] != ("codex",)):
        totals = stats_summary(group)
        label = "" if rates else " first pass"
        rates.append(f"{kind} {first_pass(totals)}{label} (n={totals['accepted']})")
    if rates:
        lines.append([(" · ".join(rates), "plain")])
    lines.append([(comparison(g, width), "link")])
    return lines


def table(records, tab, width):
    keys = ("repo", "type") if tab == "Repos" else ("tier", "worker", "model")
    rows = []
    small_sample = False
    for key, group in stats_groups(records, keys):
        g = stats_summary(group)
        small = g["settled"] < 30
        small_sample |= small
        rate = first_pass(g).rjust(4) + ("*" if small else " ")
        bars = round(10 * g["first_pass"] / g["accepted"]) if g["accepted"] else 0
        row = [[(field, "plain")] for field in key]
        row += [[(str(g["settled"]), "plain")], [(rate, "muted" if small else "plain"),
                (" " + "█" * bars + "·" * (10 - bars), "accent")]]
        row.extend([[(str(g[field]), "plain")] for field in ("needs_fixes", "claude_fixed", "blocked", "check_failed")])
        row.append([(cost(g, per_accepted=True), "plain")])
        rows.append(row)

    # Shorter headers first: names are what tells the rows apart, so they are cut last.
    for names in (["Settled", "First pass", "Sent back", "Claude fixed", "Blocked", "Check failed", "Cost/accepted"],
                  ["Settled", "First pass", "Back", "Fixed", "Block", "Check", "$/ok"],
                  ["Set", "First pass", "Back", "Fix", "Blk", "Chk", "$/ok"]):
        header = [[(name, "muted")] for name in [k.title() for k in keys] + names]
        widths = [min(width, max(sum(cell_width(text) for text, _ in cell) for cell in cells))
                  for cells in zip(header, *rows)]
        if sum(widths) + len(widths) - 1 <= width:
            break
    excess = sum(widths) + len(widths) - 1 - width
    while excess > 0:
        col = max(range(len(keys)), key=widths.__getitem__)
        if widths[col] <= 3:
            break
        widths[col] -= 1
        excess -= 1

    def columns_line(cells):
        result = []
        for i, (cell, size) in enumerate(zip(cells, widths)):
            trimmed = clip(cell, size)
            if i < len(keys) and size >= 3 and cell_width(cell[0][0]) > size:
                name, style = cell[0]
                prefix = clip(cell, size // 2)
                suffix = clip([(name[::-1], style)], size - size // 2 - 1)
                trimmed = prefix + [("…", style)] + [(s[::-1], style) for s, _ in suffix]
            pad = " " * (size - sum(cell_width(text) for text, _ in trimmed))
            if i < len(keys):
                result.extend(trimmed + [(pad, "plain")])
            else:
                result.extend([(pad, "plain")] + trimmed)  # Numbers line up on the right.
            if i < len(cells) - 1:
                result.append((" ", "plain"))
        return result

    return columns_line(header), [columns_line(row) for row in rows], small_sample


def _screen(records, legacy, *, tab, range_name, width, height, today, scroll=0):
    if height <= 0:
        return [], 0
    tabs = []
    ranges = []
    for names, selected, segments, separator, on, off in (
            (TABS, tab, tabs, "   ", "tab_selected", "tab"),
            (RANGES, range_name, ranges, " · ", "accent_bold", "muted")):
        for name in names:
            if segments:
                segments.append((separator, "muted"))
            segments.append((name, on if name == selected else off))

    span = (None, 7, 30)[RANGES.index(range_name)]
    counts = {}
    for record in records:
        if record["started"]:
            day = record["started"].date()
            if day <= today:
                counts[day] = counts.get(day, 0) + 1
    head = [tabs]
    if tab == "Overview":
        head.extend(heatmap(counts, width, today))
    if span:
        since = today - span * DAY
        records = [r for r in records if r["started"] and since < r["started"].date() <= today]
        counts = {day: n for day, n in counts.items() if day > since}
    else:
        span = (today - min(counts)).days + 1 if counts else 0
    head.append(ranges)
    foot = [[(HINT, "muted")]]
    if tab == "Overview":
        body = overview(records, counts, span, today, width)
        if legacy and height >= 4:
            foot.insert(0, [(f"{legacy} pieces from before reviews were recorded are left out.", "muted")])
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
        header, body, small = table(records, tab, width)
        if height >= 16:
            head.append([])
            head.insert(1, [])
        head.append(header)
        if small and height >= len(head) + 3:
            foot.insert(0, [(SAMPLE_NOTE, "muted")])
        room = max(0, height - len(head) - len(foot))
        scroll = min(max(0, scroll), max(0, len(body) - room))
    available = max(0, height - len(foot))
    lines = (head + body[scroll:])[:available]
    lines += [[] for _ in range(available - len(lines))]
    return [clip(line, width) for line in lines + foot], scroll


def render(records, legacy, *, tab, range_name, width, height, today, scroll=0):
    """Return screen rows as (text, style) segments, clipped to the viewport."""
    lines, _ = _screen(records, legacy, tab=tab, range_name=range_name, width=width, height=height,
                       today=today, scroll=scroll)
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
        tab = range_index = scroll = 0
        while True:
            height, width = screen.getmaxyx()
            lines, scroll = _screen(records, legacy, tab=TABS[tab], range_name=RANGES[range_index],
                                    width=width, height=height, today=datetime.date.today(), scroll=scroll)
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
