import calendar
import math
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Min
from django.urls import reverse
from django.utils import timezone

from .models import METRICS, PANEL, ImportRun, Measurement
from .templatetags.formatting import num

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


RANGE_KEYS = ("30d", "90d", "1y", "all")
DEFAULT_RANGE = "30d"
RANGE_DAYS = {"30d": 30, "90d": 90, "1y": 365}
RANGE_LABELS = {"30d": "30 days", "90d": "90 days", "1y": "1 year", "all": "All time"}
COMPOSITION_METRICS = (
    "body_fat_pct",
    "skeletal_muscle_pct",
    "visceral_fat",
    "resting_metabolism",
    "impedance",
)
DEFAULT_COMPOSITION = COMPOSITION_METRICS[0]
HEATMAP_DAYS = 364
MONTH_LABEL_GAP = 4
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MOVING_AVERAGE_WEEKS = 4
SEVEN_DAYS = 7
METRIC_ROW_LIMIT = 500
PRESSURE_UNITS = {"Systolic": "mmHg", "Diastolic": "mmHg", "Pulse": "/min"}
PRESSURE_METRICS = ("systolic_bp", "diastolic_bp")

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
    "fat_free_mass": "Fat-free mass",
    "average_weight": "Average weight",
    "average_fat_free_mass": "Average fat-free mass",
    "body_temperature": "Body temperature",
}


def _param(params, key, allowed, default):
    value = "" if params is None else str(params.get(key) or "").strip()
    return value if value in allowed else default


def parse_dashboard_params(params):
    return {
        "range": _param(params, "range", RANGE_KEYS, DEFAULT_RANGE),
        "metric": _param(params, "metric", COMPOSITION_METRICS, DEFAULT_COMPOSITION),
    }


def _options(keys, labels, current):
    return [{"key": key, "label": labels.get(key, key), "current": key == current} for key in keys]


def period_label(start, end):
    left = f"{start.day} {MONTH_ABBR[start.month - 1]}"
    right = f"{end.day} {MONTH_ABBR[end.month - 1]}"
    if start == end:
        return f"{left} {end.year}"
    if start.year == end.year:
        return f"{left} – {right} {end.year}"
    return f"{left} {start.year} – {right} {end.year}"


def resolve_range(key, today, earliest):
    if key not in RANGE_KEYS:
        key = DEFAULT_RANGE
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
        "partial": partial_buckets(start, today, "week"),
    }


def filtered_url(path, query):
    return f"{path}?{urlencode({key: str(value) for key, value in query.items() if value})}"


