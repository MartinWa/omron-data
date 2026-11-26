from datetime import date, timedelta

from django.template.loader import render_to_string

from records.reports import (
    CHART_BOTTOM,
    CHART_HEIGHT,
    CHART_LEFT,
    CHART_RIGHT,
    CHART_TOP,
    CHART_WIDTH,
    MAX_BARS,
    _plot_frame,
    _round_coord,
    axis_ticks,
    bar_chart,
    line_chart,
    nice_max,
    partial_buckets,
    thin_indexes,
)

BAR_KEYS = [
    "kind",
    "width",
    "height",
    "left",
    "right",
    "top",
    "bottom",
    "plot_bottom",
    "right_edge",
    "bars",
    "line",
    "has_line",
    "xlabels",
    "ticks",
    "aria_label",
    "empty",
]

LINE_KEYS = [
    "kind",
    "width",
    "height",
    "left",
    "right",
    "top",
    "bottom",
    "plot_bottom",
    "right_edge",
    "lines",
    "xlabels",
    "ticks",
    "aria_label",
    "empty",
]


def bar_points():
    return [
        {"date": date(2026, 1, 5), "value": 10, "partial": True, "note": "in progress"},
        {"date": date(2026, 1, 12), "value": 0},
        {"date": date(2026, 1, 19), "value": 30},
    ]


def rendered(chart):
    return render_to_string("partials/report_chart.html", {"chart": chart})


def aria_label_of(markup):
    return markup.split('aria-label="')[1].split('"')[0]


def test_nice_max_uses_the_documented_steps():
    assert nice_max(None) == 1.0
    assert nice_max(float("nan")) == 1.0
    assert nice_max(float("inf")) == 1.0
    assert nice_max(0) == 1.0
    assert nice_max(-5) == 1.0
    assert nice_max(1) == 1.0
    assert nice_max(0.5) == 0.5
    assert nice_max(18) == 20.0
    assert nice_max(92.4) == 100.0
    assert nice_max(745) == 1000.0


def test_axis_ticks_are_zero_half_and_max():
    ticks = axis_ticks(20, "t")

    assert [tick["label"] for tick in ticks] == ["0t", "10t", "20t"]
    assert [tick["value"] for tick in ticks] == [0, 10.0, 20]
    assert [tick["value"] for tick in ticks] == sorted(tick["value"] for tick in ticks)


def test_round_coord_rejects_missing_and_non_finite_values():
    assert _round_coord(None) == 0.0
    assert _round_coord(float("nan")) == 0.0
    assert _round_coord(float("inf")) == 0.0
    assert _round_coord(float("-inf")) == 0.0
    assert _round_coord(1.2345) == 1.23
    assert _round_coord(7) == 7.0
    assert isinstance(_round_coord(7), float)


def test_plot_frame_subtracts_the_padding():
    plot_width, plot_height = _plot_frame()

    assert plot_width == CHART_WIDTH - CHART_LEFT - CHART_RIGHT
    assert plot_height == CHART_HEIGHT - CHART_TOP - CHART_BOTTOM


def test_thin_indexes_keeps_every_slot_until_the_limit_then_spreads():
    assert thin_indexes(0) == []
    assert thin_indexes(MAX_BARS) == list(range(MAX_BARS))

    indexes = thin_indexes(30)

    assert len(indexes) == MAX_BARS
    assert indexes == sorted(set(indexes))
    assert indexes[0] == 0
    assert indexes[-1] == 29


def test_partial_buckets_mark_the_leading_and_trailing_edges():
    start = date(2026, 1, 7)
    end = date(2026, 2, 3)
    info = partial_buckets(start, end, "week")

    first = info[date(2026, 1, 5)]
    assert first["partial"] is True
    assert first["in_progress"] is False
    assert first["note"] == "partial week, 5 of 7 days"

    last = info[date(2026, 2, 2)]
    assert last["partial"] is True
    assert last["in_progress"] is True
    assert last["note"] == "in progress"

    assert info[date(2026, 1, 12)]["partial"] is False
    assert info[date(2026, 1, 12)]["note"] == ""


