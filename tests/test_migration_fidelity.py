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
    # Period-to-date is a compound window: cumulative over the date + a `current`
    # reset at the period grain (so YTD doesn't run on across years).
    assert isinstance(r.window_spec, list) and len(r.window_spec) == 2
    assert r.window_spec[0].get("range") == "cumulative"
    assert r.window_spec[1].get("range") == "current"
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


# ── Filter/context DAX recovery (epic wo0) ──────────────────────────────────

def test_gate_keeps_valid_filter_where():
    """wo0.1: `agg FILTER (WHERE ...)` is valid SQL and must NOT be excluded as
    residual DAX (only a DAX FILTER( not followed by WHERE is residual)."""
    from backend.yaml_generator import _has_residual_dax
    assert not _has_residual_dax("COUNT(DISTINCT source.c) FILTER (WHERE source.x = 0)")
    assert not _has_residual_dax("MEASURE(`Base`) FILTER (WHERE source.flag = 1)")
    assert _has_residual_dax("SUM(source.a) FILTER (source.x, source.y)")  # DAX FILTER(


def test_gate_keeps_filter_where_measure_in_view():
    """A measure translating to `... FILTER (WHERE ...)` reaches the view."""
    spec = _spec_with([
        {"name": "Active", "translated_sql": "COUNT(DISTINCT source.custkey) FILTER (WHERE source.care = 0)"},
    ])
    assert "Active" in [m.name for m in spec.measures]
    assert "Active" not in spec.excluded_measures


def test_calculate_bare_table_filter():
    """wo0.2: FILTER('<table>', cond) (no ALL) becomes FILTER (WHERE cond)."""
    t = DAXTranslator()
    r = t.translate("CALCULATE(SUM(Sales[amt]), FILTER('Sales', Sales[flag] = 1))", "Sales")
    sql = r.translated_sql
    assert "FILTER (WHERE" in sql
    # No leftover nested DAX FILTER( inside the WHERE clause.
    assert "FILTER (WHERE FILTER" not in sql
    assert "source.flag = 1" in sql


def test_calculate_non_equality_operator():
    """wo0.3: comparison operators other than `=` are recognised as filters."""
    t = DAXTranslator()
    r = t.translate("CALCULATE(SUM(Sales[qty]), Sales[qty] > 0)", "Sales")
    assert "FILTER (WHERE" in r.translated_sql and "source.qty > 0" in r.translated_sql


def test_calculate_blank_comparison_to_null():
    """wo0.3: `<> blank()` maps to `IS NOT NULL`."""
    t = DAXTranslator()
    r = t.translate("CALCULATE(AVERAGE(Sales[e]), FILTER('Sales', Sales[e] <> blank()))", "Sales")
    assert "IS NOT NULL" in r.translated_sql.upper()
    assert "BLANK" not in r.translated_sql.upper()


def test_calculate_multi_predicate_anded():
    """wo0.4: every filter predicate is kept and AND-ed — none silently dropped."""
    t = DAXTranslator()
    r = t.translate(
        'CALCULATE(DISTINCTCOUNT(Sales[inv]), Sales[type] = "I", Sales[care] = 0, Sales[ratted] = 0)',
        "Sales",
    )
    sql = r.translated_sql
    assert "COUNT(DISTINCT source.inv)" in sql
    for frag in ("source.type = 'I'", "source.care = 0", "source.ratted = 0"):
        assert frag in sql, f"dropped predicate: {frag}"
    assert sql.upper().count(" AND ") == 2


def test_calculate_userelationship_dropped_with_warning():
    """wo0.6: USERELATIONSHIP is dropped (with a warning), other predicates kept."""
    t = DAXTranslator()
    r = t.translate(
        "CALCULATE(COUNTROWS('Sales'), Sales[k] <> -1, USERELATIONSHIP(Calendar[ck], Sales[dk]))",
        "Sales",
    )
    sql = r.translated_sql
    assert "USERELATIONSHIP" not in sql.upper()
    assert "COUNT(*) FILTER (WHERE" in sql and "source.k <> -1" in sql
    assert any("USERELATIONSHIP" in w for w in r.warnings)


