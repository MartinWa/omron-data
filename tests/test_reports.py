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
    _day_ticks,
    _plot_frame,
    _round_coord,
    axis_ticks,
    bar_chart,
    delta,
    line_chart,
    nice_domain,
    nice_max,
    nice_step,
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
    "plot_width",
    "right_edge",
    "bars",
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
    "plot_width",
    "right_edge",
    "y_min",
    "y_max",
    "max_gap",
    "bands",
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


def test_nice_step_snaps_up_to_one_two_two_point_five_five_ten():
    assert nice_step(None) == 1.0
    assert nice_step(0) == 1.0
    assert nice_step(float("nan")) == 1.0
    assert nice_step(0.24) == 0.25
    assert nice_step(0.25) == 0.25
    assert nice_step(0.26) == 0.5
    assert nice_step(2) == 2.0
    assert nice_step(2.1) == 2.5
    assert nice_step(3) == 5.0
    assert nice_step(20) == 20.0
    assert nice_step(21) == 25.0


def test_nice_domain_pads_outward_to_an_even_number_of_round_steps():
    assert nice_domain(131, 132) == (131.0, 132.0, 0.25)
    assert nice_domain(0, 1) == (0.0, 1.0, 0.25)
    assert nice_domain(60, 140) == (60.0, 140.0, 20.0)

    dmin, dmax, step = nice_domain(72.5, 72.5)
    assert dmin == 68.0
    assert dmax == 80.0
    assert step == 2.0
    assert (dmax - dmin) / step == 6

    dmin, dmax, step = nice_domain(100, 160)
    assert (dmin, dmax) == (100.0, 180.0)
    assert (dmax - dmin) / step % 2 == 0
    assert step == 20.0


def test_nice_domain_treats_non_finite_bounds_as_a_finite_default():
    assert nice_domain(float("nan"), float("inf")) == (0.0, 1.0, 0.25)
    assert nice_domain(float("-inf"), 5.0) == nice_domain(0.0, 5.0)


def test_line_chart_ignores_non_finite_points_instead_of_crashing():
    lines = [{"label": "Weight", "points": [(0, float("nan")), (10, 72.5), (20, float("inf"))]}]
    chart = line_chart(lines, x_max=30, x_ticks=[(0, "a"), (30, "b")], y_unit="kg")

    rendered = render_to_string("partials/report_chart.html", {"chart": chart})
    assert "nan" not in rendered
    assert "inf" not in rendered
    assert chart["y_max"] > chart["y_min"]


def test_axis_ticks_are_zero_half_and_max_with_the_raw_unit():
    ticks = axis_ticks(20, "t")

    assert [tick["label"] for tick in ticks] == ["0 t", "10 t", "20 t"]
    assert [tick["value"] for tick in ticks] == [0, 10.0, 20]
    assert [tick["value"] for tick in ticks] == sorted(tick["value"] for tick in ticks)

    assert [tick["label"] for tick in axis_ticks(20, "%")] == ["0%", "10%", "20%"]
    assert [tick["label"] for tick in axis_ticks(20, "")] == ["0", "10", "20"]


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


def test_day_ticks_omit_the_year_unless_the_range_crosses_one_and_the_tick_is_in_january():
    ticks = _day_ticks(date(2026, 1, 5), date(2026, 1, 26))

    assert [label for _, label in ticks] == ["5 Jan", "12 Jan", "19 Jan", "26 Jan"]

    across = _day_ticks(date(2025, 12, 20), date(2026, 1, 10), multi_year=True)

    assert [label for _, label in across] == ["20 Dec", "27 Dec", "3 Jan 2026", "10 Jan 2026"]
    same_year = _day_ticks(date(2025, 12, 20), date(2026, 1, 10), multi_year=False)
    assert [label for _, label in same_year] == ["20 Dec", "27 Dec", "3 Jan", "10 Jan"]


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
    assert chart["ticks"][-1]["label"] == "40 t"
    assert "segments" not in chart
    assert "has_line" not in chart

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

    assert chart["bars"][0]["title"] == "Week of 5 Jan 2026: 10 t"
    assert chart["bars"][1]["title"] == "Week of 2 Feb 2026: 0.5 t, partial week, 5 of 7 days"


