import calendar
import math
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Min
from django.urls import reverse
from django.utils import timezone

from .models import METRICS, PANEL, REFERENCE_BANDS, ImportRun, Measurement
from .templatetags.formatting import axis_unit, num

MAX_BARS = 7
CHART_WIDTH = 720
CHART_HEIGHT = 240
TALL_CHART_HEIGHT = 380
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


def nice_step(raw, integer=False):
    if raw is None or not math.isfinite(float(raw)) or raw <= 0:
        return 1.0
    exponent = math.floor(math.log10(raw))
    for multiplier in (1, 2, 2.5, 5, 10):
        if integer and multiplier == 2.5 and exponent < 1:
            continue
        candidate = multiplier * (10**exponent)
        if candidate >= raw:
            return float(candidate)
    return float(10 ** (exponent + 1))


def nice_domain(lo, hi, integer=False):
    lo = float(lo)
    hi = float(hi)
    if not math.isfinite(lo):
        lo = 0.0
    if not math.isfinite(hi):
        hi = 1.0
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        span = max(abs(hi) * 0.1, 1.0)
        lo, hi = lo - span / 2, hi + span / 2
    step = nice_step((hi - lo) / 4, integer=integer)
    dmin = round(math.floor(lo / step + 1e-9) * step, 9)
    dmax = round(math.ceil(hi / step - 1e-9) * step, 9)
    steps = round((dmax - dmin) / step)
    if steps % 2:
        dmax = round(dmax + step, 9)
        steps += 1
    if steps < 2:
        dmax = round(dmin + 2 * step, 9)
        steps = 2
    return dmin, dmax, step


def _round_coord(value):
    if value is None or not math.isfinite(float(value)):
        return 0.0
    return round(float(value), 2)


def _tick_label(value, unit):
    text = str(int(value)) if value == int(value) else f"{value:g}"
    return f"{text}{axis_unit(unit)}"


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


def partial_buckets(start, end, bucket, noun="", today=None):
    label = noun or bucket
    reference = end if today is None else today
    info = {}
    for current in _iter_buckets(start, end, bucket):
        final = _bucket_end(current, bucket)
        clipped_start = max(current, start)
        clipped_end = min(final, end)
        days = max(0, (clipped_end - clipped_start).days + 1)
        total = (final - current).days + 1
        in_progress = final > end >= current and end == reference
        partial = in_progress
        if in_progress:
            note = "in progress"
        elif days < total:
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
        label = f"{value.day} {MONTH_ABBR[value.month - 1]}"
        if multi_year and value.month == 1:
            label = f"{label} {value.year}"
        return label
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


def _plot_frame(height=None):
    height = CHART_HEIGHT if height is None else height
    plot_width = CHART_WIDTH - CHART_LEFT - CHART_RIGHT
    plot_height = height - CHART_TOP - CHART_BOTTOM
    return plot_width, plot_height


