"""
Tests for backend/validator.py

Covers YAML structure validation, SQL expression checks, DDL validation,
residual DAX detection, circular reference detection, and cross-reference checks.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.validator import MetricViewValidator, ValidationResult, ValidationIssue


# ── Helpers ───────────────────────────────────────────────────────────────────

def v():
    return MetricViewValidator()


VALID_YAML = """
version: "1.1"
source: catalog.schema.fact_sales
dimensions:
  - name: region
    expr: source.region
measures:
  - name: total_revenue
    expr: SUM(source.sales_amount)
""".strip()

VALID_YAML_DICT = {
    "version": "1.1",
    "source": "catalog.schema.fact_sales",
    "dimensions": [{"name": "region", "expr": "source.region"}],
    "measures": [{"name": "total_revenue", "expr": "SUM(source.sales_amount)"}],
}


# ── Valid YAML passes ─────────────────────────────────────────────────────────

def test_valid_yaml_returns_valid_true():
    result = v().validate_yaml(VALID_YAML)
    assert result.valid is True


def test_valid_yaml_has_no_errors():
    result = v().validate_yaml(VALID_YAML)
    assert result.errors_count == 0


def test_valid_yaml_dict_returns_valid_true():
    result = v().validate_yaml_dict(VALID_YAML_DICT)
    assert result.valid is True


# ── Missing version ───────────────────────────────────────────────────────────

def test_missing_version_field_returns_error():
    doc = {
        "source": "catalog.schema.table",
        "measures": [{"name": "m1", "expr": "SUM(source.col)"}],
    }
    result = v().validate_yaml_dict(doc)
    assert result.valid is False


def test_wrong_version_produces_error_issue():
    doc = dict(VALID_YAML_DICT)
    doc["version"] = "2.0"
    result = v().validate_yaml_dict(doc)
    categories = [i.category for i in result.issues if i.severity == "error"]
    assert "yaml_structure" in categories


# ── Missing source ────────────────────────────────────────────────────────────

def test_missing_source_field_returns_invalid():
    doc = {"version": "1.1", "measures": [{"name": "m1", "expr": "SUM(source.col)"}]}
    result = v().validate_yaml_dict(doc)
    assert result.valid is False


def test_missing_source_produces_error_message():
    doc = {"version": "1.1", "measures": [{"name": "m1", "expr": "SUM(source.col)"}]}
    result = v().validate_yaml_dict(doc)
    messages = [i.message for i in result.issues]
    assert any("source" in m.lower() for m in messages)


# ── Duplicate measure names ───────────────────────────────────────────────────

def test_duplicate_measure_names_returns_invalid():
    doc = {
        "version": "1.1",
        "source": "catalog.schema.table",
        "measures": [
            {"name": "revenue", "expr": "SUM(source.amount)"},
            {"name": "revenue", "expr": "SUM(source.amount) * 2"},
        ],
    }
    result = v().validate_yaml_dict(doc)
    assert result.valid is False


def test_duplicate_measure_produces_error_issue():
    doc = {
        "version": "1.1",
        "source": "catalog.schema.table",
        "measures": [
            {"name": "revenue", "expr": "SUM(source.amount)"},
            {"name": "revenue", "expr": "SUM(source.amount) * 2"},
        ],
    }
    result = v().validate_yaml_dict(doc)
    assert any("Duplicate" in i.message for i in result.issues if i.severity == "error")


# ── Duplicate join names ──────────────────────────────────────────────────────

def test_duplicate_join_names_returns_invalid():
    doc = {
        "version": "1.1",
        "source": "catalog.schema.fact",
        "joins": [
            {"name": "dim_product", "source": "catalog.schema.dim_product", "on": "source.product_key = dim_product.product_key"},
            {"name": "dim_product", "source": "catalog.schema.dim_product", "on": "source.product_key = dim_product.product_key"},
        ],
        "measures": [],
    }
    result = v().validate_yaml_dict(doc)
    assert result.valid is False


# ── Balanced parentheses ──────────────────────────────────────────────────────

def test_unbalanced_open_paren_produces_error():
    result = v().validate_sql_expression("SUM(source.col", "test")
    assert result.valid is False
    assert any(i.severity == "error" and "paren" in i.message.lower() for i in result.issues)


def test_unbalanced_close_paren_produces_error():
    result = v().validate_sql_expression("SUM(source.col))", "test")
    assert result.valid is False


def test_balanced_parens_no_error():
    result = v().validate_sql_expression("SUM(source.col)", "test")
    paren_errors = [i for i in result.issues if i.severity == "error" and "paren" in i.message.lower()]
    assert len(paren_errors) == 0


# ── CASE/END matching ─────────────────────────────────────────────────────────

def test_mismatched_case_end_produces_error():
    result = v().validate_sql_expression("CASE WHEN x > 0 THEN 1", "test")
    assert any("CASE" in i.message for i in result.issues if i.severity == "error")


def test_matched_case_end_no_error():
    result = v().validate_sql_expression("CASE WHEN x > 0 THEN 1 ELSE 0 END", "test")
    case_errors = [i for i in result.issues if i.severity == "error" and "CASE" in i.message]
    assert len(case_errors) == 0


# ── Residual DAX detection ────────────────────────────────────────────────────

def test_detect_residual_dax_finds_calculate():
    found = v().detect_residual_dax("CALCULATE(SUM(source.amount))")
    assert "CALCULATE" in found


def test_detect_residual_dax_finds_table_column_pattern():
    found = v().detect_residual_dax("FactSales[SalesAmount]")
    assert "Table[Column]" in found


def test_detect_residual_dax_finds_double_ampersand():
    found = v().detect_residual_dax("a > 0 && b < 10")
    assert "&&" in found


def test_detect_residual_dax_finds_double_pipe():
    found = v().detect_residual_dax("a > 0 || b < 10")
    assert "||" in found


def test_detect_residual_dax_clean_sql_returns_empty():
    found = v().detect_residual_dax("SUM(source.col)")
    assert found == []


# ── Circular measure reference detection ─────────────────────────────────────

def test_circular_measure_reference_detected():
    measures = [
        {"name": "A", "expr": "MEASURE(`B`) + 1"},
        {"name": "B", "expr": "MEASURE(`A`) + 1"},
    ]
    result = v().validate_measure_references(measures)
    assert result.valid is False
    assert any("Circular" in i.message for i in result.issues)


def test_non_circular_measure_references_pass():
    measures = [
        {"name": "Revenue", "expr": "SUM(source.amount)"},
        {"name": "Double Revenue", "expr": "MEASURE(`Revenue`) * 2"},
    ]
    result = v().validate_measure_references(measures)
    assert result.valid is True


# ── DDL validation ────────────────────────────────────────────────────────────

VALID_DDL = """
CREATE OR REPLACE VIEW catalog.schema.sales_metrics
WITH METRICS
LANGUAGE YAML
AS $$
version: "1.1"
source: catalog.schema.fact_sales
measures:
  - name: total_revenue
    expr: SUM(source.sales_amount)
