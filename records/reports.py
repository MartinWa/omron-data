import calendar
import math
from datetime import date, timedelta

MAX_BARS = 7
CHART_WIDTH = 720
CHART_HEIGHT = 240
CHART_LEFT = 54
CHART_RIGHT = 14
CHART_TOP = 14
CHART_BOTTOM = 34
MONTH_ABBR = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def nice_max(value):
    if value is None or not math.isfinite(float(value)) or value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    for multiplier in (1, 2, 4, 5, 10):
        candidate = multiplier * (10**exponent)
        if candidate >= value:
            return float(candidate)
    return float(10 ** (exponent + 1))


def _round_coord(value):
    if value is None or not math.isfinite(float(value)):
        return 0.0
    return round(float(value), 2)


def _tick_label(value, unit):
    text = str(int(value)) if value == int(value) else f"{value:g}"
    return f"{text}{unit}" if unit else text


def axis_ticks(maximum, unit=""):
    return [
        {"value": 0, "label": _tick_label(0, unit)},
        {"value": maximum / 2, "label": _tick_label(maximum / 2, unit)},
        {"value": maximum, "label": _tick_label(maximum, unit)},
    ]


def _short_date(value):
    return f"{value.day} {MONTH_ABBR[value.month - 1]} {value.year}"


def _shift_months(value, months):
    index = value.month - 1 + months
    year = value.year + index // 12
    month = index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _bucket_start(value, bucket):
    if bucket == "day":
        return value
    if bucket == "week":
        return value - timedelta(days=value.weekday())
    if bucket == "month":
        return value.replace(day=1)
    return value.replace(month=1, day=1)


def _bucket_end(current, bucket):
    if bucket == "day":
        return current
    if bucket == "week":
        return current + timedelta(days=6)
    if bucket == "month":
        return _shift_months(current, 1) - timedelta(days=1)
    return date(current.year, 12, 31)


def _iter_buckets(start, end, bucket):
    current = _bucket_start(start, bucket)
    final = _bucket_start(end, bucket)
    while current <= final:
        yield current
        if current == final:
            return
        if bucket == "day":
            current += timedelta(days=1)
        elif bucket == "week":
            current += timedelta(days=7)
        elif bucket == "month":
            current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            current = current.replace(year=current.year + 1)


def partial_buckets(start, end, bucket, noun=""):
    label = noun or bucket
    info = {}
    for current in _iter_buckets(start, end, bucket):
        final = _bucket_end(current, bucket)
        clipped_start = max(current, start)
        clipped_end = min(final, end)
        days = max(0, (clipped_end - clipped_start).days + 1)
        total = (final - current).days + 1
        in_progress = final > end >= current
        partial = days < total
        if in_progress:
            note = "in progress"
        elif partial:
            note = f"partial {label}, {days} of {total} days"
        else:
            note = ""
        info[current] = {
            "partial": partial,
            "in_progress": in_progress,
            "days": days,
            "total": total,
            "note": note,
        }
    return info


def _full_bucket_label(value, bucket):
    if bucket == "day":
        return _short_date(value)
    if bucket == "week":
        return f"Week of {value.day} {MONTH_ABBR[value.month - 1]} {value.year}"
    if bucket == "month":
        return f"{MONTH_ABBR[value.month - 1]} {value.year}"
    return str(value.year)


def _tick_bucket_label(value, bucket, multi_year):
    if bucket in {"day", "week"}:
        return f"{value.day} {MONTH_ABBR[value.month - 1]}"
    if bucket == "month":
        if multi_year and value.month == 1:
            return str(value.year)
        return MONTH_ABBR[value.month - 1]
    return str(value.year)


def thin_indexes(count, maximum=MAX_BARS):
    if count <= maximum:
        return list(range(count))
    indexes = []
    for step in range(maximum):
        index = round(step * (count - 1) / (maximum - 1))
        if index not in indexes:
            indexes.append(index)
    return indexes


def _plot_frame():
    plot_width = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
    plot_height = CHART_HEIGHT - CHART_TOP - CHART_BOTTOM
    return plot_width, plot_height