def bar_chart(
    points,
    *,
    unit="",
    bucket="week",
    multi_year=False,
    value_text=None,
    aria_label="",
    height=None,
    label_of=None,
):
    frame_height = CHART_HEIGHT if height is None else height
    plot_width, plot_height = _plot_frame(frame_height)
    maximum = nice_max(max((point["value"] for point in points), default=0))
    ticks = axis_ticks(maximum, unit)
    count = len(points)
    slot = plot_width / count if count else plot_width
    bar_width = max(1.0, slot * 0.62)
    bars = []
    for index, point in enumerate(points):
        value = point["value"]
        height = plot_height * (value / maximum) if maximum else 0
        x = CHART_LEFT + slot * index + (slot - bar_width) / 2
        y = CHART_TOP + plot_height - height
        label = label_of(point) if label_of else _full_bucket_label(point["date"], bucket)
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
    indexes = thin_indexes(count)
    xlabels = [
        {
            "x": _round_coord(CHART_LEFT + slot * index + slot / 2),
            "label": (
                label_of(points[index])
                if label_of
                else _tick_bucket_label(points[index]["date"], bucket, multi_year)
            ),
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
        "height": frame_height,
        "left": CHART_LEFT,
        "right": CHART_RIGHT,
        "top": CHART_TOP,
        "bottom": CHART_BOTTOM,
        "plot_bottom": _round_coord(CHART_TOP + plot_height),
        "plot_width": plot_width,
        "right_edge": CHART_WIDTH - CHART_RIGHT,
        "bars": bars,
        "xlabels": xlabels,
        "ticks": tick_positions,
        "aria_label": aria_label,
        "empty": maximum <= 1.0 and all(point["value"] == 0 for point in points),
    }


def _dot(shape, x, y, dot_class="", title=""):
    dot = {"x": x, "y": y, "class": dot_class, "title": title}
    if shape == "rect":
        dot["x"] = _round_coord(x - 3)
        dot["y"] = _round_coord(y - 3)
        dot["width"] = 6.0
        dot["height"] = 6.0
    elif shape == "polygon":
        dot["points"] = " ".join(
            f"{_round_coord(x + dx)},{_round_coord(y + dy)}"
            for dx, dy in ((0, -4), (4, 0), (0, 4), (-4, 0))
        )
    return dot


def line_chart(
    lines,
    *,
    x_max,
    x_ticks,
    y_unit="",
    aria_label="",
    y_min=None,
    y_max=None,
    max_gap=None,
    height=None,
):
    frame_height = CHART_HEIGHT if height is None else height
    plot_width, plot_height = _plot_frame(frame_height)
    x_max = x_max if x_max > 0 else 1
    values = [
        point[1]
        for line in lines
        for point in line["points"]
        if point[1] is not None and math.isfinite(point[1])
    ]
    if values:
        lo = min(values + ([y_min] if y_min is not None else []))
        hi = max(values + ([y_max] if y_max is not None else []))
    else:
        lo = 0.0 if y_min is None else float(y_min)
        hi = 1.0 if y_max is None else float(y_max)
        if hi <= lo:
            hi = lo + 1.0
    dmin, dmax, _step = nice_domain(lo, hi, integer=UNIT_PRECISION.get(y_unit, 1) == 0)

    def project(x_value, y_value):
        x = CHART_LEFT + plot_width * (x_value / x_max)
        y = CHART_TOP + plot_height - plot_height * (y_value - dmin) / (dmax - dmin)
        return _round_coord(x), _round_coord(y)

    drawn = []
    for index, line in enumerate(lines):
        points = []
        tones = []
        titles = []
        for point in line["points"]:
            if point[1] is None:
                continue
            points.append((point[0], point[1]))
            tones.append(point[2] if len(point) > 2 else "")
            titles.append(point[3] if len(point) > 3 else "")
        if not points:
            continue
        line_class = line.get("class") or f"series-{index + 1}"
        if "series-3" in line_class:
            shape = "polygon"
        elif "series-2" in line_class or "series-4" in line_class:
            shape = "rect"
        else:
            shape = "circle"
        projected = [
            (x, y, *project(x, y), tone, title)
            for (x, y), tone, title in zip(points, tones, titles, strict=True)
        ]
        runs = []
        run = []
        previous_x = None
        for item in projected:
            if (
                max_gap is not None
                and previous_x is not None
                and item[0] - previous_x > max_gap
                and run
            ):
                runs.append(run)
                run = []
            run.append(item)
            previous_x = item[0]
        if run:
            runs.append(run)
        segments = [" ".join(f"{px},{py}" for _, _, px, py, *_ in run) for run in runs]
        dots = []
        for run in runs:
            if line.get("dots_all") or len(run) == 1:
                for _, _, px, py, tone, title in run:
                    dots.append(_dot(shape, px, py, tone, title))
        if line.get("latest_title") and projected:
            if dots and (line.get("dots_all") or len(runs[-1]) == 1):
                dots[-1]["class"] = f"{dots[-1]['class']} dot-latest".strip()
                dots[-1]["title"] = line["latest_title"]
            else:
                _, _, px, py, tone, _ = projected[-1]
                dots.append(_dot(shape, px, py, f"{tone} dot-latest".strip(), line["latest_title"]))
        marks = [
            {
                "x": project(mark_x, mark_y)[0],
                "y": project(mark_x, mark_y)[1],
                "class": line_class,
                "title": title,
            }
            for mark_x, mark_y, title in line.get("marks", [])
        ]
        drawn.append(
            {
                "label": line["label"],
                "title": line["label"],
                "class": line_class,
                "segments": segments,
                "dots": dots,
                "marks": marks,
            }
        )
    mid = (dmin + dmax) / 2
    tick_positions = [
        {"y": _round_coord(CHART_TOP + plot_height), "label": _tick_label(dmin, y_unit)},
        {"y": _round_coord(CHART_TOP + plot_height / 2), "label": _tick_label(mid, y_unit)},
        {"y": _round_coord(CHART_TOP), "label": _tick_label(dmax, y_unit)},
    ]
    xlabels = [
        {"x": _round_coord(CHART_LEFT + plot_width * (position / x_max)), "label": label}
        for position, label in x_ticks
    ]
    return {
        "kind": "lines",
        "width": CHART_WIDTH,
        "height": frame_height,
        "left": CHART_LEFT,
        "right": CHART_RIGHT,
        "top": CHART_TOP,
        "bottom": CHART_BOTTOM,
        "plot_bottom": _round_coord(CHART_TOP + plot_height),
        "plot_width": plot_width,
        "right_edge": CHART_WIDTH - CHART_RIGHT,
        "y_min": dmin,
        "y_max": dmax,
        "max_gap": max_gap,
        "lines": drawn,
        "xlabels": xlabels,
        "ticks": tick_positions,
        "aria_label": aria_label,
        "empty": not values,
    }


RANGE_KEYS = ("7d", "30d", "90d", "1y", "all", "custom")
DEFAULT_RANGE = "30d"
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
RANGE_LABELS = {
    "7d": "Last 7 days",
    "30d": "30 days",
    "90d": "90 days",
    "1y": "1 year",
    "all": "All time",
    "custom": "Custom",
}
RANGE_VERSUS = {
    "7d": "the previous 7 days",
    "30d": "the previous 30 days",
    "90d": "the previous 90 days",
    "1y": "the previous year",
}
COMPOSITION_METRICS = (
    "body_fat_pct",
    "skeletal_muscle_pct",
    "visceral_fat",
    "resting_metabolism",
    "impedance",
)
COMBINED_COMPOSITION = ("body_fat_pct", "skeletal_muscle_pct")
DEFAULT_COMPOSITION = COMPOSITION_METRICS[0]
HEATMAP_DAYS = 364
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
SEVEN_DAYS = 7
METRIC_ROW_LIMIT = 500
CARD_ROW_LIMIT = 200
PRESSURE_METRICS = ("systolic_bp", "diastolic_bp")

METRIC_DOMAINS = {
    "body_fat_pct": (20, 45),
    "skeletal_muscle_pct": (15, 45),
    "visceral_fat": (1, 20),
    "resting_metabolism": (1500, 2500),
    "impedance": (200, 600),
}

UNIT_PRECISION = {"mmHg": 0, "kg": 1, "%": 1, "°C": 1, "/min": 0, "kcal": 0, "ohm": 0}

METRIC_LABELS = {
    "blood_pressure": "Blood pressure",
    "systolic_bp": "Systolic",
    "diastolic_bp": "Diastolic",
    "pulse": "Pulse",
    "body_weight": "Weight",
    "bmi": "BMI",
    "body_fat_pct": "Body fat",
    "skeletal_muscle_pct": "Skeletal muscle",
    "body_water_pct": "Body water",
    "visceral_fat": "Visceral fat",
    "resting_metabolism": "Resting metabolism",
    "impedance": "Impedance",
    "fat_mass": "Fat mass",
    "average_weight_reported": "Average weight as reported",
    "average_fat_mass": "Average fat mass",
    "body_temperature": "Body temperature",
}

COMPOSITION_SWITCHER = (
    ("body_fat_pct", "Body fat & muscle"),
    ("visceral_fat", METRIC_LABELS["visceral_fat"]),
    ("resting_metabolism", METRIC_LABELS["resting_metabolism"]),
    ("impedance", METRIC_LABELS["impedance"]),
)


def _param(params, key, allowed, default):
    value = "" if params is None else str(params.get(key) or "").strip()
    return value if value in allowed else default


def _date_param(params, key):
    value = "" if params is None else str(params.get(key) or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_dashboard_params(params):
    return {
        "range": _param(params, "range", RANGE_KEYS, DEFAULT_RANGE),
        "metric": _param(params, "metric", COMPOSITION_METRICS, DEFAULT_COMPOSITION),
        "from": _date_param(params, "from"),
        "to": _date_param(params, "to"),
    }


def _options(keys, labels, current):
    return [{"key": key, "label": labels.get(key, key), "current": key == current} for key in keys]


def _composition_options(current):
    return [
        {
            "key": key,
            "label": label,
            "current": key == current
            or (key == COMBINED_COMPOSITION[0] and current in COMBINED_COMPOSITION),
        }
        for key, label in COMPOSITION_SWITCHER
    ]


def period_label(start, end):
    left = f"{start.day} {MONTH_ABBR[start.month - 1]}"
    right = f"{end.day} {MONTH_ABBR[end.month - 1]}"
    if start == end:
        return f"{left} {end.year}"
    if start.year == end.year:
        return f"{left} – {right} {end.year}"
    return f"{left} {start.year} – {right} {end.year}"


def resolve_range(key, today, earliest, date_from=None, date_to=None):
    if key not in RANGE_KEYS:
        key = DEFAULT_RANGE
    if key == "custom":
        if date_from is None or date_to is None or date_from > date_to:
            return resolve_range(DEFAULT_RANGE, today, earliest)
        start = date_from
        end = min(date_to, today)
        if start > end:
            return resolve_range(DEFAULT_RANGE, today, earliest)
        days = (end - start).days + 1
        previous = {"start": start - timedelta(days=days), "end": start - timedelta(days=1)}
        return {
            "key": "custom",
            "start": start,
            "end": end,
            "bucket": "week",
            "previous": previous,
            "label": period_label(start, end),
            "partial": partial_buckets(start, end, "week", today=today),
        }
    previous = None
    if key == "all":
        start = earliest or today
    else:
        days = RANGE_DAYS[key]
        start = today - timedelta(days=days - 1)
        previous_end = start - timedelta(days=1)
        previous = {"start": previous_end - timedelta(days=days - 1), "end": previous_end}
    return {
        "key": key,
        "start": start,
        "end": today,
        "bucket": "week",
        "previous": previous,
        "label": period_label(start, today),
        "partial": partial_buckets(start, today, "week", today=today),
    }


def filtered_url(path, query):
    return f"{path}?{urlencode({key: str(value) for key, value in query.items() if value})}"


def _range_query(key, date_from=None, date_to=None):
    query = {"range": key}
    if key == "custom":
        if date_from is not None:
            query["from"] = date_from.isoformat()
        if date_to is not None:
            query["to"] = date_to.isoformat()
    return query


def delta(current, previous, *, absolute=False, suffix="", precision=1, harm="none", comparison=""):
    if current is None or previous is None:
        return None
    if harm not in {"none", "up", "down"}:
        harm = "none"

    def tone_of(direction):
        if direction == "flat" or harm == "none":
            return "neutral"
        worse = "up" if harm == "up" else "down"
        return "worse" if direction == worse else "better"

    if absolute:
        change = current - previous
        if abs(change) < 0.5 * 10 ** (-precision):
            change = 0.0
        if change > 0:
            text = f"+{change:.{precision}f}{suffix}"
            direction = "up"
            spoken = f"up {abs(change):.{precision}f}{suffix}"
        elif change < 0:
            text = f"−{abs(change):.{precision}f}{suffix}"
            direction = "down"
            spoken = f"down {abs(change):.{precision}f}{suffix}"
        else:
            text = "—"
            direction = "flat"
            spoken = "unchanged"
        return {
            "text": text,
            "direction": direction,
            "tone": tone_of(direction),
            "aria": f"{spoken} versus {comparison}",
        }
    if previous == 0:
        return {
            "text": "—",
            "direction": "flat",
            "tone": "neutral",
            "aria": f"no reading in {comparison} to compare",
        }
    change = (current - previous) / previous * 100
    percent = round(abs(change))
    if change > 0:
        text = f"↑ {percent}%"
        direction = "up"
        spoken = f"up {percent} percent"
    elif change < 0:
        text = f"↓ {percent}%"
        direction = "down"
        spoken = f"down {percent} percent"
    else:
        text = "—"
        direction = "flat"
        spoken = "unchanged"
    return {
        "text": text,
        "direction": direction,
        "tone": tone_of(direction),
        "aria": f"{spoken} versus {comparison}",
    }


def _mean(values):
    collected = list(values)
    if not collected:
        return None
    return sum(collected) / len(collected)


def _week_start(day):
    return day - timedelta(days=day.weekday())


def _day_start(day):
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.OMRON_TIME_ZONE))