def test_qualified_measure_ref_resolves_to_measure():
    """wo0.5: 'Table'[Measure] resolves to MEASURE(`...`), not a fact column."""
    t = DAXTranslator()
    r = t.translate("CALCULATE('Sales Budget'[Bgt Daily Sales], DATESMTD('Calendar'[Date]))",
                    "Sales Budget", measures={"Bgt Daily Sales": "SUM(source.x)"})
    assert "MEASURE(`Bgt Daily Sales`)" in r.translated_sql
    assert "source.bgt_daily_sales" not in r.translated_sql


def test_filter_over_values_not_a_plain_filter():
    """FILTER(VALUES(...), cond) is an iteration, not a row filter — left for
    manual review (kept partial), not silently rewritten."""
    t = DAXTranslator()
    r = t.translate("CALCULATE(SUM(Sales[amt]), FILTER(VALUES(Sales[x]), Sales[x] > 0))", "Sales")
    assert r.status != "converted"


# ── Schema-aware pruning (wo0.9) + join scoping (wo0.10) ─────────────────────

def test_prune_measure_referencing_missing_column():
    """With known_columns, a measure referencing a column absent from the target
    table is excluded (with a reason), not emitted into a view that won't deploy."""
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "main", "s",
        tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[],
        translated_measures=[
            {"name": "Good", "translated_sql": "SUM(source.amt)"},
            {"name": "Bad", "translated_sql": "SUM(source.ghost)"},
        ],
        known_columns={"sales": {"amt"}},
    )
    names = [m.name for m in spec.measures]
    assert "Good" in names and "Bad" not in names
    assert "ghost" in spec.excluded_measures["Bad"]


def test_measure_referencing_undeclared_alias_is_excluded():
    """A measure whose expr references an alias that is neither `source` nor a
    declared join in this view (here `dim`, with no relationship) cannot resolve
    at deploy (UNRESOLVED_COLUMN on the alias), so it is excluded — while a
    measure using only `source` columns is kept."""
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "main", "s",
        tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[],
        translated_measures=[
            {"name": "X", "translated_sql": "SUM(source.amt) FILTER (WHERE dim.flag = 1)"},
            {"name": "OK", "translated_sql": "SUM(source.amt)"},
        ],
        known_columns={"sales": {"amt", "flag"}},
    )
    names = [m.name for m in spec.measures]
    assert "X" not in names        # references undeclared alias `dim`
    assert "OK" in names           # only `source` -> valid
    assert "not joined in this view" in spec.excluded_measures.get("X", "")


def test_source_schema_separates_source_from_view():
    """source_schema routes the source/join FQNs to a different schema than the
    view itself (raw vs semantic split)."""
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "cat", "views",
        tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[],
        translated_measures=[{"name": "T", "translated_sql": "SUM(source.amt)"}],
        source_schema="raw",
    )
    assert spec.source == "cat.raw.sales"
    assert spec.view_name == "cat.views.sales_metric_view"


def test_dim_schema_routes_dimension_joins_separately():
    """Dimension (no-DAX) join targets can live in a different schema than the
    fact source tables — fact from source_schema, dim join from dim_schema."""
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "cat", "views",
        tables=[{"name": "Sales", "columns": [], "measures": []},
                {"name": "Customer", "columns": [{"name": "Name", "dataType": "string"}], "measures": []}],
        relationships=[{"from": "Sales.custkey", "to": "Customer.custkey", "type": "manyToOne"}],
        translated_measures=[{"name": "T", "translated_sql": "SUM(source.amt)"}],
        source_schema="raw", dim_schema="sem", dim_tables={"Customer"},
    )
    assert spec.source == "cat.raw.sales"                 # fact from source_schema
    cust = next(j for j in spec.joins if j.name == "customer")
    assert cust.source == "cat.sem.customer"              # dim join from dim_schema


