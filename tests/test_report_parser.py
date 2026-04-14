"""
Tests for backend/report_parser.py

Covers ReportParser methods: parse_report_pages, parse_page_visuals,
normalize_position, and related dataclasses/constants.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.report_parser import (
    ReportParser,
    PBIVisual,
    PBIReportPage,
    PBIReport,
    PBI_VISUAL_TYPE_MAP,
    UNSUPPORTED_VISUALS,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def parser():
    return ReportParser()


# ── parse_report_pages ─────────────────────────────────────────────────────────

def test_parse_report_pages_returns_list():
    pages = parser().parse_report_pages(
        [{"name": "ReportSection1", "displayName": "Overview", "order": 0}]
    )
    assert isinstance(pages, list)
    assert len(pages) == 1


def test_parse_report_pages_display_name_preserved():
    pages = parser().parse_report_pages(
        [{"name": "ReportSection1", "displayName": "Overview", "order": 0}]
    )
    assert pages[0].display_name == "Overview"


def test_parse_report_pages_empty_input_returns_empty():
    pages = parser().parse_report_pages([])
    assert pages == []


def test_parse_report_pages_name_field_preserved():
    pages = parser().parse_report_pages(
        [{"name": "ReportSection1", "displayName": "Overview", "order": 0}]
    )
    assert pages[0].name == "ReportSection1"


def test_parse_report_pages_order_field_preserved():
    pages = parser().parse_report_pages(
        [{"name": "ReportSection1", "displayName": "Overview", "order": 3}]
    )
    assert pages[0].order == 3


def test_parse_report_pages_multiple_pages_sorted_by_order():
    raw = [
        {"name": "S2", "displayName": "B", "order": 2},
        {"name": "S0", "displayName": "A", "order": 0},
        {"name": "S1", "displayName": "C", "order": 1},
    ]
    pages = parser().parse_report_pages(raw)
    assert [p.order for p in pages] == [0, 1, 2]


def test_parse_report_pages_missing_displayName_falls_back_to_name():
    pages = parser().parse_report_pages(
        [{"name": "ReportSection1", "order": 0}]
    )
    assert pages[0].display_name == "ReportSection1"


def test_parse_report_pages_returns_pbi_report_page_objects():
    pages = parser().parse_report_pages(
        [{"name": "ReportSection1", "displayName": "Overview", "order": 0}]
    )
    assert isinstance(pages[0], PBIReportPage)


# ── parse_page_visuals ─────────────────────────────────────────────────────────

def test_parse_page_visuals_extracts_type():
    visuals = parser().parse_page_visuals(
        [{"id": "v1", "visualType": "barChart", "title": "Sales",
          "x": 0, "y": 0, "width": 400, "height": 300}]
    )
    assert visuals[0].visual_type == "barChart"


def test_parse_page_visuals_extracts_position():
    visuals = parser().parse_page_visuals(
        [{"id": "v1", "visualType": "barChart", "title": "Sales",
          "x": 10, "y": 20, "width": 400, "height": 300}]
    )
    pos = visuals[0].position
    assert "x" in pos and "y" in pos and "width" in pos and "height" in pos


def test_parse_page_visuals_extracts_title():
    visuals = parser().parse_page_visuals(
        [{"id": "v1", "visualType": "barChart", "title": "Sales",
          "x": 0, "y": 0, "width": 400, "height": 300}]
    )
    assert visuals[0].title == "Sales"


def test_parse_page_visuals_empty_returns_empty():
    visuals = parser().parse_page_visuals([])
    assert visuals == []


def test_parse_page_visuals_returns_pbi_visual_objects():
    visuals = parser().parse_page_visuals(
        [{"id": "v1", "visualType": "barChart", "title": "Sales",
          "x": 0, "y": 0, "width": 400, "height": 300}]
    )
    assert isinstance(visuals[0], PBIVisual)


def test_parse_page_visuals_multiple_visuals():
    raw = [
        {"id": "v1", "visualType": "barChart", "title": "A", "x": 0, "y": 0, "width": 400, "height": 300},
        {"id": "v2", "visualType": "lineChart", "title": "B", "x": 400, "y": 0, "width": 400, "height": 300},
    ]
    visuals = parser().parse_page_visuals(raw)
    assert len(visuals) == 2


# ── normalize_position ─────────────────────────────────────────────────────────

def test_normalize_position_full_width():
    # 1280px wide on 1280px canvas → 6 columns
    pos = parser().normalize_position(0, 0, 1280, 300, 1280)
    assert pos["x"] == 0
    assert pos["y"] == 0
    assert pos["width"] == 6
    assert pos["height"] == 300


def test_normalize_position_half_width():
    # ~640px → ~3 columns
    pos = parser().normalize_position(0, 0, 640, 300, 1280)
    assert pos["width"] == 3


def test_normalize_position_quarter_width():
    # ~320px → ~2 columns (round(320/213.3) = round(1.5) = 2)
    pos = parser().normalize_position(0, 0, 320, 300, 1280)
    assert pos["width"] >= 1


def test_normalize_position_minimum_width_is_1():
    # Very small visual still has width at least 1
    pos = parser().normalize_position(0, 0, 10, 100, 1280)
    assert pos["width"] >= 1


def test_normalize_position_x_offset():
    # Visual starting at half the canvas
    pos = parser().normalize_position(640, 0, 640, 300, 1280)
    assert pos["x"] == 3


def test_normalize_position_y_passthrough():
    pos = parser().normalize_position(0, 500, 400, 300, 1280)
    assert pos["y"] == 500


def test_normalize_position_height_passthrough():
    pos = parser().normalize_position(0, 0, 400, 456, 1280)
    assert pos["height"] == 456


def test_normalize_position_returns_dict_with_required_keys():
    pos = parser().normalize_position(0, 0, 640, 300, 1280)
    for key in ("x", "y", "width", "height"):
        assert key in pos


def test_normalize_position_width_does_not_exceed_6():
    pos = parser().normalize_position(0, 0, 9999, 300, 1280)
    assert pos["width"] <= 6


# ── PBI_VISUAL_TYPE_MAP ────────────────────────────────────────────────────────

def test_visual_type_map_has_bar_chart():
    assert "barChart" in PBI_VISUAL_TYPE_MAP
    assert PBI_VISUAL_TYPE_MAP["barChart"] == "bar"


def test_visual_type_map_has_line_chart():
    assert "lineChart" in PBI_VISUAL_TYPE_MAP
    assert PBI_VISUAL_TYPE_MAP["lineChart"] == "line"


def test_visual_type_map_has_counter_types():
    # card and kpiVisual should map to counter
    assert PBI_VISUAL_TYPE_MAP.get("card") == "counter"
    assert PBI_VISUAL_TYPE_MAP.get("kpiVisual") == "counter"


def test_visual_type_map_has_table_types():
    assert PBI_VISUAL_TYPE_MAP.get("tableEx") == "table"
    assert PBI_VISUAL_TYPE_MAP.get("matrix") == "table"


def test_visual_type_map_has_pie_chart():
    assert "pieChart" in PBI_VISUAL_TYPE_MAP
    assert PBI_VISUAL_TYPE_MAP["pieChart"] == "pie"


# ── UNSUPPORTED_VISUALS ────────────────────────────────────────────────────────

def test_unsupported_visuals_contains_map():
    assert "mapVisual" in UNSUPPORTED_VISUALS


def test_unsupported_visuals_contains_slicer():
    assert "slicerVisual" in UNSUPPORTED_VISUALS


def test_unsupported_visuals_is_frozenset():
    assert isinstance(UNSUPPORTED_VISUALS, frozenset)


# ── Dataclass defaults ─────────────────────────────────────────────────────────

def test_pbi_visual_dataclass_defaults():
    v = PBIVisual(visual_id="v1", visual_type="barChart")
    assert v.title == ""
    assert v.measures == []
    assert v.dimensions == []
    assert v.filters == []
    assert v.position == {}


def test_pbi_report_page_dataclass_defaults():
    p = PBIReportPage(name="S1", display_name="Overview")
    assert p.order == 0
    assert p.visuals == []


def test_pbi_report_dataclass_defaults():
    r = PBIReport(id="r1", name="My Report")
    assert r.dataset_id == ""
    assert r.pages == []


def test_pbi_visual_to_dict_has_required_keys():
    v = PBIVisual(visual_id="v1", visual_type="barChart")
    d = v.to_dict()
    for key in ("visual_id", "visual_type", "title", "measures", "dimensions", "filters", "position"):
        assert key in d


def test_pbi_report_page_to_dict_has_required_keys():
    p = PBIReportPage(name="S1", display_name="Overview")
    d = p.to_dict()
    for key in ("name", "display_name", "order", "visuals"):
        assert key in d


def test_pbi_report_to_dict_has_required_keys():
    r = PBIReport(id="r1", name="My Report")
    d = r.to_dict()
    for key in ("id", "name", "dataset_id", "pages"):
        assert key in d