def _history(owner, window=None):
    series = {}
    rows = Measurement.objects.filter(owner=owner)
    if window:
        start, end = window
        rows = rows.filter(at__gte=_day_start(start), at__lt=_day_start(end + timedelta(days=1)))
    for metric, at, value, note in rows.order_by("at", "id").values_list(
        "metric", "at", "value", "note"
    ):
        if value is None:
            continue
        moment = timezone.localtime(at) if timezone.is_aware(at) else at
        series.setdefault(metric, []).append(
            {
                "at": at,
                "date": moment.date(),
                "value": float(value),
                "note": note,
                "stamp": f"{_short_date(moment.date())} {moment:%H:%M}",
            }
        )
    return series


def _first_date(owner):
    earliest = Measurement.objects.filter(owner=owner, value__isnull=False).aggregate(
        first=Min("at")
    )["first"]
    if earliest is None:
        return None
    moment = timezone.localtime(earliest) if timezone.is_aware(earliest) else earliest
    return moment.date()


def _fetch_window(resolved):
    starts = [resolved["start"], resolved["end"] - timedelta(days=HEATMAP_DAYS)]
    if resolved["previous"]:
        starts.append(resolved["previous"]["start"])
    return min(starts), resolved["end"]


def _in_window(points, start, end):
    return [point for point in points if start <= point["date"] <= end]


def _window(points, window):
    if not window:
        return []
    return _in_window(points, window["start"], window["end"])


def _range_readings(series, start, end):
    return [
        point
        for metric, points in series.items()
        if metric != PANEL
        for point in _in_window(points, start, end)
    ]


def _last_import(owner):
    moment = (
        ImportRun.objects.filter(owner=owner)
        .order_by("-imported_at", "-id")
        .values_list("imported_at", flat=True)
        .first()
    )
    return None if moment is None else timezone.localtime(moment).date()


def _pressure_text(systolic, diastolic):
    if systolic is None or diastolic is None:
        return "—"
    return f"{num(systolic, 0)}/{num(diastolic, 0)} mmHg"


def _latest_pair(owner):
    at = (
        Measurement.objects.filter(owner=owner, metric__in=PRESSURE_METRICS, value__isnull=False)
        .values("at")
        .annotate(count=Count("metric"))
        .filter(count=len(PRESSURE_METRICS))
        .order_by("-at")
        .values_list("at", flat=True)
        .first()
    )
    if at is None:
        return None
    values = dict(
        Measurement.objects.filter(owner=owner, at=at, metric__in=PRESSURE_METRICS).values_list(
            "metric", "value"
        )
    )
    return values["systolic_bp"], values["diastolic_bp"]


def _latest_weight(owner):
    return (
        Measurement.objects.filter(owner=owner, metric="body_weight", value__isnull=False)
        .order_by("-at", "-id")
        .values_list("value", flat=True)
        .first()
    )


def _metric_value_text(unit, value):
    text = num(value, UNIT_PRECISION.get(unit, 1))
    return f"{text}{axis_unit(unit)}"


def _kg_text(value):
    return _metric_value_text("kg", value)


def _pct_text(value):
    return _metric_value_text("%", value)