def test_join_direct_fact_edge_wins_over_indirect():
    """wo0.10: a dimension related to several facts must join rooted at the
    CURRENT fact (direct edge), not an earlier indirect relationship."""
    gen = MetricViewYAMLGenerator()
    rels = [
        {"from": "OtherFact.dimkey", "to": "Dim.dimkey", "type": "manyToOne"},  # indirect, seen first
        {"from": "Sales.dimkey", "to": "Dim.dimkey", "type": "manyToOne"},      # direct from current fact
    ]
    spec = gen.build_spec(
        "M", "Sales", "main", "s",
        tables=[{"name": "Sales", "columns": [], "measures": []},
                {"name": "Dim", "columns": [{"name": "Label", "dataType": "string"}], "measures": []}],
        relationships=rels,
        translated_measures=[{"name": "T", "translated_sql": "SUM(source.amt) FILTER (WHERE dim.label = 'x')"}],
    )
    join_names = [j.name for j in spec.joins]
    assert "dim" in join_names, f"direct fact->dim join missing: {join_names}"
    dim_join = next(j for j in spec.joins if j.name == "dim")
    assert dim_join.on == "dim.dimkey = source.dimkey"


# ── In-measure RLS handling ──────────────────────────────────────────────────

def test_rls_gate_stripped_and_flagged():
    """A permission-gate IF over a security measure is reduced to its then-branch
    and the measure is flagged rls_applied (RLS enforced via row filters instead)."""
    t = DAXTranslator(security_tables=["Measure Security"],
                      security_measures=["Count Measures E"])
    r = t.translate(
        "IF([Count Measures E] > 0, CALCULATE(SUM([CostE]), 'Measure Security'), BLANK())",
        "Sales",
    )
    assert r.rls_applied is True
    assert r.translated_sql == "SUM(source.coste)"
    assert "CALCULATE" not in r.translated_sql.upper()
    assert any("RLS" in w for w in r.warnings)


def test_security_table_filter_arg_dropped_and_flagged():
    """A bare security-table CALCULATE filter arg is dropped and flags RLS."""
    t = DAXTranslator(security_tables=["Measure Security"])
    r = t.translate("CALCULATE(SUM([CostE]), 'Measure Security')", "Sales")
    assert r.translated_sql == "SUM(source.coste)"
    assert r.rls_applied is True


def test_non_security_table_filter_arg_dropped_silently():
    """A bare NON-security table filter arg (context transition) is dropped but
    NOT flagged as RLS."""
    t = DAXTranslator()
    r = t.translate("CALCULATE(DISTINCTCOUNT(Customer[Customer Name]), Sales)", "Sales")
    assert "COUNT(DISTINCT" in r.translated_sql.upper()
    assert "CALCULATE" not in r.translated_sql.upper()
    assert r.rls_applied is False


def test_non_rls_if_untouched():
    """A plain IF (not gating on a security measure) is left for the normal
    conditional pass — it still becomes a CASE and is not flagged rls_applied."""
    t = DAXTranslator(security_measures=["Count Measures E"])
    r = t.translate("IF([Requested] = 0, BLANK(), SUM(Sales[difot]))", "Sales")
    assert "CASE WHEN" in r.translated_sql.upper()
    assert r.rls_applied is False


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


