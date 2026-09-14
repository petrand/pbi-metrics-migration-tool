"""
Tests for backend/evaluator.py

Covers MeasureEvaluation, FactGroupEvaluation, PipelineSummary,
MigrationManifest, EvaluationReporter helpers, and serialisation.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.evaluator import (
    EvaluationReporter,
    MeasureEvaluation,
    FactGroupEvaluation,
    PipelineSummary,
    MigrationManifest,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def reporter():
    return EvaluationReporter()


def _make_measure_eval(name="Revenue", status="converted", confidence=95,
                       issues=None, warnings=None, deployed=True):
    return MeasureEvaluation(
        name=name,
        original_dax=f"SUM(Fact[{name}])",
        translated_sql=f"SUM(source.{name.lower()})",
        status=status,
        confidence=confidence,
        issues=issues or [],
        warnings=warnings or [],
        deployed=deployed,
    )


# ── evaluate_measure ──────────────────────────────────────────────────────────

def test_evaluate_measure_returns_measure_evaluation():
    r = reporter()
    result = r.evaluate_measure("Revenue", "SUM(Fact[Revenue])", {"sql": "SUM(source.revenue)", "status": "converted", "confidence": 100})
    assert isinstance(result, MeasureEvaluation)


def test_evaluate_measure_name_preserved():
    r = reporter()
    result = r.evaluate_measure("My Measure", "SUM(x)", {"sql": "SUM(source.x)", "status": "converted", "confidence": 90})
    assert result.name == "My Measure"


def test_evaluate_measure_converted_status_preserved():
    r = reporter()
    result = r.evaluate_measure("m", "SUM(x)", {"sql": "SUM(source.x)", "status": "converted", "confidence": 100})
    assert result.status == "converted"


def test_evaluate_measure_unsupported_status_when_no_sql():
    r = reporter()
    result = r.evaluate_measure("m", "COMPLEX_DAX()", {"sql": None, "status": "unsupported", "confidence": 0})
    assert result.status == "unsupported"


def test_evaluate_measure_confidence_zero_for_unsupported():
    r = reporter()
    result = r.evaluate_measure("m", "COMPLEX()", {"sql": None, "status": "unsupported", "confidence": 0})
    assert result.confidence == 0


def test_evaluate_measure_confidence_reduced_by_issues():
    r = reporter()
    result = r.evaluate_measure(
        "m", "SUM(x)",
        {"sql": "SUM(source.x)", "status": "converted", "confidence": 100,
         "issues": ["something failed"]}
    )
    # Each issue deducts 10 from confidence
    assert result.confidence <= 90


def test_evaluate_measure_normalises_success_status_to_converted():
    r = reporter()
    result = r.evaluate_measure("m", "SUM(x)", {"sql": "SUM(source.x)", "status": "success", "confidence": 95})
    assert result.status == "converted"


def test_evaluate_measure_normalises_failed_status_to_unsupported():
    r = reporter()
    result = r.evaluate_measure("m", "x", {"sql": None, "status": "failed", "confidence": 0})
    assert result.status == "unsupported"


def test_evaluate_measure_accepts_applied_transformations():
    r = reporter()
    result = r.evaluate_measure("m", "SUM(x)", {
        "sql": "SUM(source.x)",
        "status": "converted",
        "confidence": 100,
        "applied_transformations": ["SUM_translation"],
    })
    assert "SUM_translation" in result.applied_transformations


def test_evaluate_measure_window_spec_preserved():
    r = reporter()
    ws = {"range": "cumulative", "order_by": "date", "group_by": "year"}
    result = r.evaluate_measure("m", "TOTALYTD(x)", {
        "sql": "SUM(source.x)",
        "status": "converted",
        "confidence": 85,
        "window_spec": ws,
    })
    assert result.window_spec == ws


# ── evaluate_fact_group ───────────────────────────────────────────────────────

def test_evaluate_fact_group_returns_fact_group_evaluation():
    r = reporter()
    measures = [_make_measure_eval()]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    assert isinstance(result, FactGroupEvaluation)


def test_evaluate_fact_group_total_measures_correct():
    r = reporter()
    measures = [_make_measure_eval(f"m{i}") for i in range(5)]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    assert result.total_measures == 5


def test_evaluate_fact_group_converted_count_correct():
    r = reporter()
    measures = [
        _make_measure_eval("m1", status="converted"),
        _make_measure_eval("m2", status="converted"),
        _make_measure_eval("m3", status="unsupported"),
    ]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    assert result.converted == 2


def test_evaluate_fact_group_conversion_rate_excludes_excluded():
    r = reporter()
    measures = [
        _make_measure_eval("m1", status="converted"),
        _make_measure_eval("m2", status="excluded"),  # should not count in denominator
        _make_measure_eval("m3", status="unsupported"),
    ]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    # effective total = 2 (converted + unsupported), deployable = 1
    assert result.conversion_rate == pytest.approx(50.0, abs=0.1)


def test_conversion_rate_counts_excluded_from_view_against_total():
    """A converted measure excluded from the view (deployed=False) must lower
    the conversion rate and be reported as not_deployed."""
    r = reporter()
    measures = [
        _make_measure_eval("m1", status="converted", deployed=True),
        _make_measure_eval("m2", status="converted", deployed=False),  # excluded from view
    ]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    # 1 of 2 actually in the view -> 50%, not 100%.
    assert result.conversion_rate == pytest.approx(50.0, abs=0.1)
    assert result.deployed == 1
    assert result.not_deployed == 1


def test_evaluate_fact_group_100_percent_when_all_converted():
    r = reporter()
    measures = [_make_measure_eval(f"m{i}", status="converted") for i in range(4)]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    assert result.conversion_rate == pytest.approx(100.0, abs=0.1)


def test_evaluate_fact_group_validation_status_ok_when_no_issues():
    r = reporter()
    measures = [_make_measure_eval()]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    assert result.validation_status == "OK"


def test_evaluate_fact_group_validation_status_errors_when_issues_present():
    r = reporter()
    measures = [_make_measure_eval(issues=["blocking issue"])]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    assert result.validation_status == "ERRORS"


def test_evaluate_fact_group_validation_status_warnings_when_only_warnings():
    r = reporter()
    measures = [_make_measure_eval(warnings=["non-blocking warning"])]
    result = r.evaluate_fact_group("Sales", "FactSales", measures)
    assert result.validation_status == "WARNINGS"


def test_evaluate_fact_group_dims_and_joins_count_stored():
    r = reporter()
    result = r.evaluate_fact_group("Sales", "FactSales", [], dims_count=3, joins_count=2)
    assert result.dimensions_count == 3
    assert result.joins_count == 2


# ── generate_pipeline_summary ─────────────────────────────────────────────────

def _build_group(name, converted, unsupported, partial=0, excluded=0, manual_overrides=0):
    total = converted + unsupported + partial + excluded + manual_overrides
    effective = total - excluded
    deployable = converted + manual_overrides
    rate = (deployable / effective * 100) if effective > 0 else 0.0
    return FactGroupEvaluation(
        name=name,
        source_table=f"Fact{name}",
        total_measures=total,
        converted=converted,
        partial=partial,
        unsupported=unsupported,
        manual_overrides=manual_overrides,
        excluded=excluded,
        conversion_rate=round(rate, 1),
        deployed=deployable,  # all converted/override measures are in-view here
        not_deployed=0,
        measures=[],
    )


def test_generate_pipeline_summary_returns_pipeline_summary():
    r = reporter()
    groups = [_build_group("Sales", 8, 2)]
    summary = r.generate_pipeline_summary(groups, "TestModel", "main", "default", 1.5, "2026-01-01T00:00:00Z")
    assert isinstance(summary, PipelineSummary)


def test_generate_pipeline_summary_total_measures_aggregated():
    r = reporter()
    groups = [_build_group("Sales", 8, 2), _build_group("Finance", 5, 0)]
    summary = r.generate_pipeline_summary(groups, "M", "c", "s", 1.0, "t")
    assert summary.total_measures == 15


def test_generate_pipeline_summary_overall_conversion_rate_correct():
    r = reporter()
    # 8 converted out of 10 effective
    groups = [_build_group("Sales", 8, 2)]
    summary = r.generate_pipeline_summary(groups, "M", "c", "s", 1.0, "t")
    assert summary.overall_conversion_rate == pytest.approx(80.0, abs=0.1)


def test_generate_pipeline_summary_model_name_stored():
    r = reporter()
    summary = r.generate_pipeline_summary([], "My Model", "c", "s", 0.0, "t")
    assert summary.model_name == "My Model"


def test_generate_pipeline_summary_catalog_and_schema_stored():
    r = reporter()
    summary = r.generate_pipeline_summary([], "M", "my_catalog", "my_schema", 0.0, "t")
    assert summary.target_catalog == "my_catalog"
    assert summary.target_schema == "my_schema"


def test_generate_pipeline_summary_duration_stored():
    r = reporter()
    summary = r.generate_pipeline_summary([], "M", "c", "s", 14.25, "t")
    assert summary.duration_seconds == pytest.approx(14.25)


# ── to_text_summary ───────────────────────────────────────────────────────────

def _build_simple_summary():
    rpt = EvaluationReporter()
    group = _build_group("Sales", 8, 2)
    return rpt.generate_pipeline_summary([group], "Test Model", "cat", "sch", 1.0, "2026-01-01T00:00:00Z")


def test_to_text_summary_contains_pipeline_summary_header():
    rpt = reporter()
    text = rpt.to_text_summary(_build_simple_summary())
    assert "PIPELINE SUMMARY" in text


def test_to_text_summary_contains_fact_group_name():
    rpt = reporter()
    text = rpt.to_text_summary(_build_simple_summary())
    assert "Sales" in text


def test_to_text_summary_contains_total_row():
    rpt = reporter()
    text = rpt.to_text_summary(_build_simple_summary())
    assert "TOTAL" in text


def test_to_text_summary_contains_model_name():
    rpt = reporter()
    text = rpt.to_text_summary(_build_simple_summary())
    assert "Test Model" in text


def test_to_text_summary_is_string():
    rpt = reporter()
    text = rpt.to_text_summary(_build_simple_summary())
    assert isinstance(text, str)


# ── to_dict serialisation ─────────────────────────────────────────────────────

def test_to_dict_serialises_measure_evaluation():
    rpt = reporter()
    m = _make_measure_eval()
    d = rpt.to_dict(m)
    assert isinstance(d, dict)
    assert d["name"] == "Revenue"


def test_to_dict_serialises_fact_group():
    rpt = reporter()
    group = _build_group("Sales", 8, 2)
    d = rpt.to_dict(group)
    assert d["name"] == "Sales"
    assert d["total_measures"] == 10


def test_to_dict_serialises_pipeline_summary():
    rpt = reporter()
    summary = _build_simple_summary()
    d = rpt.to_dict(summary)
    assert "total_measures" in d
    assert "overall_conversion_rate" in d


def test_to_dict_serialises_nested_lists():
    rpt = reporter()
    m = _make_measure_eval()
    group = FactGroupEvaluation(
        name="Sales", source_table="FactSales",
        total_measures=1, converted=1, partial=0, unsupported=0,
        manual_overrides=0, excluded=0, conversion_rate=100.0,
        measures=[m],
    )
    d = rpt.to_dict(group)
    assert isinstance(d["measures"], list)
    assert len(d["measures"]) == 1


def test_to_dict_handles_none_values():
    rpt = reporter()
    m = MeasureEvaluation(
        name="x", original_dax="", translated_sql=None,
        status="unsupported", confidence=0,
    )
    d = rpt.to_dict(m)
    assert d["translated_sql"] is None


# ── generate_manifest ─────────────────────────────────────────────────────────

def test_generate_manifest_returns_migration_manifest():
    rpt = reporter()
    summary = _build_simple_summary()
    manifest = rpt.generate_manifest(summary, {}, {})
    assert isinstance(manifest, MigrationManifest)


def test_generate_manifest_has_uuid_id():
    rpt = reporter()
    summary = _build_simple_summary()
    manifest = rpt.generate_manifest(summary, {}, {})
    import uuid
    # Should not raise
    uuid.UUID(manifest.id)


def test_generate_manifest_version_is_2_0():
    rpt = reporter()
    summary = _build_simple_summary()
    manifest = rpt.generate_manifest(summary, {}, {})
    assert manifest.version == "2.0"


def test_generate_manifest_errors_contain_unsupported_measures():
    rpt = reporter()
    m = _make_measure_eval("Broken", status="unsupported", confidence=0)
    group = FactGroupEvaluation(
        name="Sales", source_table="FactSales",
        total_measures=1, converted=0, partial=0, unsupported=1,
        manual_overrides=0, excluded=0, conversion_rate=0.0,
        measures=[m],
    )
    summary = rpt.generate_pipeline_summary([group], "M", "c", "s", 0.0, "t")
    manifest = rpt.generate_manifest(summary, {}, {})
    assert len(manifest.errors) > 0
    assert "Broken" in manifest.errors[0]