def _day_ticks(start, end, maximum=4, multi_year=False):
    span = (end - start).days
    ticks = []
    for index in thin_indexes(span + 1, maximum):
        day = start + timedelta(days=index)
        ticks.append((index, _tick_bucket_label(day, "day", multi_year)))
    return ticks


def _rolling_average(points):
    averages = {}
    for point in points:
        day = point["date"]
        window = [
            other["value"]
            for other in points
            if day - timedelta(days=SEVEN_DAYS - 1) <= other["date"] <= day
        ]
        if window:
            averages[day] = sum(window) / len(window)
    return averages


def _reference_tone(value):
    for band in REFERENCE_BANDS["blood_pressure"]:
        if band["from"] <= value and (band["to"] is None or value < band["to"]):
            return band["tone"]
    return "cat-ok"


def _diastolic_tone(value):
    if value >= 90:
        return "cat-danger"
    if value >= 80:
        return "cat-stage1"
    return "cat-ok"


def _category_legend():
    legend = [{"label": "Normal <120 mmHg", "class": "cat-ok"}]
    for band in REFERENCE_BANDS["blood_pressure"]:
        legend.append(
            {
                "label": f"{band['short']} mmHg",
                "class": band["tone"],
            }
        )
    return legend


def _bp_category(systolic, diastolic):
    def rank_systolic(value):
        if value > 180:
            return 4
        if value >= 140:
            return 3
        if value >= 130:
            return 2
        if value >= 120:
            return 1
        return 0

    def rank_diastolic(value):
        if value > 120:
            return 4
        if value >= 90:
            return 3
        if value >= 80:
            return 2
        return 0

    rank = max(rank_systolic(systolic), rank_diastolic(diastolic))
    if rank == 4:
        return {"text": "Hypertensive crisis", "class": "band-danger"}
    if rank == 3:
        return {"text": "Stage 2", "class": "band-danger"}
    if rank == 2:
        return {"text": "Stage 1", "class": "band-warn"}
    if rank == 1:
        return {"text": "Elevated", "class": "band-warn"}
    return {"text": "Normal", "class": "band-ok"}


def _kpi_cards(owner, series, resolved, last_import):
    start = resolved["start"]
    end = resolved["end"]
    previous = resolved["previous"]
    if previous:
        versus = RANGE_VERSUS.get(resolved["key"], "the previous period")
    else:
        versus = "the previous period"

    pair = _latest_pair(owner)
    latest_text = "—" if pair is None else _pressure_text(*pair)

    seven_start = end - timedelta(days=SEVEN_DAYS - 1)
    seven_systolic = [
        point["value"]
        for point in series.get("systolic_bp", [])
        if seven_start <= point["date"] <= end
    ]
    seven_diastolic = [
        point["value"]
        for point in series.get("diastolic_bp", [])
        if seven_start <= point["date"] <= end
    ]
    seven_text = _pressure_text(_mean(seven_systolic), _mean(seven_diastolic))
    seven_sub = None
    seven_delta = None
    if seven_systolic:
        seven_sub = (
            f"based on {num(len(seven_systolic))} reading{'' if len(seven_systolic) == 1 else 's'}"
        )
    if previous:
        previous_seven_end = previous["end"]
        previous_seven_start = previous_seven_end - timedelta(days=SEVEN_DAYS - 1)
        previous_seven_systolic = _mean(
            point["value"]
            for point in series.get("systolic_bp", [])
            if previous_seven_start <= point["date"] <= previous_seven_end
        )
        seven_delta = delta(
            _mean(seven_systolic),
            previous_seven_systolic,
            absolute=True,
            suffix=" mmHg",
            precision=0,
            harm="up",
            comparison=versus,
        )

    weights = _in_window(series.get("body_weight", []), start, end)
    latest_weight = _latest_weight(owner)
    latest_weight_text = "—" if latest_weight is None else _kg_text(latest_weight)
    previous_weight = _mean(
        point["value"] for point in _window(series.get("body_weight", []), previous)
    )

    change_text = "—"
    change_delta = None
    if len(weights) >= 2:
        first = weights[0]
        last = weights[-1]
        change_text = f"{_kg_text(first['value'])} → {_kg_text(last['value'])}"
        change_delta = delta(
            last["value"],
            first["value"],
            absolute=True,
            suffix=" kg",
            precision=1,
            harm="none",
            comparison="the first reading in the range",
        )

    readings = len(_range_readings(series, start, end))
    previous_readings = (
        len(_range_readings(series, previous["start"], previous["end"])) if previous else None
    )
    weight_mean = _mean(point["value"] for point in weights)

    return [
        {
            "key": "blood-pressure",
            "label": "Latest blood pressure",
            "value": latest_text,
            "delta": None,
        },
        {
            "key": "seven-day",
            "label": "7-day average",
            "value": seven_text,
            "sub": seven_sub,
            "delta": seven_delta,
        },
        {
            "key": "weight",
            "label": "Latest weight",
            "value": latest_weight_text,
            "delta": delta(
                weight_mean,
                previous_weight,
                absolute=True,
                suffix=" kg",
                precision=1,
                harm="none",
                comparison=versus,
            ),
        },
        {
            "key": "weight-change",
            "label": "Weight change",
            "value": change_text,
            "delta": change_delta,
        },
        {
            "key": "readings",
            "label": "Readings",
            "value": num(readings),
            "delta": delta(readings, previous_readings, harm="none", comparison=versus),
        },
        {
            "key": "last-import",
            "label": "Last import",
            "value": "—" if last_import is None else last_import.isoformat(),
            "delta": None,
        },
    ]


