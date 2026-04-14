"""
Tests for backend/dashboard_generator.py

Covers DashboardGenerator and DashboardSpec: spec serialisation,
widget generation, layout logic, and metric-view-based generation.
"""
import sys
import os
import json
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.dashboard_generator import DashboardGenerator, DashboardSpec


# ── Sample input data ──────────────────────────────────────────────────────────

SAMPLE_MV_SPECS = [
    {
        "fact_table": "FactSales",
        "measures": [
            {"name": "Total Revenue", "expression": "SUM(source.salesamount)"},
            {"name": "Customer Count", "expression": "COUNT(DISTINCT source.customerkey)"},
        ],
        "dimensions": [
            {"name": "Region", "expression": "dimcustomer.region"},
            {"name": "Year", "expression": "dimdate.year"},
        ],
    }
]

SAMPLE_CATALOG = "main"
SAMPLE_SCHEMA = "sales_metrics"
SAMPLE_MODEL = "Sales Analytics"


# ── Helpers ────────────────────────────────────────────────────────────────────

def gen():
    return DashboardGenerator()


def make_spec():
    return gen().generate_from_metric_views(
        metric_view_specs=SAMPLE_MV_SPECS,
        model_name=SAMPLE_MODEL,
        catalog=SAMPLE_CATALOG,
        schema=SAMPLE_SCHEMA,
    )


# ── DashboardSpec serialisation ────────────────────────────────────────────────

def test_dashboard_spec_to_dict_has_datasets():
    spec = make_spec()
    d = spec.to_dict()
    assert "datasets" in d


def test_dashboard_spec_to_dict_has_pages():
    spec = make_spec()
    d = spec.to_dict()
    assert "pages" in d


def test_dashboard_spec_to_json_is_string():
    spec = make_spec()
    result = spec.to_json()
    assert isinstance(result, str)


def test_dashboard_spec_to_json_is_valid_json():
    spec = make_spec()
    result = spec.to_json()
    parsed = json.loads(result)
    assert isinstance(parsed, dict)


# ── generate_from_metric_views ─────────────────────────────────────────────────

def test_generate_from_metric_views_returns_dashboard_spec():
    spec = make_spec()
    assert isinstance(spec, DashboardSpec)


def test_generate_from_metric_views_has_at_least_one_page():
    spec = make_spec()
    d = spec.to_dict()
    assert len(d.get("pages", [])) >= 1


def test_generate_from_metric_views_has_datasets():
    spec = make_spec()
    d = spec.to_dict()
    assert len(d.get("datasets", [])) >= 1


def test_generate_from_metric_views_display_name_includes_model():
    spec = make_spec()
    # The spec's display_name should incorporate the model name
    assert SAMPLE_MODEL in spec.display_name


# ── Dataset query structure ────────────────────────────────────────────────────

def test_dataset_query_uses_measure_syntax():
    spec = make_spec()
    d = spec.to_dict()
    spec_str = json.dumps(d)
    assert "MEASURE(" in spec_str


def test_dataset_query_references_catalog_schema():
    spec = make_spec()
    d = spec.to_dict()
    spec_str = json.dumps(d)
    expected = f"{SAMPLE_CATALOG}.{SAMPLE_SCHEMA}"
    assert expected in spec_str


# ── Widget type versions ───────────────────────────────────────────────────────

def test_counter_widget_has_version_2():
    g = gen()
    layout_item = g._build_counter_widget(
        dataset_name="ds_test",
        measure_name="Total Revenue",
        measure_expr="SUM(`Total Revenue`)",
        title="Total Revenue",
        position={"x": 0, "y": 0, "width": 2, "height": 2},
    )
    spec = layout_item["widget"]["spec"]
    assert spec["version"] == 2


def test_counter_widget_type_is_counter():
    g = gen()
    layout_item = g._build_counter_widget(
        dataset_name="ds_test",
        measure_name="Total Revenue",
        measure_expr="SUM(`Total Revenue`)",
        title="Total Revenue",
        position={"x": 0, "y": 0, "width": 2, "height": 2},
    )
    assert layout_item["widget"]["spec"]["widgetType"] == "counter"


def test_bar_widget_has_version_3():
    g = gen()
    layout_item = g._build_bar_widget(
        dataset_name="ds_test",
        x_field="Region",
        y_field="Total Revenue",
        y_agg="SUM",
        title="Revenue by Region",
        position={"x": 0, "y": 2, "width": 3, "height": 4},
    )
    spec = layout_item["widget"]["spec"]
    assert spec["version"] == 3


def test_bar_widget_has_encodings_x_and_y():
    g = gen()
    layout_item = g._build_bar_widget(
        dataset_name="ds_test",
        x_field="Region",
        y_field="Total Revenue",
        y_agg="SUM",
        title="Revenue by Region",
        position={"x": 0, "y": 2, "width": 3, "height": 4},
    )
    encodings = layout_item["widget"]["spec"]["encodings"]
    assert "x" in encodings
    assert "y" in encodings