def test_partial_buckets_mark_month_ends_and_take_their_noun_from_the_caller():
    info = partial_buckets(date(2026, 1, 15), date(2026, 3, 10), "month")
    assert info[date(2026, 1, 1)]["note"] == "partial month, 17 of 31 days"
    assert info[date(2026, 2, 1)]["partial"] is False
    assert info[date(2026, 3, 1)]["in_progress"] is True

    named = partial_buckets(date(2026, 1, 15), date(2026, 3, 5), "month", noun="calendar month")
    assert named[date(2026, 1, 1)]["note"] == "partial calendar month, 17 of 31 days"


def test_bar_chart_marks_partial_bars_and_stays_finite():
    chart = bar_chart(bar_points(), unit="t", bucket="week", aria_label="Volume")

    assert chart["kind"] == "bars"
    assert len(chart["bars"]) == 3
    assert chart["bars"][0]["partial"] is True
    assert chart["bars"][0]["in_progress"] is False
    assert "in progress" in chart["bars"][0]["title"]
    assert chart["bars"][2]["height"] > 0
    assert chart["aria_label"] == "Volume"
    assert chart["empty"] is False
    assert chart["ticks"][-1]["label"] == "40t"

    for bar in chart["bars"]:
        assert all(isinstance(bar[key], float) for key in ("x", "y", "width", "height"))
        assert bar["x"] >= CHART_LEFT
        assert bar["y"] >= CHART_TOP


def test_bar_chart_returns_the_documented_keys_in_order():
    chart = bar_chart(bar_points(), unit="t", aria_label="Volume")

    assert list(chart) == BAR_KEYS
    assert list(chart["bars"][0]) == [
        "x",
        "y",
        "width",
        "height",
        "title",
        "partial",
        "in_progress",
        "index",
    ]
    assert [bar["index"] for bar in chart["bars"]] == [0, 1, 2]


def test_bar_chart_titles_name_the_bucket_the_value_and_the_note():
    points = [
        {"date": date(2026, 1, 5), "value": 10},
        {"date": date(2026, 2, 2), "value": 0.5, "note": "partial week, 5 of 7 days"},
    ]
    chart = bar_chart(points, unit="t", bucket="week")

    assert chart["bars"][0]["title"] == "Week of 5 Jan 2026: 10t"
    assert chart["bars"][1]["title"] == "Week of 2 Feb 2026: 0.5t, partial week, 5 of 7 days"


def test_bar_chart_takes_the_value_text_from_the_caller():
    points = [{"date": date(2026, 1, 5), "value": 10}]
    chart = bar_chart(points, bucket="month", value_text=lambda point: f"{point['value']} ppm")

    assert chart["bars"][0]["title"] == "Jan 2026: 10 ppm"


def test_bar_chart_zero_series_collapses_to_a_one_unit_axis():
    points = [{"date": date(2026, 1, 5), "value": 0}]
    chart = bar_chart(points, unit="t", bucket="week", aria_label="Volume")

    assert chart["empty"] is True
    assert chart["ticks"][-1]["label"] == "1t"
    assert chart["bars"][0]["height"] == 0
    assert chart["bars"][0]["y"] == float(CHART_TOP + _plot_frame()[1])


def test_bar_chart_without_points_does_not_divide_by_zero():
    chart = bar_chart([], unit="t", aria_label="Volume")

    assert chart["bars"] == []
    assert chart["xlabels"] == []
    assert chart["line"] == ""
    assert chart["has_line"] is False
    assert chart["empty"] is True
    assert chart["ticks"][-1]["label"] == "1t"


def test_bar_chart_thins_xlabels_and_builds_the_moving_average_line():
    points = [
        {"date": date(2026, 1, 5), "value": 1, "moving_average": 1},
        {"date": date(2026, 1, 12), "value": 3, "moving_average": 2},
        {"date": date(2026, 1, 19), "value": 6, "moving_average": None},
    ]
    chart = bar_chart(points, unit="d", bucket="week")

    assert [label["label"] for label in chart["xlabels"]] == ["5 Jan", "12 Jan", "19 Jan"]
    assert chart["has_line"] is True
    assert len(chart["line"].split()) == 2

    crowded = [
        {"date": date(2026, 1, 5) + timedelta(days=7 * index), "value": index}
        for index in range(30)
    ]
    assert len(bar_chart(crowded, bucket="week")["xlabels"]) == MAX_BARS