def _blood_pressure_card(series, resolved):
    start = resolved["start"]
    end = resolved["end"]
    multi_year = start.year != end.year
    x_max = (end - start).days
    x_ticks = _day_ticks(start, end, multi_year=multi_year)
    systolic = _in_window(series.get("systolic_bp", []), start, end)
    diastolic = _in_window(series.get("diastolic_bp", []), start, end)
    pulse = _in_window(series.get("pulse", []), start, end)

    def points_of(readings, unit, label, tone_of=None):
        points = []
        for point in readings:
            tone = tone_of(point["value"]) if tone_of else ""
            title = f"{point['stamp']} — {label} {_metric_value_text(unit, point['value'])}"
            points.append(((point["date"] - start).days, point["value"], tone, title))
        return points

    systolic_average = _rolling_average(systolic)
    diastolic_average = _rolling_average(diastolic)
    systolic_marks = [
        (
            (point["date"] - start).days,
            point["value"],
            f"Systolic {_metric_value_text('mmHg', point['value'])} on {point['stamp']}",
        )
        for point in systolic
        if point["value"] >= 140
    ]
    diastolic_marks = [
        (
            (point["date"] - start).days,
            point["value"],
            f"Diastolic {_metric_value_text('mmHg', point['value'])} on {point['stamp']}",
        )
        for point in diastolic
        if point["value"] >= 90
    ]
    chart = line_chart(
        [
            {
                "label": "Systolic",
                "points": points_of(systolic, "mmHg", "Systolic", _reference_tone),
                "dots_all": True,
                "marks": systolic_marks,
            },
            {
                "label": "Diastolic",
                "points": points_of(diastolic, "mmHg", "Diastolic", _diastolic_tone),
                "dots_all": True,
                "marks": diastolic_marks,
            },
            {
                "label": "Systolic 7-day average",
                "points": [
                    ((day - start).days, value) for day, value in sorted(systolic_average.items())
                ],
                "class": "series-1 avg",
            },
            {
                "label": "Diastolic 7-day average",
                "points": [
                    ((day - start).days, value) for day, value in sorted(diastolic_average.items())
                ],
                "class": "series-2 avg",
            },
        ],
        x_max=x_max,
        x_ticks=x_ticks,
        y_unit="mmHg",
        max_gap=SEVEN_DAYS,
        height=TALL_CHART_HEIGHT,
        aria_label=f"Systolic and diastolic blood pressure from {resolved['label']}",
    )
    pulse_chart = line_chart(
        [{"label": "Pulse", "points": points_of(pulse, "/min", "Pulse"), "dots_all": True}],
        x_max=x_max,
        x_ticks=x_ticks,
        y_unit="/min",
        max_gap=SEVEN_DAYS,
        height=TALL_CHART_HEIGHT,
        aria_label=f"Pulse from {resolved['label']}",
    )
    legend = [
        {"label": line["label"], "class": line["class"], "unit": "mmHg"} for line in chart["lines"]
    ]
    pulse_legend = [
        {"label": line["label"], "class": line["class"], "unit": "/min"}
        for line in pulse_chart["lines"]
    ]

    moments = {}
    for code, key in (
        ("systolic_bp", "systolic"),
        ("diastolic_bp", "diastolic"),
        ("pulse", "pulse"),
    ):
        for point in _in_window(series.get(code, []), start, end):
            row = moments.setdefault(
                point["at"],
                {
                    "date": point["stamp"],
                    "systolic": None,
                    "diastolic": None,
                    "pulse": None,
                    "note": "",
                },
            )
            row[key] = point["value"]
            if point["note"] and not row["note"]:
                row["note"] = point["note"]
    rows_all = []
    latest_text = None
    category = None
    for moment in sorted(moments):
        row = moments[moment]
        rows_all.append(
            {
                "date": row["date"],
                "systolic": "—" if row["systolic"] is None else num(row["systolic"], 0),
                "diastolic": "—" if row["diastolic"] is None else num(row["diastolic"], 0),
                "pulse": "—" if row["pulse"] is None else num(row["pulse"]),
                "note": row["note"],
            }
        )
        if row["systolic"] is not None and row["diastolic"] is not None:
            latest_text = _pressure_text(row["systolic"], row["diastolic"])
            category = _bp_category(row["systolic"], row["diastolic"])
    return {
        "chart": chart,
        "pulse_chart": pulse_chart,
        "legend": legend,
        "pulse_legend": pulse_legend,
        "category_legend": _category_legend(),
        "rows": rows_all[-CARD_ROW_LIMIT:],
        "rows_total": len(rows_all),
        "rows_url": filtered_url(reverse("readings"), {"metric": PANEL}),
        "has_points": bool(rows_all),
        "latest": latest_text,
        "category": category,
    }


def _weight_card(series, resolved):
    start = resolved["start"]
    end = resolved["end"]
    points = _in_window(series.get("body_weight", []), start, end)
    averages = _rolling_average(points)
    latest_title = None
    if points:
        last = points[-1]
        latest_title = f"Latest {_kg_text(last['value'])} on {_short_date(last['date'])}"
    chart = line_chart(
        [
            {
                "label": "Daily weight",
                "points": [
                    (
                        (point["date"] - start).days,
                        point["value"],
                        "",
                        f"{point['stamp']} — {_kg_text(point['value'])}",
                    )
                    for point in points
                ],
                "dots_all": True,
                "latest_title": latest_title,
            },
            {
                "label": "7-day average",
                "points": [((day - start).days, value) for day, value in sorted(averages.items())],
                "class": "series-2 avg",
            },
        ],
        x_max=(end - start).days,
        x_ticks=_day_ticks(start, end, multi_year=start.year != end.year),
        y_unit="kg",
        max_gap=SEVEN_DAYS,
        height=TALL_CHART_HEIGHT,
        aria_label=f"Daily weight and 7-day average from {resolved['label']}",
    )
    rows_all = [
        {
            "date": point["stamp"],
            "value": _kg_text(point["value"]),
            "average": "—" if point["date"] not in averages else _kg_text(averages[point["date"]]),
            "note": point["note"],
        }
        for point in points
    ]
    return {
        "chart": chart,
        "legend": [
            {"label": line["label"], "class": line["class"], "unit": "kg"}
            for line in chart["lines"]
        ],
        "rows": rows_all[-CARD_ROW_LIMIT:],
        "rows_total": len(rows_all),
        "rows_url": filtered_url(reverse("readings"), {"metric": "body_weight"}),
        "has_points": bool(points),
        "has_average": any("avg" in line["class"] for line in chart["lines"]),
        "readings": len(points),
    }