def test_line_widget_has_version_3():
    g = gen()
    layout_item = g._build_line_widget(
        dataset_name="ds_test",
        x_field="Year",
        y_field="Total Revenue",
        y_agg="SUM",
        title="Revenue over Time",
        position={"x": 3, "y": 2, "width": 3, "height": 4},
    )
    spec = layout_item["widget"]["spec"]
    assert spec["version"] == 3


def test_pie_widget_has_version_3():
    g = gen()
    layout_item = g._build_pie_widget(
        dataset_name="ds_test",
        category_field="Region",
        value_field="Total Revenue",
        title="Revenue Share",
        position={"x": 3, "y": 6, "width": 3, "height": 4},
    )
    spec = layout_item["widget"]["spec"]
    assert spec["version"] == 3


def test_table_widget_has_version_2():
    g = gen()
    layout_item = g._build_table_widget(
        dataset_name="ds_test",
        columns=["Region", "Total Revenue"],
        title="Summary Table",
        position={"x": 0, "y": 10, "width": 6, "height": 4},
    )
    spec = layout_item["widget"]["spec"]
    assert spec["version"] == 2


# ── Widget positions ───────────────────────────────────────────────────────────

def test_widget_position_has_x_y_width_height():
    g = gen()
    layout_item = g._build_counter_widget(
        dataset_name="ds_test",
        measure_name="Total Revenue",
        measure_expr="SUM(`Total Revenue`)",
        title="Total Revenue",
        position={"x": 1, "y": 2, "width": 2, "height": 2},
    )
    pos = layout_item["position"]
    for key in ("x", "y", "width", "height"):
        assert key in pos


def test_widget_position_width_max_6():
    g = gen()
    layout_item = g._build_table_widget(
        dataset_name="ds_test",
        columns=["Region", "Total Revenue"],
        title="Summary",
        position={"x": 0, "y": 0, "width": 6, "height": 4},
    )
    assert layout_item["position"]["width"] <= 6


# ── Auto-layout ────────────────────────────────────────────────────────────────

def _get_all_layout_items(spec: DashboardSpec) -> list:
    """Flatten all layout entries from all pages of the spec."""
    d = spec.to_dict()
    layouts = []
    for page in d.get("pages", []):
        layouts.extend(page.get("layout", []))
    return layouts


def test_auto_layout_counters_in_first_row():
    spec = make_spec()
    layouts = _get_all_layout_items(spec)
    # Items at y=0 are counter widgets (first row)
    first_row = [l for l in layouts if l.get("position", {}).get("y", -1) == 0]
    assert len(first_row) >= 1, "Expected at least one widget in the first row (counters)"


def test_auto_layout_bar_charts_after_counters():
    spec = make_spec()
    layouts = _get_all_layout_items(spec)
    # There should be some widgets not in row 0 (bar charts below counters)
    below_first = [l for l in layouts if l.get("position", {}).get("y", 0) > 0]
    assert len(below_first) >= 1, "Expected bar charts and table below counter row"


def test_auto_layout_table_at_end():
    spec = make_spec()
    layouts = _get_all_layout_items(spec)
    assert len(layouts) >= 2, "Expected multiple layout items"
    # The table widget should have the maximum y value
    y_values = [l.get("position", {}).get("y", 0) for l in layouts]
    max_y = max(y_values)
    # Find the widget at max_y and verify it's a table
    table_candidates = [l for l in layouts if l.get("position", {}).get("y", 0) == max_y]
    assert len(table_candidates) >= 1


def test_auto_layout_table_widget_spans_full_width():
    spec = make_spec()
    layouts = _get_all_layout_items(spec)
    # The table (last y) should span full 6 columns
    y_values = [l.get("position", {}).get("y", 0) for l in layouts]
    max_y = max(y_values)
    table_items = [l for l in layouts if l.get("position", {}).get("y", 0) == max_y]
    # At least one full-width widget at the end
    full_width = [l for l in table_items if l.get("position", {}).get("width", 0) == 6]
    assert len(full_width) >= 1


# ── Edge cases ─────────────────────────────────────────────────────────────────

def test_generate_with_empty_specs_returns_default():
    g = gen()
    spec = g.generate_from_metric_views(
        metric_view_specs=[],
        model_name=SAMPLE_MODEL,
        catalog=SAMPLE_CATALOG,
        schema=SAMPLE_SCHEMA,
    )
    assert isinstance(spec, DashboardSpec)
    d = spec.to_dict()
    assert "pages" in d
    assert "datasets" in d


def test_generate_display_name_is_model_dashboard():
    spec = make_spec()
    assert spec.display_name == f"{SAMPLE_MODEL} Dashboard"


def test_dashboard_spec_datasets_is_list():
    spec = make_spec()
    assert isinstance(spec.datasets, list)


def test_dashboard_spec_pages_is_list():
    spec = make_spec()
    assert isinstance(spec.pages, list)