def test_line_chart_projects_finite_points_and_labels():
    lines = [
        {"label": "2026", "points": [(1, 1.5), (32, 4.0)]},
        {"label": "2025", "points": [(1, 0.5), (32, 2.0)]},
    ]
    chart = line_chart(lines, x_max=366, x_ticks=[(1, "Jan"), (60, "Mar")], y_unit=" t")

    assert chart["kind"] == "lines"
    assert len(chart["lines"]) == 2
    assert [line["class"] for line in chart["lines"]] == ["series-1", "series-2"]
    assert [label["label"] for label in chart["xlabels"]] == ["Jan", "Mar"]
    assert chart["ticks"][-1]["label"] == "4 t"
    assert chart["empty"] is False
    for line in chart["lines"]:
        for pair in line["points"].split():
            x, y = pair.split(",")
            assert float(x) == float(x)
            assert float(y) == float(y)


def test_line_chart_returns_the_documented_keys_in_order():
    chart = line_chart([{"label": "2026", "points": [(1, 1.5)]}], x_max=366, x_ticks=[])

    assert list(chart) == LINE_KEYS
    assert list(chart["lines"][0]) == ["label", "class", "points", "dots"]


def test_line_chart_draws_a_single_dot_for_a_single_point_line():
    chart = line_chart([{"label": "Today", "points": [(5, 3.0)]}], x_max=10, x_ticks=[(5, "5")])

    line = chart["lines"][0]
    assert line["dots"] == [{"x": "380.0", "y": "62.0"}]
    assert len(line["points"].split()) == 1
    assert [label["label"] for label in chart["xlabels"]] == ["5"]


def test_line_chart_skips_empty_lines_but_keeps_the_input_class_order():
    lines = [
        {"label": "2026", "points": []},
        {"label": "2025", "points": [(1, 0.5)]},
    ]
    chart = line_chart(lines, x_max=0, x_ticks=[])

    assert len(chart["lines"]) == 1
    assert chart["lines"][0]["label"] == "2025"
    assert chart["lines"][0]["class"] == "series-2"
    assert len(chart["lines"][0]["dots"]) == 1


def test_line_chart_without_points_is_empty_and_stays_finite():
    chart = line_chart([], x_max=0, x_ticks=[])

    assert chart["lines"] == []
    assert chart["xlabels"] == []
    assert chart["empty"] is True
    assert chart["ticks"][-1]["label"] == "1"


def test_report_chart_partial_renders_bars():
    markup = rendered(bar_chart(bar_points(), unit="t", aria_label="Blood pressure by week"))

    assert markup.startswith('<svg class="chart chart-bars"')
    assert 'viewBox="0 0 720 240"' in markup
    assert 'role="img"' in markup
    assert 'preserveAspectRatio="xMidYMid meet"' in markup
    assert aria_label_of(markup) == "Blood pressure by week"
    assert 'class="chart-bar bar-partial"' in markup
    assert "<title>Week of 5 Jan 2026: 10t, in progress</title>" in markup
    assert '<line class="chart-gridline"' in markup
    assert '<line class="chart-axis"' in markup
    assert markup.count("<rect") == 3
    assert "nan" not in markup
    assert "inf" not in markup
    assert "None" not in markup


def test_report_chart_partial_renders_lines_with_dots():
    lines = [
        {"label": "2026", "points": [(1, 1.5), (32, 4.0)]},
        {"label": "2025", "points": [(1, 0.5)]},
    ]
    markup = rendered(
        line_chart(lines, x_max=366, x_ticks=[(1, "Jan"), (60, "Mar")], aria_label="Trend")
    )

    assert markup.startswith('<svg class="chart chart-lines"')
    assert 'role="img"' in markup
    assert aria_label_of(markup) == "Trend"
    assert '<polyline class="chart-line series-1"' in markup
    assert '<polyline class="chart-line series-2"' in markup
    assert markup.count("<circle") == 1
    assert "nan" not in markup
    assert "inf" not in markup
    assert "None" not in markup