def test_bar_chart_keeps_the_percent_sign_tight_and_spaces_other_units():
    assert bar_chart(bar_points(), unit="%")["ticks"][-1]["label"] == "40%"
    assert bar_chart(bar_points(), unit="kg")["ticks"][-1]["label"] == "40 kg"


def test_bar_chart_takes_the_value_text_from_the_caller():
    points = [{"date": date(2026, 1, 5), "value": 10}]
    chart = bar_chart(points, bucket="month", value_text=lambda point: f"{point['value']} ppm")

    assert chart["bars"][0]["title"] == "Jan 2026: 10 ppm"


def test_bar_chart_zero_series_collapses_to_a_one_unit_axis():
    points = [{"date": date(2026, 1, 5), "value": 0}]
    chart = bar_chart(points, unit="t", bucket="week", aria_label="Volume")

    assert chart["empty"] is True
    assert chart["ticks"][-1]["label"] == "1 t"
    assert chart["bars"][0]["height"] == 0
    assert chart["bars"][0]["y"] == float(CHART_TOP + _plot_frame()[1])


def test_bar_chart_without_points_does_not_divide_by_zero():
    chart = bar_chart([], unit="t", aria_label="Volume")

    assert chart["bars"] == []
    assert chart["xlabels"] == []
    assert chart["empty"] is True
    assert chart["ticks"][-1]["label"] == "1 t"


def test_bar_chart_thins_xlabels_when_the_buckets_crowd():
    crowded = [
        {"date": date(2026, 1, 5) + timedelta(days=7 * index), "value": index}
        for index in range(30)
    ]
    chart = bar_chart(crowded, bucket="week")

    assert len(chart["xlabels"]) == MAX_BARS
    assert chart["xlabels"][0]["label"] == "5 Jan"
    assert chart["xlabels"][-1]["label"] == "27 Jul"


def test_bar_chart_week_ticks_show_the_year_only_across_a_year_boundary_in_january():
    points = [
        {"date": date(2025, 12, 29), "value": 1},
        {"date": date(2026, 1, 5), "value": 2},
    ]

    assert [label["label"] for label in bar_chart(points, bucket="week")["xlabels"]] == [
        "29 Dec",
        "5 Jan",
    ]
    assert [
        label["label"] for label in bar_chart(points, bucket="week", multi_year=True)["xlabels"]
    ] == ["29 Dec", "5 Jan 2026"]


def test_line_chart_projects_finite_segments_and_labels():
    lines = [
        {"label": "2026", "points": [(1, 1.5), (32, 4.0)]},
        {"label": "2025", "points": [(1, 0.5), (32, 2.0)]},
    ]
    chart = line_chart(lines, x_max=366, x_ticks=[(1, "Jan"), (60, "Mar")], y_unit="t")

    assert chart["kind"] == "lines"
    assert len(chart["lines"]) == 2
    assert [line["class"] for line in chart["lines"]] == ["series-1", "series-2"]
    assert [label["label"] for label in chart["xlabels"]] == ["Jan", "Mar"]
    assert [tick["label"] for tick in chart["ticks"]] == ["0 t", "2 t", "4 t"]
    assert chart["empty"] is False
    for line in chart["lines"]:
        assert list(line) == ["label", "class", "segments", "dots", "marks"]
        assert len(line["segments"]) == 1
        for pair in line["segments"][0].split():
            x, y = pair.split(",")
            assert float(x) == float(x)
            assert float(y) == float(y)


