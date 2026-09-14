"""Regression tests for the E1-E5 migration-fidelity fixes.

Covers:
  E1  dimension scoping to reachable joins + duplicate-name disambiguation
  E2  YAML quoting of name/expr, balanced IF->CASE, keyword-guarded refs
  E3  time-intelligence -> window specs (CALCULATE wrapper stripped)
  E5  distinct relationship count in the pipeline summary
Plus an end-to-end assertion against tmdl_code/SSAS_Sales.
"""

import os

import pytest

from backend.dax_translator import DAXTranslator
from backend.evaluator import EvaluationReporter
from backend.pipeline import MigrationConfig, MigrationPipeline
from backend.tmdl_parser import TMDLParser
from backend.validator import MetricViewValidator
from backend.yaml_generator import MetricViewYAMLGenerator, _yaml_scalar

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None

SSAS_DIR = os.path.join(os.path.dirname(__file__), "..", "tmdl_code", "SSAS_Sales")


# ── E1: dimension scoping & de-duplication ──────────────────────────────────

def _model(tables, relationships):
    return {"name": "M", "tables": tables, "relationships": relationships}


def test_e1_dimensions_scoped_to_reachable_joins():
    """A dim reachable only from another fact must not leak into this fact's view."""
    tables = [
        {"name": "Sales", "columns": [], "measures": [{"name": "Amt", "expression": "SUM(Sales[amt])"}]},
        {"name": "Product", "columns": [{"name": "Cat", "dataType": "string"}], "measures": []},
        {"name": "Clinic", "columns": [{"name": "Ward", "dataType": "string"}], "measures": []},
    ]
    # Sales joins Product; Clinic relates to a different (absent) fact only.
    rels = [
        {"from": "Sales.productkey", "to": "Product.productkey", "type": "manyToOne"},
        {"from": "Visit.clinickey", "to": "Clinic.clinickey", "type": "manyToOne"},
    ]
    gen = MetricViewYAMLGenerator()
    specs = gen.generate_from_model(_model(tables, rels), "main", "s")
    sales_spec = next(s for s, _, _ in specs if s.source.endswith(".sales"))
    exprs = [d.expr for d in sales_spec.dimensions]
    assert any(e.startswith("product.") for e in exprs)
    # Clinic is not reachable from Sales -> no clinic.* dimension.
    assert not any(e.startswith("clinic.") for e in exprs)


def test_e1_duplicate_dimension_names_disambiguated():
    tables = [
        {"name": "Sales", "columns": [], "measures": [{"name": "Amt", "expression": "SUM(Sales[amt])"}]},
        {"name": "Customer", "columns": [{"name": "Name", "dataType": "string"}], "measures": []},
        {"name": "Store", "columns": [{"name": "Name", "dataType": "string"}], "measures": []},
    ]
    rels = [
        {"from": "Sales.custkey", "to": "Customer.custkey", "type": "manyToOne"},
        {"from": "Sales.storekey", "to": "Store.storekey", "type": "manyToOne"},
    ]
    gen = MetricViewYAMLGenerator()
    specs = gen.generate_from_model(_model(tables, rels), "main", "s")
    spec = next(s for s, _, _ in specs if s.source.endswith(".sales"))
    names = [d.name for d in spec.dimensions]
    assert len(names) == len(set(names)), f"duplicate dimension names: {names}"


# ── E2: YAML quoting & IF translation ───────────────────────────────────────

@pytest.mark.parametrize("value", [
    "MEASURE(`Total Sales`)",   # backtick
    "MTD GP %",                 # percent
    "line1\nline2",             # embedded newline (multi-line DAX)
    "a: b",                     # colon-space
    "$ revenue",                # leading currency
])
def test_e2_yaml_scalar_roundtrips(value):
    if _yaml is None:
        pytest.skip("PyYAML not available")
    rendered = _yaml_scalar(value)
    loaded = _yaml.safe_load(f"k: {rendered}")
    assert loaded["k"] == " ".join(value.split())


def test_e2_if_balanced_case_end():
    t = DAXTranslator()
    r = t.translate("IF([Requested]=0, BLANK(), SUM(Sales[difot]))", "Sales")
    sql_up = r.translated_sql.upper()
    assert sql_up.count("CASE") == sql_up.count("END") == 1
    assert "BLANK()" not in r.translated_sql  # -> NULL
    # The bracket after WHEN must not be mangled into a `case_when.` table ref.
    assert "case_when" not in r.translated_sql.lower()


def test_e2_nested_if():
    t = DAXTranslator()
    r = t.translate("IF(A[x]>0, 1, IF(A[y]>0, 2, 3))", "A")
    up = r.translated_sql.upper()
    assert up.count("CASE") == up.count("END") == 2


# ── E3: time-intelligence -> window ─────────────────────────────────────────

def test_e3_datesytd_to_cumulative_window():
    t = DAXTranslator()
    r = t.translate('CALCULATE([Daily Sales], DATESYTD(Calendar[Date], "30-06"))', "Sales")
    assert r.window_spec and r.window_spec.get("range") == "cumulative"
    assert "CALCULATE" not in r.translated_sql.upper()


def test_e3_sameperiodlastyear_to_offset_window():
    t = DAXTranslator()
    r = t.translate("CALCULATE([MTD Sales], SAMEPERIODLASTYEAR(Calendar[Date]))", "Sales")
    assert r.window_spec and "offset" in r.window_spec
    assert "SAMEPERIODLASTYEAR" not in r.translated_sql.upper()
    assert "CALCULATE" not in r.translated_sql.upper()