def _combined_composition_card(series, metric, resolved):
    start = resolved["start"]
    end = resolved["end"]
    domains = [METRIC_DOMAINS[code] for code in COMBINED_COMPOSITION]
    readings = {code: _in_window(series.get(code, []), start, end) for code in COMBINED_COMPOSITION}
    lines = [
        {
            "label": METRIC_LABELS[code],
            "points": [
                (
                    (point["date"] - start).days,
                    point["value"],
                    "",
                    f"{point['stamp']} — {METRIC_LABELS[code]} {_pct_text(point['value'])}",
                )
                for point in readings[code]
            ],
            "dots_all": True,
            "class": "series-1" if code == COMBINED_COMPOSITION[0] else "series-4",
        }
        for code in COMBINED_COMPOSITION
    ]
    lines += [
        {
            "label": f"{METRIC_LABELS[code]} 7-day average",
            "points": [
                ((day - start).days, value)
                for day, value in sorted(_rolling_average(readings[code]).items())
            ],
            "class": ("series-1" if code == COMBINED_COMPOSITION[0] else "series-4") + " avg",
        }
        for code in COMBINED_COMPOSITION
    ]
    chart = line_chart(
        lines,
        x_max=(end - start).days,
        x_ticks=_day_ticks(start, end, multi_year=start.year != end.year),
        y_unit="%",
        y_min=min(domain[0] for domain in domains),
        y_max=max(domain[1] for domain in domains),
        max_gap=SEVEN_DAYS,
        height=TALL_CHART_HEIGHT,
        aria_label=f"Body fat and skeletal muscle, both in percent, from {resolved['label']}",
    )
    moments = {}
    for code in COMBINED_COMPOSITION:
        for point in _in_window(series.get(code, []), start, end):
            row = moments.setdefault(
                point["at"],
                {
                    "date": point["stamp"],
                    "body_fat_pct": None,
                    "skeletal_muscle_pct": None,
                    "note": "",
                },
            )
            row[code] = point["value"]
            if point["note"] and not row["note"]:
                row["note"] = point["note"]
    rows_all = [
        {
            "date": row["date"],
            "body_fat_pct": "—" if row["body_fat_pct"] is None else _pct_text(row["body_fat_pct"]),
            "skeletal_muscle_pct": (
                "—" if row["skeletal_muscle_pct"] is None else _pct_text(row["skeletal_muscle_pct"])
            ),
            "note": row["note"],
        }
        for row in (moments[moment] for moment in sorted(moments))
    ]
    return {
        "metric": metric,
        "combined": True,
        "label": "Body fat & skeletal muscle",
        "unit": "%",
        "chart": chart,
        "legend": [
            {"label": line["label"], "class": line["class"], "unit": "%"} for line in chart["lines"]
        ],
        "note": (
            "Body fat and skeletal muscle are both percentages, so they share one axis; "
            "a percentage never shares an axis with a kilogram."
        ),
        "links": [
            {
                "label": f"All {METRIC_LABELS[code].lower()} readings",
                "url": filtered_url(
                    reverse("dashboard_metric", kwargs={"metric": code}),
                    _range_query(resolved["key"], resolved["start"], resolved["end"]),
                ),
            }
            for code in COMBINED_COMPOSITION
        ],
        "rows": rows_all[-CARD_ROW_LIMIT:],
        "rows_total": len(rows_all),
        "rows_url": filtered_url(
            reverse("dashboard_metric", kwargs={"metric": metric}),
            _range_query(resolved["key"], resolved["start"], resolved["end"]),
        ),
        "has_points": bool(rows_all),
        "readings": len(rows_all),
    }


def _composition_card(series, metric, resolved):
    if metric in COMBINED_COMPOSITION:
        return _combined_composition_card(series, metric, resolved)
    start = resolved["start"]
    end = resolved["end"]
    label = METRIC_LABELS.get(metric, metric)
    unit = METRICS[metric].unit
    domain = METRIC_DOMAINS.get(metric)
    readings = _in_window(series.get(metric, []), start, end)
    averages = _rolling_average(readings)
    lines = [
        {
            "label": label,
            "points": [
                (
                    (point["date"] - start).days,
                    point["value"],
                    "",
                    f"{point['stamp']} — {label} {_metric_value_text(unit, point['value'])}",
                )
                for point in readings
            ],
            "dots_all": True,
        },
        {
            "label": f"{label} 7-day average",
            "points": [((day - start).days, value) for day, value in sorted(averages.items())],
            "class": "series-2 avg",
        },
    ]
    chart = line_chart(
        lines,
        x_max=(end - start).days,
        x_ticks=_day_ticks(start, end, multi_year=start.year != end.year),
        y_unit=unit,
        y_min=domain[0] if domain else None,
        y_max=domain[1] if domain else None,
        max_gap=SEVEN_DAYS,
        height=TALL_CHART_HEIGHT,
        aria_label=f"{label} from {resolved['label']}",
    )
    rows_all = [
        {
            "date": point["stamp"],
            "value": _metric_value_text(unit, point["value"]),
            "note": point["note"],
        }
        for point in readings
    ]
    return {
        "metric": metric,
        "combined": False,
        "label": label,
        "unit": unit,
        "chart": chart,
        "legend": [
            {"label": line["label"], "class": line["class"], "unit": unit}
            for line in chart["lines"]
        ],
        "note": f"{label} only, so a percentage never shares an axis with a kilogram.",
        "links": [],
        "rows": rows_all[-CARD_ROW_LIMIT:],
        "rows_total": len(rows_all),
        "rows_url": filtered_url(
            reverse("dashboard_metric", kwargs={"metric": metric}),
            _range_query(resolved["key"], resolved["start"], resolved["end"]),
        ),
        "has_points": bool(readings),
        "readings": len(readings),
    }


def _heat_level(count):
    if count <= 0:
        return 0
    return min(count, 3)


def _consistency_card(series, resolved):
    start = resolved["start"]
    end = resolved["end"]
    grid_start = start - timedelta(days=start.weekday())
    grid_end = end + timedelta(days=6 - end.weekday())
    counts = {}
    for point in _range_readings(series, start, end):
        counts[point["date"]] = counts.get(point["date"], 0) + 1
    weeks = []
    current = grid_start
    while current <= grid_end:
        cells = []
        for offset in range(7):
            day = current + timedelta(days=offset)
            active = start <= day <= end
            count = counts.get(day, 0)
            if not active:
                title = ""
            elif count:
                title = f"{_short_date(day)}: {count} reading{'' if count == 1 else 's'}"
            else:
                title = f"{_short_date(day)}: no readings"
            cells.append(
                {
                    "date": day,
                    "level": _heat_level(count) if active else None,
                    "count": count if active else None,
                    "title": title,
                    "label": _short_date(day),
                }
            )
        weeks.append({"cells": cells, "label": ""})
        current += timedelta(days=7)
    multi_year = start.year != end.year
    step = max(1, math.ceil(len(weeks) / 8))
    if len(weeks) <= 6:
        step = max(step, 2)
    for column, week in enumerate(weeks):
        if column % step == 0:
            week["label"] = _tick_bucket_label(week["cells"][0]["date"], "day", multi_year)
    heat_rows = [
        {
            "date": _short_date(day),
            "weekday": WEEKDAYS[day.weekday()],
            "readings": counts[day],
        }
        for day in sorted(counts)
        if counts[day] > 0
    ]
    return {
        "weeks": weeks,
        "heat_rows": heat_rows,
        "days": len(heat_rows),
        "readings": sum(counts.values()),
        "has_heatmap": bool(heat_rows),
        "window_label": period_label(start, end),
    }


def _reading_text(point):
    count = int(point["value"])
    return f"{count} reading{'' if count == 1 else 's'}"


def _weekly_card(series, resolved):
    start = resolved["start"]
    end = resolved["end"]
    counts = {}
    for point in _range_readings(series, start, end):
        week = _week_start(point["date"])
        counts[week] = counts.get(week, 0) + 1
    points = []
    for week in sorted(resolved["partial"]):
        info = resolved["partial"][week]
        points.append(
            {
                "date": week,
                "value": counts.get(week, 0),
                "partial": info["partial"],
                "in_progress": info["in_progress"],
                "note": info["note"],
            }
        )
    chart = bar_chart(
        points,
        bucket="week",
        multi_year=start.year != end.year,
        value_text=_reading_text,
        height=TALL_CHART_HEIGHT,
        aria_label=f"Readings per week from {resolved['label']}",
    )
    rows_all = [
        {
            "date": _full_bucket_label(point["date"], "week"),
            "text": _reading_text(point),
            "note": point["note"],
        }
        for point in points
    ]
    return {
        "chart": chart,
        "rows": rows_all[-CARD_ROW_LIMIT:],
        "rows_total": len(rows_all),
        "rows_url": reverse("readings"),
        "total": sum(counts.values()),
        "weeks": len(points),
        "has_points": bool(points),
    }