$$
""".strip()


def test_valid_ddl_returns_valid_true():
    result = v().validate_ddl(VALID_DDL)
    assert result.valid is True


def test_ddl_missing_create_view_returns_invalid():
    result = v().validate_ddl("WITH METRICS LANGUAGE YAML AS $$ version: 1.1 $$")
    assert result.valid is False


def test_ddl_missing_with_metrics_returns_invalid():
    result = v().validate_ddl("CREATE VIEW x LANGUAGE YAML AS $$ version: '1.1'\nsource: a.b.c\n$$")
    assert result.valid is False


def test_ddl_missing_language_yaml_returns_invalid():
    result = v().validate_ddl("CREATE VIEW x WITH METRICS AS $$ version: '1.1'\nsource: a.b.c\n$$")
    assert result.valid is False


# ── Cross-reference check ─────────────────────────────────────────────────────

def test_undefined_join_alias_produces_warning():
    doc = {
        "version": "1.1",
        "source": "catalog.schema.fact_sales",
        "joins": [],  # no joins defined
        "measures": [
            {"name": "revenue", "expr": "SUM(dim_product.amount)"},  # references undefined alias
        ],
    }
    result = v().validate_yaml_dict(doc)
    warnings = [i for i in result.issues if i.severity == "warning" and "cross_reference" in i.category]
    assert len(warnings) > 0


def test_defined_join_alias_no_cross_ref_warning():
    doc = {
        "version": "1.1",
        "source": "catalog.schema.fact_sales",
        "joins": [
            {"name": "dim_product", "source": "catalog.schema.dim_product", "on": "source.product_key = dim_product.product_key"},
        ],
        "measures": [
            {"name": "revenue", "expr": "SUM(dim_product.amount)"},
        ],
    }
    result = v().validate_yaml_dict(doc)
    cross_ref_warnings = [i for i in result.issues if "cross_reference" in i.category]
    assert len(cross_ref_warnings) == 0


# ── ValidationResult helpers ──────────────────────────────────────────────────

def test_validation_result_to_dict_has_valid_key():
    result = v().validate_yaml(VALID_YAML)
    d = result.to_dict()
    assert "valid" in d


def test_validation_result_to_dict_has_issues_list():
    result = v().validate_yaml(VALID_YAML)
    d = result.to_dict()
    assert isinstance(d["issues"], list)


def test_errors_count_property_counts_errors():
    issues = [
        ValidationIssue("error", "yaml_structure", "msg1"),
        ValidationIssue("warning", "sql_syntax", "msg2"),
        ValidationIssue("error", "ddl", "msg3"),
    ]
    r = ValidationResult(valid=False, issues=issues)
    assert r.errors_count == 2


def test_warnings_count_property_counts_warnings():
    issues = [
        ValidationIssue("error", "yaml_structure", "msg1"),
        ValidationIssue("warning", "sql_syntax", "msg2"),
    ]
    r = ValidationResult(valid=False, issues=issues)
    assert r.warnings_count == 1