def bar_chart(points, *, unit="", bucket="week", multi_year=False, value_text=None, aria_label=""):
    plot_width, plot_height = _plot_frame()
    maximum = nice_max(max((point["value"] for point in points), default=0))
    ticks = axis_ticks(maximum, unit)
    count = len(points)
    slot = plot_width / count if count else plot_width
    bar_width = max(3.0, slot * 0.62)
    bars = []
    ma_points = []
    for index, point in enumerate(points):
        value = point["value"]
        height = plot_height * (value / maximum) if maximum else 0
        x = CHART_LEFT + slot * index + (slot - bar_width) / 2
        y = CHART_TOP + plot_height - height
        label = _full_bucket_label(point["date"], bucket)
        text = value_text(point) if value_text else _tick_label(value, unit)
        title = f"{label}: {text}"
        if point.get("note"):
            title = f"{title}, {point['note']}"
        bars.append(
            {
                "x": _round_coord(x),
                "y": _round_coord(y),
                "width": _round_coord(bar_width),
                "height": _round_coord(height),
                "title": title,
                "partial": point.get("partial", False),
                "in_progress": point.get("in_progress", False),
                "index": index,
            }
        )
        if point.get("moving_average") is not None:
            ma_value = point["moving_average"]
            ma_y = CHART_TOP + plot_height - plot_height * (ma_value / maximum)
            ma_points.append(
                f"{_round_coord(CHART_LEFT + slot * index + slot / 2)},{_round_coord(ma_y)}"
            )
    indexes = thin_indexes(count)
    xlabels = [
        {
            "x": _round_coord(CHART_LEFT + slot * index + slot / 2),
            "label": _tick_bucket_label(points[index]["date"], bucket, multi_year),
        }
        for index in indexes
    ]
    tick_positions = [
        {"y": _round_coord(CHART_TOP + plot_height), "label": ticks[0]["label"]},
        {"y": _round_coord(CHART_TOP + plot_height / 2), "label": ticks[1]["label"]},
        {"y": _round_coord(CHART_TOP), "label": ticks[2]["label"]},
    ]
    return {
        "kind": "bars",
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "left": CHART_LEFT,
        "right": CHART_RIGHT,
        "top": CHART_TOP,
        "bottom": CHART_BOTTOM,
        "plot_bottom": _round_coord(CHART_TOP + plot_height),
        "right_edge": CHART_WIDTH - CHART_RIGHT,
        "bars": bars,
        "line": " ".join(ma_points),
        "has_line": len(ma_points) >= 2,
        "xlabels": xlabels,
        "ticks": tick_positions,
        "aria_label": aria_label,
        "empty": maximum <= 1.0 and all(point["value"] == 0 for point in points),
    }


def line_chart(lines, *, x_max, x_ticks, y_unit="", aria_label=""):
    plot_width, plot_height = _plot_frame()
    values = [value for line in lines for _, value in line["points"]]
    maximum = nice_max(max(values, default=0))
    x_max = x_max if x_max > 0 else 1

    def project(x_value, y_value):
        x = CHART_LEFT + plot_width * (x_value / x_max)
        y = CHART_TOP + plot_height - plot_height * (y_value / maximum)
        return f"{_round_coord(x)},{_round_coord(y)}"

    drawn = []
    for index, line in enumerate(lines):
        if not line["points"]:
            continue
        projected = [project(x, y) for x, y in line["points"]]
        dots = []
        if len(projected) == 1:
            x, y = projected[0].split(",")
            dots.append({"x": x, "y": y})
        drawn.append(
            {
                "label": line["label"],
                "class": f"series-{index + 1}",
                "points": " ".join(projected),
                "dots": dots,
            }
        )
    tick_positions = [
        {"y": _round_coord(CHART_TOP + plot_height), "label": _tick_label(0, y_unit)},
        {"y": _round_coord(CHART_TOP + plot_height / 2), "label": _tick_label(maximum / 2, y_unit)},
        {"y": _round_coord(CHART_TOP), "label": _tick_label(maximum, y_unit)},
    ]
    xlabels = [
        {"x": _round_coord(CHART_LEFT + plot_width * (position / x_max)), "label": label}
        for position, label in x_ticks
    ]
    return {
        "kind": "lines",
        "width": CHART_WIDTH,
        "height": CHART_HEIGHT,
        "left": CHART_LEFT,
        "right": CHART_RIGHT,
        "top": CHART_TOP,
        "bottom": CHART_BOTTOM,
        "plot_bottom": _round_coord(CHART_TOP + plot_height),
        "right_edge": CHART_WIDTH - CHART_RIGHT,
        "lines": drawn,
        "xlabels": xlabels,
        "ticks": tick_positions,
        "aria_label": aria_label,
        "empty": maximum <= 1.0 and all(value == 0 for value in values),
    }