def _weekday_card(series, resolved):
    start = resolved["start"]
    end = resolved["end"]
    counts = [0] * len(WEEKDAYS)
    for point in _range_readings(series, start, end):
        counts[point["date"].weekday()] += 1
    anchor = _week_start(start)
    points = [
        {"date": anchor + timedelta(days=offset), "value": counts[offset]}
        for offset in range(len(WEEKDAYS))
    ]
    chart = bar_chart(
        points,
        bucket="day",
        value_text=_reading_text,
        label_of=lambda point: WEEKDAYS[point["date"].weekday()],
        height=TALL_CHART_HEIGHT,
        aria_label=f"Readings per weekday from {resolved['label']}",
    )
    rows_all = [
        {"date": WEEKDAYS[offset], "text": _reading_text(points[offset])}
        for offset in range(len(WEEKDAYS))
    ]
    return {
        "chart": chart,
        "rows": rows_all[-CARD_ROW_LIMIT:],
        "rows_total": len(rows_all),
        "rows_url": reverse("readings"),
        "total": sum(counts),
        "has_points": bool(sum(counts)),
    }


def _extreme(point, unit):
    return {"value": _metric_value_text(unit, point["value"]), "date": _short_date(point["date"])}


def _extremes_card(series, resolved):
    start = resolved["start"]
    end = resolved["end"]
    items = []
    for code in METRICS:
        if code == PANEL:
            continue
        points = _in_window(series.get(code, []), start, end)
        if not points:
            continue
        unit = METRICS[code].unit
        items.append(
            {
                "metric": code,
                "label": METRIC_LABELS.get(code, code),
                "unit": unit,
                "count": len(points),
                "high": _extreme(
                    max(points, key=lambda point: (point["value"], point["at"])), unit
                ),
                "low": _extreme(min(points, key=lambda point: (point["value"], point["at"])), unit),
                "latest": _extreme(points[-1], unit),
                "url": filtered_url(
                    reverse("dashboard_metric", kwargs={"metric": code}),
                    _range_query(resolved["key"], resolved["start"], resolved["end"]),
                ),
            }
        )
    return items


DASHBOARD_NAV_ITEMS = (
    ("overview", "Overview", "dashboards"),
    ("blood_pressure", "Blood pressure", "dashboard_metric"),
    ("weight", "Weight & composition", "dashboard_weight"),
    ("consistency", "Consistency", "dashboard_consistency"),
)


def _dashboard_nav(parsed, active):
    query = {
        **_range_query(parsed["range"], parsed["from"], parsed["to"]),
        "metric": parsed["metric"],
    }
    items = []
    for key, label, name in DASHBOARD_NAV_ITEMS:
        if name == "dashboard_metric":
            path = reverse("dashboard_metric", kwargs={"metric": PANEL})
        else:
            path = reverse(name)
        items.append(
            {
                "key": key,
                "label": label,
                "url": filtered_url(path, query),
                "current": key == active,
            }
        )
    return items


def _nav_active_for_metric(metric):
    if metric == PANEL:
        return "blood_pressure"
    if metric == "body_weight" or metric in COMPOSITION_METRICS:
        return "weight"
    return ""


def _toolbar_dates(resolved, today):
    start = resolved["start"] if resolved is not None else today - timedelta(days=29)
    end = resolved["end"] if resolved is not None else today
    return start.isoformat(), end.isoformat()


def _dashboard_scaffold(owner, params, today, active):
    parsed = parse_dashboard_params(params)
    earliest = _first_date(owner)
    resolved = (
        resolve_range(parsed["range"], today, earliest, parsed["from"], parsed["to"])
        if earliest is not None
        else None
    )
    custom_from, custom_to = _toolbar_dates(resolved, today)
    context = {
        "has_data": earliest is not None,
        "today": today,
        "range_key": parsed["range"],
        "metric": parsed["metric"],
        "query_metric": "" if params is None else str(params.get("metric") or "").strip(),
        "custom_from": custom_from,
        "custom_to": custom_to,
        "range_options": _options(RANGE_KEYS, RANGE_LABELS, parsed["range"]),
        "metric_options": _composition_options(parsed["metric"]),
        "dashboard_nav": _dashboard_nav(parsed, active),
        "dashboards_url": filtered_url(
            reverse("dashboards"),
            {
                **_range_query(parsed["range"], parsed["from"], parsed["to"]),
                "metric": parsed["metric"],
            },
        ),
    }
    if earliest is None:
        return context, parsed, None, None
    series = _history(owner, _fetch_window(resolved))
    start = resolved["start"]
    end = resolved["end"]
    readings = _range_readings(series, start, end)
    last_import = _last_import(owner)
    last_dates = [point["date"] for points in series.values() for point in points]
    context.update(
        {
            "range": resolved,
            "period_label": resolved["label"],
            "last_import": last_import,
            "readings_total": len(readings),
            "range_empty": not readings,
            "last_reading_days": (end - max(last_dates)).days if last_dates else None,
        }
    )
    return context, parsed, resolved, series


def _section_cards(context, blood_pressure, weight):
    nav = {item["key"]: item for item in context["dashboard_nav"]}
    kpis = {card["key"]: card for card in context["kpis"]}
    if blood_pressure["latest"]:
        bp_summary = f"Latest {blood_pressure['latest']}"
        bp_extra = f"AHA category: {blood_pressure['category']['text']}"
        bp_extra_class = blood_pressure["category"]["class"]
    else:
        bp_summary = f"No blood pressure readings in {context['period_label']}"
        bp_extra = ""
        bp_extra_class = ""
    if weight["has_points"]:
        weight_summary = f"Latest {weight['rows'][-1]['value']}"
    else:
        weight_summary = f"No weight readings in {context['period_label']}"
    return [
        {
            "key": "blood_pressure",
            "label": "Blood pressure",
            "url": nav["blood_pressure"]["url"],
            "blurb": (
                "Systolic and diastolic with dots coloured by AHA category and outlier rings, "
                "pulse on its own scale, and the readings behind them."
            ),
            "summary": bp_summary,
            "extra": bp_extra,
            "extra_class": bp_extra_class,
            "cta": "Open the blood-pressure page",
        },
        {
            "key": "weight",
            "label": "Weight & composition",
            "url": nav["weight"]["url"],
            "blurb": (
                "Daily weigh-ins with the 7-day rolling average, "
                "plus body fat and skeletal muscle on one chart, "
                "or one other composition metric at a time."
            ),
            "summary": weight_summary,
            "delta": kpis["weight"]["delta"] if weight["has_points"] else None,
            "cta": "Open weight & composition",
        },
        {
            "key": "consistency",
            "label": "Consistency",
            "url": nav["consistency"]["url"],
            "blurb": "A year heatmap of reading days and the readings per week.",
            "summary": f"{num(context['readings_total'])} readings in {context['period_label']}",
            "cta": "Open consistency",
        },
    ]