def test_generator_pushes_down_window_over_window():
    """A prior-period-of-windowed measure (LY of MTD) is rebuilt via offset
    pushdown — kept, not excluded — with an offset copy of the windowed leaf."""
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "main", "s", tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[],
        translated_measures=[
            {"name": "MTD", "translated_sql": "SUM(source.amt)", "window": [{"order": "d", "range": "cumulative"}]},
            {"name": "LY MTD", "translated_sql": "MEASURE(`MTD`)", "window": [{"order": "d", "range": "trailing", "offset": "-1 year"}]},
        ],
    )
    by_name = {m.name: m for m in spec.measures}
    assert "LY MTD" not in spec.excluded_measures            # no longer excluded
    ly = by_name["LY MTD"]
    assert ly.window is None                                 # now a windowless composition
    off = by_name["MTD__off_1_year"]                         # offset copy of the leaf
    assert off.expr == "SUM(source.amt)"
    assert any(w.get("offset") == "-1 year" for w in off.window)
    assert "MTD__off_1_year" in ly.expr
    assert any("offset pushdown" in w for w in spec.build_warnings)


def _spec_with(measures):
    return MetricViewYAMLGenerator().build_spec(
        "M", "Sales", "main", "s", tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[], translated_measures=measures)


def test_pushdown_additive_prior_period():
    """SPLY of a simple additive windowed measure is kept via pushdown."""
    spec = _spec_with([
        {"name": "MTD Sales", "translated_sql": "SUM(source.amt)", "window": [{"order": "d", "range": "cumulative"}]},
        {"name": "SPLY MTD Sales", "translated_sql": "MEASURE(`MTD Sales`)", "window": [{"order": "d", "range": "trailing", "offset": "-1 year"}]},
    ])
    names = [m.name for m in spec.measures]
    assert "SPLY MTD Sales" in names and "SPLY MTD Sales" not in spec.excluded_measures
    assert "MTD Sales__off_1_year" in names


def test_pushdown_dedups_shared_offset_leaf():
    """Two prior-period measures over the same leaf share one offset copy."""
    spec = _spec_with([
        {"name": "MTD", "translated_sql": "SUM(source.amt)", "window": [{"order": "d", "range": "cumulative"}]},
        {"name": "LY A", "translated_sql": "MEASURE(`MTD`)", "window": [{"order": "d", "range": "trailing", "offset": "-1 year"}]},
        {"name": "LY B", "translated_sql": "MEASURE(`MTD`) * 2", "window": [{"order": "d", "range": "trailing", "offset": "-1 year"}]},
    ])
    off_copies = [m.name for m in spec.measures if m.name.startswith("MTD__off_")]
    assert off_copies == ["MTD__off_1_year"]  # created once, shared


def test_depends_on_excluded_names_the_dependency():
    """A measure dropped for referencing an excluded measure names which one."""
    spec = _spec_with([
        {"name": "Daily GP %", "translated_sql": "CALCULATE(SUM(source.gp), VALUES(source.x))"},  # residual -> excluded
        {"name": "MTD GP %", "translated_sql": "MEASURE(`Daily GP %`)", "window": [{"order": "d", "range": "cumulative"}]},
    ])
    assert spec.excluded_measures["MTD GP %"].endswith("Daily GP %")
    assert "references a measure that was excluded" in spec.excluded_measures["MTD GP %"]


def test_pushdown_disabled_by_option():
    """With convert_nested_windows=False, prior-period measures are excluded
    (window-over-window) rather than rewritten."""
    gen = MetricViewYAMLGenerator()
    tms = [
        {"name": "MTD", "translated_sql": "SUM(source.amt)", "window": [{"order": "d", "range": "cumulative"}]},
        {"name": "LY MTD", "translated_sql": "MEASURE(`MTD`)", "window": [{"order": "d", "range": "trailing", "offset": "-1 year"}]},
    ]
    spec = gen.build_spec("M", "Sales", "main", "s",
        tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[], translated_measures=tms, convert_nested_windows=False)
    assert "LY MTD" in spec.excluded_measures
    assert not any(m.name.startswith("MTD__off_") for m in spec.measures)