def test_line_chart_returns_the_documented_keys_in_order():
    chart = line_chart([{"label": "2026", "points": [(1, 1.5)]}], x_max=366, x_ticks=[])

    assert list(chart) == LINE_KEYS
    assert list(chart["lines"][0]) == ["label", "class", "segments", "dots", "marks"]
    assert chart["bands"] == []
    assert chart["max_gap"] is None


def test_line_chart_draws_a_single_dot_for_a_single_point_line():
    chart = line_chart([{"label": "Today", "points": [(5, 3.0)]}], x_max=10, x_ticks=[(5, "5")])

    line = chart["lines"][0]
    assert line["segments"] == ["380.0,110.0"]
    assert line["dots"] == [{"x": 380.0, "y": 110.0, "class": "", "title": ""}]
    assert [label["label"] for label in chart["xlabels"]] == ["5"]


def test_line_chart_domain_follows_the_data_instead_of_zero():
    chart = line_chart(
        [{"label": "Weight", "points": [(0, 131), (15, 131.5), (29, 132)]}],
        x_max=29,
        x_ticks=[],
        y_unit="kg",
    )

    assert 126 <= chart["y_min"] < chart["y_max"] <= 136
    assert chart["y_max"] - chart["y_min"] <= 10
    assert [tick["label"] for tick in chart["ticks"]] == ["131 kg", "131.5 kg", "132 kg"]


def test_line_chart_flat_series_gets_a_non_degenerate_domain():
    chart = line_chart([{"label": "Flat", "points": [(0, 72.5), (3, 72.5)]}], x_max=29, x_ticks=[])

    assert chart["y_max"] > chart["y_min"]
    assert chart["lines"][0]["segments"] == ["54.0,134.0 121.45,134.0"]


def test_line_chart_widens_the_domain_for_given_bounds_but_never_clips_the_data():
    bounded = line_chart(
        [{"label": "Fat", "points": [(0, 22.0), (10, 24.0)]}],
        x_max=10,
        x_ticks=[],
        y_unit="%",
        y_min=20,
        y_max=45,
    )
    assert bounded["y_min"] <= 20
    assert bounded["y_max"] >= 45

    outside = line_chart(
        [{"label": "Fat", "points": [(0, 10.0), (10, 50.0)]}],
        x_max=10,
        x_ticks=[],
        y_min=20,
        y_max=45,
    )
    assert outside["y_min"] <= 10
    assert outside["y_max"] >= 50


def test_line_chart_splits_segments_across_gaps_and_missing_values():
    chart = line_chart(
        [{"label": "W", "points": [(0, 1), (3, 2), (20, 3), (21, None), (22, 4)]}],
        x_max=29,
        x_ticks=[],
        max_gap=7,
    )

    line = chart["lines"][0]
    assert len(line["segments"]) == 2
    assert [len(segment.split()) for segment in line["segments"]] == [2, 2]
    assert line["dots"] == []

    isolated = line_chart(
        [{"label": "W", "points": [(0, 1), (5, None), (10, 2)]}],
        x_max=29,
        x_ticks=[],
        max_gap=7,
    )
    line = isolated["lines"][0]
    assert len(line["segments"]) == 2
    assert len(line["dots"]) == 2


def test_line_chart_dots_all_draws_every_point_and_the_latest_dot_is_emphasised():
    chart = line_chart(
        [
            {
                "label": "W",
                "points": [(0, 72.0), (3, 72.4), (6, 72.5)],
                "dots_all": True,
                "latest_title": "Latest 72.5 kg on 26 Nov 2026",
            }
        ],
        x_max=29,
        x_ticks=[],
        y_unit="kg",
    )

    line = chart["lines"][0]
    assert len(line["dots"]) == 3
    assert line["dots"][-1]["class"] == "dot-latest"
    assert line["dots"][-1]["title"] == "Latest 72.5 kg on 26 Nov 2026"
    assert all(dot["class"] == "" for dot in line["dots"][:-1])