def delta(current, previous, *, absolute=False, suffix="", comparison=""):
    if current is None or previous is None:
        return None
    if absolute:
        change = current - previous
        if change > 0:
            text = f"+{change:.1f}{suffix}"
            direction = "up"
            spoken = f"up {abs(change):.1f}{suffix}"
        elif change < 0:
            text = f"−{abs(change):.1f}{suffix}"
            direction = "down"
            spoken = f"down {abs(change):.1f}{suffix}"
        else:
            text = "—"
            direction = "flat"
            spoken = "unchanged"
        return {"text": text, "class": direction, "aria": f"{spoken} versus {comparison}"}
    if previous == 0:
        return {
            "text": "—",
            "class": "flat",
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
    return {"text": text, "class": direction, "aria": f"{spoken} versus {comparison}"}


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
    return f"{num(systolic)}/{num(diastolic)} mmHg"


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


def _axis_unit(unit):
    if not unit:
        return ""
    return unit if unit == "%" else f" {unit}"


def _day_ticks(start, end, maximum=4):
    span = (end - start).days
    ticks = []
    for index in thin_indexes(span + 1, maximum):
        day = start + timedelta(days=index)
        ticks.append((index, f"{day.day} {MONTH_ABBR[day.month - 1]}"))
    return ticks


def _kpi_cards(owner, series, resolved, last_import):
    start = resolved["start"]
    end = resolved["end"]
    previous = resolved["previous"]
    versus = f"the previous {RANGE_LABELS[resolved['key']]}" if previous else "the previous period"

    pair = _latest_pair(owner)
    latest_text = "—" if pair is None else _pressure_text(*pair)

    seven_start = end - timedelta(days=SEVEN_DAYS - 1)
    seven_text = _pressure_text(
        _mean(
            point["value"]
            for point in series.get("systolic_bp", [])
            if seven_start <= point["date"] <= end
        ),
        _mean(
            point["value"]
            for point in series.get("diastolic_bp", [])
            if seven_start <= point["date"] <= end
        ),
    )

    weights = _in_window(series.get("body_weight", []), start, end)
    latest_weight = _latest_weight(owner)
    latest_weight_text = "—" if latest_weight is None else f"{num(latest_weight)} kg"
    previous_weight = _mean(
        point["value"] for point in _window(series.get("body_weight", []), previous)
    )
    systolic_in_range = _in_window(series.get("systolic_bp", []), start, end)
    previous_systolic = _mean(
        point["value"] for point in _window(series.get("systolic_bp", []), previous)
    )

    change_text = "—"
    change_delta = None
    if len(weights) >= 2:
        first = weights[0]
        last = weights[-1]
        change_text = f"{num(first['value'])} → {num(last['value'])} kg"
        change_delta = delta(
            last["value"],
            first["value"],
            absolute=True,
            suffix=" kg",
            comparison="the first reading in the range",
        )

    readings = len(_range_readings(series, start, end))
    previous_readings = (
        len(_range_readings(series, previous["start"], previous["end"])) if previous else None
    )
    weight_mean = _mean(point["value"] for point in weights)
    systolic_mean = _mean(point["value"] for point in systolic_in_range)

    return [
        {
            "key": "blood-pressure",
            "label": "Latest blood pressure",
            "value": latest_text,
            "delta": delta(systolic_mean, previous_systolic, comparison=versus),
        },
        {"key": "seven-day", "label": "7-day average", "value": seven_text, "delta": None},
        {
            "key": "weight",
            "label": "Latest weight",
            "value": latest_weight_text,
            "delta": delta(weight_mean, previous_weight, comparison=versus),
        },
        {
            "key": "weight-change",
            "label": "Weight change",
            "value": change_text,
            "delta": change_delta,
        },
        {
            "key": "readings",
            "label": "Readings in range",
            "value": num(readings),
            "delta": delta(readings, previous_readings, comparison=versus),
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
    lines = []
    for code, label in (
        ("systolic_bp", "Systolic"),
        ("diastolic_bp", "Diastolic"),
        ("pulse", "Pulse"),
    ):
        points = _in_window(series.get(code, []), start, end)
        lines.append(
            {
                "label": label,
                "points": [((point["date"] - start).days, point["value"]) for point in points],
            }
        )
    moments = {}
    for code, key in (
        ("systolic_bp", "systolic"),
        ("diastolic_bp", "diastolic"),
        ("pulse", "pulse"),
    ):
        for point in _in_window(series.get(code, []), start, end):
            row = moments.setdefault(
                point["at"],
                {"date": point["stamp"], "systolic": "—", "diastolic": "—", "pulse": "—"},
            )
            row[key] = num(point["value"])
    rows = [moments[moment] for moment in sorted(moments)]
    chart = line_chart(
        lines,
        x_max=(end - start).days,
        x_ticks=_day_ticks(start, end),
        y_unit="",
        aria_label=f"Systolic, diastolic and pulse from {resolved['label']}",
    )
    legend = [
        {"label": line["label"], "class": line["class"], "unit": PRESSURE_UNITS[line["label"]]}
        for line in chart["lines"]
    ]
    return {"chart": chart, "legend": legend, "rows": rows, "has_points": bool(rows)}


def _weight_card(series, resolved):
    start = resolved["start"]
    end = resolved["end"]
    buckets = {}
    for point in _in_window(series.get("body_weight", []), start, end):
        buckets.setdefault(_week_start(point["date"]), []).append(point["value"])
    points = []
    for week in sorted(buckets):
        info = resolved["partial"].get(week, {})
        values = buckets[week]
        points.append(
            {
                "date": week,
                "value": round(sum(values) / len(values), 2),
                "moving_average": None,
                "partial": info.get("partial", False),
                "in_progress": info.get("in_progress", False),
                "note": info.get("note", ""),
            }
        )
    for index, point in enumerate(points):
        if index + 1 >= MOVING_AVERAGE_WEEKS:
            window = points[index - MOVING_AVERAGE_WEEKS + 1 : index + 1]
            total = sum(item["value"] for item in window)
            point["moving_average"] = round(total / len(window), 2)
    chart = bar_chart(
        points,
        unit=" kg",
        bucket="week",
        multi_year=start.year != end.year,
        value_text=lambda point: f"{point['value']:.1f} kg",
        aria_label=f"Weight per week from {resolved['label']}",
    )
    rows = [
        {
            "date": _full_bucket_label(point["date"], "week"),
            "text": f"{num(point['value'])} kg",
            "note": point["note"],
        }
        for point in points
    ]
    return {
        "chart": chart,
        "rows": rows,
        "has_points": bool(points),
        "has_line": chart["has_line"],
        "window": MOVING_AVERAGE_WEEKS,
        "weeks": len(points),
    }


def _composition_card(series, metric, resolved):
    start = resolved["start"]
    end = resolved["end"]
    label = METRIC_LABELS.get(metric, metric)
    unit = METRICS[metric].unit
    readings = _in_window(series.get(metric, []), start, end)
    lines = [
        {
            "label": label,
            "points": [((point["date"] - start).days, point["value"]) for point in readings],
        }
    ]
    chart = line_chart(
        lines,
        x_max=(end - start).days,
        x_ticks=_day_ticks(start, end),
        y_unit=_axis_unit(unit),
        aria_label=f"{label} from {resolved['label']}",
    )
    rows = [
        {"date": point["stamp"], "value": num(point["value"]), "note": point["note"]}
        for point in readings
    ]
    return {
        "metric": metric,
        "label": label,
        "unit": unit,
        "chart": chart,
        "rows": rows,
        "has_points": bool(readings),
        "readings": len(readings),
    }


def _heat_level(count):
    if count <= 0:
        return 0
    return min(count, 3)


def _consistency_card(series, resolved):
    end = resolved["end"]
    heat_start = end - timedelta(days=HEATMAP_DAYS)
    grid_start = heat_start - timedelta(days=heat_start.weekday())
    grid_end = end + timedelta(days=6 - end.weekday())
    counts = {}
    for point in _range_readings(series, heat_start, end):
        counts[point["date"]] = counts.get(point["date"], 0) + 1
    weeks = []
    month_label = None
    last_label_column = None
    current = grid_start
    while current <= grid_end:
        column = (current - grid_start).days // 7
        cells = []
        for offset in range(7):
            day = current + timedelta(days=offset)
            active = heat_start <= day <= end
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
        week_label = ""
        first = cells[0]["date"]
        if first >= heat_start:
            this_month = MONTH_ABBR[first.month - 1]
            if this_month != month_label:
                month_label = this_month
                if last_label_column is None or column - last_label_column >= MONTH_LABEL_GAP:
                    last_label_column = column
                    week_label = this_month
        weeks.append({"cells": cells, "label": week_label})
        current += timedelta(days=7)
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
        "window_label": period_label(heat_start, end),
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
        aria_label=f"Readings per week from {resolved['label']}",
    )
    rows = [
        {
            "date": _full_bucket_label(point["date"], "week"),
            "text": _reading_text(point),
            "note": point["note"],
        }
        for point in points
    ]
    return {
        "chart": chart,
        "rows": rows,
        "total": sum(counts.values()),
        "weeks": len(points),
        "has_points": bool(points),
    }


def _extreme(point):
    return {"value": num(point["value"]), "date": _short_date(point["date"])}


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
        items.append(
            {
                "metric": code,
                "label": METRIC_LABELS.get(code, code),
                "unit": METRICS[code].unit,
                "count": len(points),
                "high": _extreme(max(points, key=lambda point: (point["value"], point["at"]))),
                "low": _extreme(min(points, key=lambda point: (point["value"], point["at"]))),
                "latest": _extreme(points[-1]),
                "url": filtered_url(
                    reverse("dashboard_metric", kwargs={"metric": code}),
                    {"range": resolved["key"]},
                ),
            }
        )
    return items


def build_dashboards(owner, params, today):
    parsed = parse_dashboard_params(params)
    earliest = _first_date(owner)
    context = {
        "has_data": earliest is not None,
        "today": today,
        "range_key": parsed["range"],
        "metric": parsed["metric"],
        "range_options": _options(RANGE_KEYS, RANGE_LABELS, parsed["range"]),
        "metric_options": _options(COMPOSITION_METRICS, METRIC_LABELS, parsed["metric"]),
    }
    if earliest is None:
        return context

    resolved = resolve_range(parsed["range"], today, earliest)
    series = _history(owner, _fetch_window(resolved))
    start = resolved["start"]
    end = resolved["end"]
    readings = _range_readings(series, start, end)
    last_import = _last_import(owner)
    context.update(
        {
            "range": resolved,
            "period_label": resolved["label"],
            "last_import": last_import,
            "readings_total": len(readings),
            "range_empty": not readings,
            "kpis": _kpi_cards(owner, series, resolved, last_import),
        }
    )
    if not readings:
        return context

    context.update(
        {
            "blood_pressure": _blood_pressure_card(series, resolved),
            "weight": _weight_card(series, resolved),
            "composition": _composition_card(series, parsed["metric"], resolved),
            "consistency": _consistency_card(series, resolved),
            "weekly": _weekly_card(series, resolved),
            "extremes": _extremes_card(series, resolved),
        }
    )
    return context


def build_metric_dashboard(owner, metric, params, today):
    if metric not in METRICS:
        return None
    parsed = parse_dashboard_params(params)
    earliest = _first_date(owner)
    resolved = resolve_range(parsed["range"], today, earliest)
    start = resolved["start"]
    end = resolved["end"]
    points = _in_window(_history(owner, (start, end)).get(metric, []), start, end)
    label = METRIC_LABELS.get(metric, metric)
    unit = METRICS[metric].unit
    chart = line_chart(
        [
            {
                "label": label,
                "points": [((point["date"] - start).days, point["value"]) for point in points],
            }
        ],
        x_max=(end - start).days,
        x_ticks=_day_ticks(start, end),
        y_unit=_axis_unit(unit),
        aria_label=f"{label} from {resolved['label']}",
    )
    rows = [
        {
            "date": point["stamp"],
            "value": num(point["value"]),
            "unit": unit,
            "note": point["note"],
        }
        for point in reversed(points)
    ]
    return {
        "has_data": earliest is not None,
        "metric": metric,
        "metric_label": label,
        "unit": unit,
        "range_key": parsed["range"],
        "range_options": _options(RANGE_KEYS, RANGE_LABELS, parsed["range"]),
        "range": resolved,
        "period_label": resolved["label"],
        "today": today,
        "dashboards_url": filtered_url(
            reverse("dashboards"),
            {"range": parsed["range"], "metric": parsed["metric"]},
        ),
        "chart": chart,
        "has_points": bool(points),
        "range_empty": not points,
        "readings_total": len(points),
        "rows": rows[:METRIC_ROW_LIMIT],
        "hidden_rows": max(0, len(rows) - METRIC_ROW_LIMIT),
        "extremes": None
        if not points
        else {
            "high": _extreme(max(points, key=lambda point: (point["value"], point["at"]))),
            "low": _extreme(min(points, key=lambda point: (point["value"], point["at"]))),
            "latest": _extreme(points[-1]),
        },
    }