# ── E5: distinct relationship count ─────────────────────────────────────────

def test_e5_distinct_relationship_count():
    rep = EvaluationReporter()
    fg = rep.evaluate_fact_group("Sales", "main.s.sales", [], dims_count=0, joins_count=5)
    fg2 = rep.evaluate_fact_group("Orders", "main.s.orders", [], dims_count=0, joins_count=5)
    summary = rep.generate_pipeline_summary(
        [fg, fg2], "M", "main", "s", duration=0.1, started_at="t",
        total_relationships=5,
    )
    # Distinct model relationships (5), not the per-fact-group sum (10).
    assert summary.total_relationships == 5


def test_e5_confidence_reflects_status():
    """Lock in that partial translations score below fully-converted ones."""
    t = DAXTranslator()
    converted = t.translate("SUM(Sales[amt])", "Sales")
    partial = t.translate("CALCULATE(SUM(Sales[amt]), FILTER(VALUES(Sales[x]), Sales[x]>0))", "Sales")
    assert converted.confidence > partial.confidence
    assert converted.status == "converted"


# ── Deploy-hardening fixes (found deploying to a live workspace) ─────────────

def test_offset_window_has_semiadditive():
    t = DAXTranslator()
    r = t.translate("CALCULATE([MTD Sales], SAMEPERIODLASTYEAR(Calendar[Date]))", "Sales")
    assert r.window_spec.get("semiadditive")  # required by the MV parser


def test_single_arg_calculate_unwrapped():
    t = DAXTranslator()
    r = t.translate("CALCULATE(DISTINCTCOUNT(Sales[inv]))", "Sales")
    assert "CALCULATE" not in r.translated_sql.upper()
    assert "COUNT(DISTINCT" in r.translated_sql.upper()


def test_code_fences_stripped():
    t = DAXTranslator()
    r = t.translate("``` SUM(Sales[amt]) ```", "Sales")
    assert "`" not in r.translated_sql


def test_generator_excludes_residual_dax_measures():
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "main", "s", tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[],
        translated_measures=[
            {"name": "Good", "translated_sql": "SUM(source.amt)"},
            {"name": "Bad", "translated_sql": "CALCULATE(SUM(source.amt), VALUES(source.x))"},
        ],
    )
    names = [m.name for m in spec.measures]
    assert "Good" in names and "Bad" not in names
    assert "Bad" in spec.excluded_measures  # {name: reason}


def test_generator_excludes_window_over_window():
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "main", "s", tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[],
        translated_measures=[
            {"name": "MTD", "translated_sql": "SUM(source.amt)", "window": [{"order": "d", "range": "cumulative"}]},
            {"name": "LY MTD", "translated_sql": "MEASURE(`MTD`)", "window": [{"order": "d", "range": "trailing", "offset": "-1 year"}]},
        ],
    )
    names = [m.name for m in spec.measures]
    assert "MTD" in names and "LY MTD" not in names


# ── E4: row-level security ──────────────────────────────────────────────────

def test_e4_parses_roles_and_rls():
    if not os.path.isdir(SSAS_DIR):
        pytest.skip("SSAS_Sales fixture absent")
    model = TMDLParser().parse(SSAS_DIR).to_dict()
    roles = model.get("roles", [])
    assert roles, "roles/ should be parsed"
    readers = next((r for r in roles if r["name"] == "Readers"), None)
    assert readers and "Branch" in readers["tablePermissions"]
    assert "USERNAME()" in readers["tablePermissions"]["Branch"]


def test_e4_scaffolding_and_notes():
    from backend import rls_generator
    roles = [{
        "name": "Readers", "modelPermission": "read",
        "tablePermissions": {"Branch": "[BranchSkey]=1"},
        "members": ["DOMAIN\\Grp"],
    }]
    assert rls_generator.rls_notes(roles)
    sql = rls_generator.generate_rls_scaffolding(roles, "main", "s")
    assert "SET ROW FILTER" in sql and "DAX predicate: [BranchSkey]=1" in sql


def test_e4_pipeline_surfaces_rls():
    if not os.path.isdir(SSAS_DIR):
        pytest.skip("SSAS_Sales fixture absent")
    model = TMDLParser().parse(SSAS_DIR).to_dict()
    res = MigrationPipeline().run(model, MigrationConfig(catalog="main", schema="ssas_sales"))
    assert res.rls_notes, "RLS presence must be surfaced, not silently dropped"
    assert "SET ROW FILTER" in res.rls_scaffolding


# ── E1.3: end-to-end SSAS assertion ─────────────────────────────────────────

@pytest.mark.skipif(not os.path.isdir(SSAS_DIR), reason="SSAS_Sales fixture absent")
def test_ssas_end_to_end_no_errors_and_clean_dims():
    model = TMDLParser().parse(SSAS_DIR).to_dict()
    res = MigrationPipeline().run(model, MigrationConfig(catalog="main", schema="ssas_sales"))
    assert res.status == "complete"
    assert res.validation_result["errors"] == 0
    # Dimension scoping + de-dup: no undefined-alias or duplicate-name issues
    # should originate from dimensions[...] locations.
    v = MetricViewValidator()
    for ddl in res.generated_sql.values():
        for i in v.validate_ddl(ddl).issues:
            loc = i.location or ""
            if loc.startswith("dimensions"):
                assert i.category != "cross_reference", f"dim undefined alias: {i.message}"
                assert "Duplicate dimension" not in i.message
    # Distinct relationship count is reported, not the inflated per-group sum.
    assert res.pipeline_summary["total_relationships"] == len(model["relationships"])