def test_line_chart_marks_project_with_the_line_class():
    chart = line_chart(
        [
            {
                "label": "Systolic",
                "points": [(0, 120), (5, 141)],
                "marks": [(5, 141, "Systolic 141 mmHg on 25 Nov 2026 08:00")],
            }
        ],
        x_max=10,
        x_ticks=[],
        y_unit="mmHg",
    )

    assert chart["lines"][0]["marks"] == [
        {
            "x": 380.0,
            "y": 105.2,
            "class": "series-1",
            "title": "Systolic 141 mmHg on 25 Nov 2026 08:00",
        }
    ]


def test_line_chart_projects_bands_clamped_to_the_domain():
    chart = line_chart(
        [{"label": "BP", "points": [(0, 100), (10, 160)]}],
        x_max=10,
        x_ticks=[],
        y_unit="mmHg",
        bands=[
            {
                "from": 120,
                "to": 130,
                "label": "AHA elevated (systolic 120–129)",
                "class": "band-warn",
            },
            {"from": 130, "to": 140, "label": "AHA stage 1 (130–139)", "class": "band-warn"},
            {"from": 140, "to": None, "label": "AHA stage 2 (≥140)", "class": "band-danger"},
            {"from": 200, "to": 300, "label": "Outside", "class": "band-danger"},
        ],
    )

    assert chart["y_min"] == 100.0
    assert chart["y_max"] == 180.0
    assert chart["bands"] == [
        {
            "y": 134.0,
            "height": 24.0,
            "class": "band-warn",
            "label": "AHA elevated (systolic 120–129)",
        },
        {"y": 110.0, "height": 24.0, "class": "band-warn", "label": "AHA stage 1 (130–139)"},
        {"y": 14.0, "height": 96.0, "class": "band-danger", "label": "AHA stage 2 (≥140)"},
    ]


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


def test_line_chart_accepts_an_explicit_line_class_for_averages():
    chart = line_chart(
        [
            {"label": "Daily", "points": [(0, 1), (5, 2)], "dots_all": True},
            {"label": "Average", "points": [(0, 1), (5, 2)], "class": "series-2 avg"},
        ],
        x_max=10,
        x_ticks=[],
    )

    assert [line["class"] for line in chart["lines"]] == ["series-1", "series-2 avg"]
    assert chart["lines"][1]["dots"] == []


def test_line_chart_without_points_is_empty_and_stays_finite():
    chart = line_chart([], x_max=0, x_ticks=[])

    assert chart["lines"] == []
    assert chart["xlabels"] == []
    assert chart["empty"] is True
    assert chart["ticks"][-1]["label"] == "1"


def test_delta_absolute_texts_carry_precision_suffix_and_harm_tone():
    up = delta(
        140,
        120,
        absolute=True,
        suffix=" mmHg",
        precision=0,
        harm="up",
        comparison="the previous 30 days",
    )
    assert up == {
        "text": "+20 mmHg",
        "direction": "up",
        "tone": "worse",
        "aria": "up 20 mmHg versus the previous 30 days",
    }

    down = delta(72.1, 72.5, absolute=True, suffix=" kg", precision=1, harm="none", comparison="x")
    assert down["text"] == "−0.4 kg"
    assert down["direction"] == "down"
    assert down["tone"] == "neutral"
    assert down["aria"] == "down 0.4 kg versus x"

    better = delta(120, 140, absolute=True, suffix=" mmHg", precision=0, harm="up", comparison="x")
    assert better["tone"] == "better"

    flat = delta(5, 5, absolute=True, suffix=" kg", precision=1, harm="up", comparison="x")
    assert flat["text"] == "—"
    assert flat["tone"] == "neutral"
    assert flat["aria"] == "unchanged versus x"

    assert delta(None, 5) is None
    assert delta(5, None) is None