def build_dashboards(owner, params, today):
    context, parsed, resolved, series = _dashboard_scaffold(owner, params, today, "overview")
    if resolved is None:
        return context
    context["kpis"] = _kpi_cards(owner, series, resolved, context["last_import"])
    if context["range_empty"]:
        return context
    blood_pressure = _blood_pressure_card(series, resolved)
    weight = _weight_card(series, resolved)
    context.update(
        {
            "blood_pressure": blood_pressure,
            "weight": weight,
            "composition": _composition_card(series, parsed["metric"], resolved),
            "consistency": _consistency_card(series, resolved),
            "weekly": _weekly_card(series, resolved),
            "extremes": _extremes_card(series, resolved),
            "sections": _section_cards(context, blood_pressure, weight),
        }
    )
    return context


def build_dashboard_weight(owner, params, today):
    context, parsed, resolved, series = _dashboard_scaffold(owner, params, today, "weight")
    if resolved is None:
        return context
    context.update(
        {
            "weight": _weight_card(series, resolved),
            "composition": _composition_card(series, parsed["metric"], resolved),
        }
    )
    return context


def build_dashboard_consistency(owner, params, today):
    context, parsed, resolved, series = _dashboard_scaffold(owner, params, today, "consistency")
    if resolved is None:
        return context
    context.update(
        {
            "consistency": _consistency_card(series, resolved),
            "weekly": _weekly_card(series, resolved),
            "weekday": _weekday_card(series, resolved),
        }
    )
    return context


def _build_panel_dashboard(owner, params, today):
    context, parsed, resolved, series = _dashboard_scaffold(owner, params, today, "blood_pressure")
    context.update(
        {
            "metric": PANEL,
            "metric_label": METRIC_LABELS[PANEL],
            "unit": METRICS[PANEL].unit,
        }
    )
    if resolved is None:
        return context
    card = _blood_pressure_card(series, resolved)
    context.update(
        {
            "blood_pressure": card,
            "has_points": card["has_points"],
            "range_empty": not card["has_points"],
            "readings_total": card["rows_total"],
        }
    )
    return context


def build_metric_dashboard(owner, metric, params, today):
    if metric not in METRICS:
        return None
    if metric == PANEL:
        return _build_panel_dashboard(owner, params, today)
    parsed = parse_dashboard_params(params)
    earliest = _first_date(owner)
    resolved = resolve_range(parsed["range"], today, earliest, parsed["from"], parsed["to"])
    start = resolved["start"]
    end = resolved["end"]
    points = _in_window(_history(owner, (start, end)).get(metric, []), start, end)
    label = METRIC_LABELS.get(metric, metric)
    unit = METRICS[metric].unit
    domain = METRIC_DOMAINS.get(metric)
    averages = _rolling_average(points) if metric == "body_weight" else {}
    lines = []
    if metric == "body_weight":
        latest_title = None
        if points:
            last = points[-1]
            latest_title = f"Latest {_kg_text(last['value'])} on {_short_date(last['date'])}"
        lines.append(
            {
                "label": "Daily weight",
                "points": [
                    (
                        (point["date"] - start).days,
                        point["value"],
                        "",
                        f"{point['stamp']} — {_kg_text(point['value'])}",
                    )
                    for point in points
                ],
                "dots_all": True,
                "latest_title": latest_title,
            }
        )
        if averages:
            lines.append(
                {
                    "label": "7-day average",
                    "points": [
                        ((day - start).days, value) for day, value in sorted(averages.items())
                    ],
                    "class": "series-2 avg",
                }
            )
    else:
        lines.append(
            {
                "label": label,
                "points": [
                    (
                        (point["date"] - start).days,
                        point["value"],
                        "",
                        f"{point['stamp']} — {label} {_metric_value_text(unit, point['value'])}",
                    )
                    for point in points
                ],
            }
        )
    chart = line_chart(
        lines,
        x_max=(end - start).days,
        x_ticks=_day_ticks(start, end, multi_year=start.year != end.year),
        y_unit=unit,
        y_min=domain[0] if domain else None,
        y_max=domain[1] if domain else None,
        max_gap=SEVEN_DAYS,
        height=TALL_CHART_HEIGHT,
        aria_label=f"{label} from {resolved['label']}",
    )
    rows = []
    for point in reversed(points):
        row = {
            "date": point["stamp"],
            "value": _metric_value_text(unit, point["value"]),
            "note": point["note"],
        }
        if metric == "body_weight":
            row["average"] = (
                "—" if point["date"] not in averages else _kg_text(averages[point["date"]])
            )
        rows.append(row)
    custom_from, custom_to = _toolbar_dates(resolved, today)
    return {
        "has_data": earliest is not None,
        "metric": metric,
        "metric_label": label,
        "unit": unit,
        "range_key": parsed["range"],
        "query_metric": "" if params is None else str(params.get("metric") or "").strip(),
        "custom_from": custom_from,
        "custom_to": custom_to,
        "range_options": _options(RANGE_KEYS, RANGE_LABELS, parsed["range"]),
        "metric_options": _composition_options(parsed["metric"]),
        "dashboard_nav": _dashboard_nav(parsed, _nav_active_for_metric(metric)),
        "range": resolved,
        "period_label": resolved["label"],
        "today": today,
        "dashboards_url": filtered_url(
            reverse("dashboards"),
            {
                **_range_query(parsed["range"], parsed["from"], parsed["to"]),
                "metric": parsed["metric"],
            },
        ),
        "chart": chart,
        "legend": (
            [{"label": line["label"], "class": line["class"]} for line in chart["lines"]]
            if metric == "body_weight"
            else None
        ),
        "has_points": bool(points),
        "range_empty": not points,
        "readings_total": len(points),
        "rows": rows[:METRIC_ROW_LIMIT],
        "hidden_rows": max(0, len(rows) - METRIC_ROW_LIMIT),
        "extremes": None
        if not points
        else {
            "high": _extreme(max(points, key=lambda point: (point["value"], point["at"])), unit),
            "low": _extreme(min(points, key=lambda point: (point["value"], point["at"])), unit),
            "latest": _extreme(points[-1], unit),
        },
    }