def test_pushdown_aborts_on_windowed_composition():
    """A prior-period over a windowed *composition* can't be pushed down and is
    still excluded (falls back to the window-over-window rule)."""
    spec = _spec_with([
        {"name": "Base", "translated_sql": "SUM(source.amt)", "window": [{"order": "d", "range": "cumulative"}]},
        {"name": "WComp", "translated_sql": "MEASURE(`Base`)", "window": [{"order": "d", "range": "cumulative"}]},
        {"name": "PP", "translated_sql": "MEASURE(`WComp`)", "window": [{"order": "d", "range": "trailing", "offset": "-1 year"}]},
    ])
    assert "PP" in spec.excluded_measures
    assert "PP" not in [m.name for m in spec.measures]


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


# ── E6: no measure is dropped without a warning/error ───────────────────────

def _run(tables, relationships=None):
    model = {"name": "M", "tables": tables, "relationships": relationships or []}
    return MigrationPipeline().run(model, MigrationConfig(catalog="main", schema="s"))


def test_build_spec_records_upstream_excluded_measure():
    """A measure marked excluded upstream is recorded, not silently skipped."""
    gen = MetricViewYAMLGenerator()
    spec = gen.build_spec(
        "M", "Sales", "main", "s",
        tables=[{"name": "Sales", "columns": [], "measures": []}],
        relationships=[],
        translated_measures=[
            {"name": "Skipped", "translated_sql": "SUM(source.amt)", "status": "excluded"},
        ],
    )
    assert "Skipped" not in [m.name for m in spec.measures]
    assert "Skipped" in spec.excluded_measures


def test_generator_retains_skipped_view_exclusions():
    """When a whole fact group's view is skipped (empty), its exclusions survive."""
    tables = [{"name": "Weird", "columns": [],
               "measures": [{"name": "Ratio", "expression": "Weird[a] + Weird[b]"}]}]
    gen = MetricViewYAMLGenerator()
    specs = gen.generate_from_model(_model(tables, []), "main", "s")
    assert specs == []                       # empty view -> not emitted
    assert "weird" in gen.skipped_exclusions  # ...but the drop is retained
    assert "Ratio" in gen.skipped_exclusions["weird"]


def test_pipeline_surfaces_excluded_measure_as_warning():
    """A residual-DAX measure is excluded from the view AND surfaced as a warning."""
    tables = [{"name": "Sales", "columns": [], "measures": [
        {"name": "Good", "expression": "SUM(Sales[amt])"},
        {"name": "Bad", "expression": "CALCULATE(SUM(Sales[amt]), VALUES(Sales[x]))"},
    ]}]
    res = _run(tables)
    assert res.status == "complete"
    assert any("Bad" in w for w in res.warnings), res.warnings
    assert not res.errors                    # backstop must not fire for a clean run


def test_pipeline_empty_view_group_not_silent():
    """Every measure of a fully-excluded (skipped) fact group is still reported."""
    tables = [{"name": "Weird", "columns": [],
               "measures": [{"name": "Ratio", "expression": "Weird[a] + Weird[b]"}]}]
    res = _run(tables)
    assert any("Ratio" in w for w in res.warnings), res.warnings


def test_pipeline_no_measure_dropped_without_report():
    """Invariant: every input measure is either in a deployed view or reported."""
    tables = [{"name": "Sales", "columns": [{"name": "Region", "dataType": "string"}],
               "measures": [
                   {"name": "Total", "expression": "SUM(Sales[amt])"},                     # deployed
                   {"name": "Resid", "expression": "CALCULATE(SUM(Sales[amt]), VALUES(Sales[x]))"},  # excluded
                   {"name": "Bare", "expression": "Sales[a] + Sales[b]"},                   # non-aggregating
               ]}]
    res = _run(tables)
    reported = " ".join(res.warnings + res.errors)
    deployed_yaml = " ".join(res.generated_yaml.values())
    for name in ("Total", "Resid", "Bare"):
        assert name in deployed_yaml or name in reported, f"{name} vanished silently"


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