def test_delta_treats_sub_half_unit_changes_as_flat():
    tiny = delta(
        72.50001, 72.5, absolute=True, suffix=" kg", precision=1, harm="none", comparison="x"
    )
    assert tiny["text"] == "—"
    assert tiny["direction"] == "flat"
    assert tiny["tone"] == "neutral"


def test_delta_relative_stays_a_percentage_and_never_carries_harm():
    up = delta(112, 100, harm="none", comparison="the previous 30 days")

    assert up["text"] == "↑ 12%"
    assert up["direction"] == "up"
    assert up["tone"] == "neutral"
    assert up["aria"] == "up 12 percent versus the previous 30 days"

    warned = delta(112, 100, harm="up", comparison="the previous 30 days")
    assert warned["tone"] == "worse"

    none = delta(100, 0, comparison="the previous period")
    assert none["text"] == "—"
    assert none["tone"] == "neutral"
    assert none["aria"] == "no reading in the previous period to compare"


def test_report_chart_partial_renders_bars_without_a_line():
    markup = rendered(bar_chart(bar_points(), unit="t", aria_label="Blood pressure by week"))

    assert markup.startswith('<svg class="chart chart-bars"')
    assert 'viewBox="0 0 720 240"' in markup
    assert 'role="img"' in markup
    assert 'preserveAspectRatio="xMidYMid meet"' in markup
    assert aria_label_of(markup) == "Blood pressure by week"
    assert 'class="chart-bar bar-partial"' in markup
    assert "<title>Week of 5 Jan 2026: 10 t, in progress</title>" in markup
    assert '<line class="chart-gridline"' in markup
    assert '<line class="chart-axis"' in markup
    assert markup.count("<rect") == 3
    assert "<polyline" not in markup
    assert "nan" not in markup
    assert "inf" not in markup
    assert "None" not in markup


def test_report_chart_partial_renders_bands_segments_dots_of_every_shape_and_marks():
    chart = line_chart(
        [
            {"label": "A", "points": [(0, 1), (5, 2)], "dots_all": True},
            {"label": "B", "points": [(0, 3), (5, 4)], "dots_all": True},
            {
                "label": "C",
                "points": [(0, 5), (5, 6)],
                "dots_all": True,
                "marks": [(5, 6, "Mark six")],
            },
        ],
        x_max=10,
        x_ticks=[(0, "start"), (10, "end")],
        y_unit="u",
        bands=[{"from": 4, "to": None, "label": "High band", "class": "band-danger"}],
        aria_label="Shapes",
    )
    markup = rendered(chart)

    assert markup.startswith('<svg class="chart chart-lines"')
    assert 'role="img"' in markup
    assert aria_label_of(markup) == "Shapes"
    assert markup.index('<g class="chart-bands">') < markup.index('<g class="chart-lines">')
    assert 'class="chart-band band-danger"' in markup
    assert "<title>High band</title>" in markup
    assert markup.count("<polyline") == 3
    assert '<polyline class="chart-line series-1"' in markup
    assert markup.count('<circle class="chart-dot series-1"') == 2
    assert markup.count('<rect class="chart-dot series-2"') == 2
    assert markup.count('<polygon class="chart-dot series-3"') == 2
    assert '<circle class="chart-dot series-3 outlier"' in markup
    assert "<title>Mark six</title>" in markup
    assert "nan" not in markup
    assert "inf" not in markup
    assert "None" not in markup


def test_report_chart_partial_renders_a_marked_latest_dot():
    chart = line_chart(
        [
            {
                "label": "W",
                "points": [(0, 72.0), (5, 72.5)],
                "dots_all": True,
                "latest_title": "Latest 72.5 kg on 26 Nov 2026",
            }
        ],
        x_max=10,
        x_ticks=[],
        aria_label="Trend",
    )
    markup = rendered(chart)

    assert '<circle class="chart-dot series-1 dot-latest"' in markup
    assert "<title>Latest 72.5 kg on 26 Nov 2026</title>" in markup
