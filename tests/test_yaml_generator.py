"""
Tests for the Metric View YAML generator's current-spec feature coverage:
join rely/cardinality, snowflake nested joins, agent metadata (format,
synonyms, display_name), list-form window measures, and filtered measures.
"""

import yaml as _yaml

from backend.yaml_generator import (
    MetricViewYAMLGenerator,
    MetricViewMeasure,
    MetricViewSpec,
    pbi_format_to_metric_format,
)
from backend.overrides import Overrides, MeasureOverride, DimensionOverride
from backend.validator import MetricViewValidator
from backend.dax_translator import DAXTranslator


def _gen():
    return MetricViewYAMLGenerator()


def _star_model():
    return {
        "name": "Sales",
        "tables": [
            {"name": "FactSales", "measures": [
                {"name": "Total Sales", "expression": "SUM(FactSales[Amount])",
                 "translated_sql": "SUM(source.amount)", "status": "converted",
                 "formatString": "$#,##0.00"},
            ], "columns": []},
            {"name": "DimCustomer", "columns": [
                {"name": "CustomerName", "dataType": "string"},
                {"name": "CustomerKey", "dataType": "int64"},
            ]},
        ],
        "relationships": [
            {"from": "FactSales.CustomerKey", "to": "DimCustomer.CustomerKey",
             "type": "manyToOne"},
        ],
    }


# ── Join rely / cardinality (k9t) ───────────────────────────────────────────

def test_many_to_one_join_emits_rely_at_most_one_match():
    specs = _gen().generate_from_model(_star_model(), "main", "gold")
    _, yaml_str, _ = specs[0]
    doc = _yaml.safe_load(yaml_str)
    join = doc["joins"][0]
    assert join["name"] == "dimcustomer"
    assert join["rely"] == {"at_most_one_match": True}
    assert join["on"] == "dimcustomer.customerkey = source.customerkey"


def test_many_to_many_join_omits_rely():
    model = _star_model()
    model["relationships"][0]["type"] = "manyToMany"
    specs = _gen().generate_from_model(model, "main", "gold")
    _, yaml_str, _ = specs[0]
    doc = _yaml.safe_load(yaml_str)
    assert "rely" not in doc["joins"][0]


# ── Snowflake nested joins (aa6) ─────────────────────────────────────────────

def test_snowflake_relationship_produces_nested_join():
    model = {
        "name": "Sales",
        "tables": [
            {"name": "FactSales", "measures": [
                {"name": "Total", "translated_sql": "SUM(source.amt)",
                 "status": "converted"}], "columns": []},
            {"name": "DimCustomer", "columns": [
                {"name": "CustomerName", "dataType": "string"}]},
            {"name": "DimRegion", "columns": [
                {"name": "RegionName", "dataType": "string"}]},
        ],
        "relationships": [
            {"from": "FactSales.CustomerKey", "to": "DimCustomer.CustomerKey",
             "type": "manyToOne"},
            {"from": "DimCustomer.RegionKey", "to": "DimRegion.RegionKey",
             "type": "manyToOne"},
        ],
    }
    _, yaml_str, _ = _gen().generate_from_model(model, "main", "gold")[0]
    doc = _yaml.safe_load(yaml_str)
    # DimRegion nests under DimCustomer (recursive `joins:` field).
    assert len(doc["joins"]) == 1
    customer = doc["joins"][0]
    assert customer["name"] == "dimcustomer"
    assert "joins" in customer
    region = customer["joins"][0]
    assert region["name"] == "dimregion"
    assert region["on"] == "dimregion.regionkey = dimcustomer.regionkey"
    # The nested dimension is referenced by its dotted path.
    dim_exprs = [d["expr"] for d in doc["dimensions"]]
    assert "dimcustomer.dimregion.regionname" in dim_exprs


# ── Agent metadata: format (d1p) ─────────────────────────────────────────────

def test_currency_format_string_maps_to_currency_type():
    _, yaml_str, _ = _gen().generate_from_model(_star_model(), "main", "gold")[0]
    doc = _yaml.safe_load(yaml_str)
    fmt = doc["measures"][0]["format"]
    assert fmt["type"] == "currency"
    assert fmt["currency_code"] == "USD"


def test_pbi_format_mapping_variants():
    assert pbi_format_to_metric_format("0.00%")["type"] == "percentage"
    assert pbi_format_to_metric_format("$#,##0")["type"] == "currency"
    assert pbi_format_to_metric_format("#,##0")["type"] == "number"
    assert pbi_format_to_metric_format("") is None
    assert pbi_format_to_metric_format("General Date") is None


# ── Agent metadata: synonyms + display_name via overrides (d1p) ──────────────

def test_measure_and_dimension_metadata_from_overrides():
    ov = Overrides()
    ov.measure_overrides["Total Sales"] = MeasureOverride(
        name="Total Sales", expr="", display_name="Revenue",
        synonyms=["sales", "turnover"])
    ov.dimension_overrides["CustomerName"] = DimensionOverride(
        name="CustomerName", display_name="Customer",
        synonyms=["client", "account"])
    _, yaml_str, _ = _gen().generate_from_model(_star_model(), "main", "gold",
                                                overrides=ov)[0]
    doc = _yaml.safe_load(yaml_str)
    measure = doc["measures"][0]
    assert measure["display_name"] == "Revenue"
    assert measure["synonyms"] == ["sales", "turnover"]
    dim = next(d for d in doc["dimensions"] if d["name"] == "CustomerName")
    assert dim["display_name"] == "Customer"
    assert dim["synonyms"] == ["client", "account"]


# ── Window measures rendered as a list (5js) ─────────────────────────────────

def test_window_measure_rendered_as_list():
    spec = MetricViewSpec(source="main.gold.f", view_name="main.gold.f_mv")
    spec.measures.append(MetricViewMeasure(
        name="t7d", expr="SUM(source.value)",
        window={"order": "date", "range": "trailing 7 day",
                "semiadditive": "last", "offset": "-1 day"}))
    yaml_str = _gen().generate_yaml(spec)
    doc = _yaml.safe_load(yaml_str)
    window = doc["measures"][0]["window"]
    assert isinstance(window, list)
    assert window[0]["order"] == "date"
    assert window[0]["range"] == "trailing 7 day"
    assert window[0]["semiadditive"] == "last"
    assert window[0]["offset"] == "-1 day"


def test_legacy_order_by_key_is_mapped_to_order():
    spec = MetricViewSpec(source="main.gold.f", view_name="main.gold.f_mv")
    spec.measures.append(MetricViewMeasure(
        name="ytd", expr="SUM(source.value)",
        window={"order_by": "order_date", "range": "cumulative"}))
    doc = _yaml.safe_load(_gen().generate_yaml(spec))
    window = doc["measures"][0]["window"]
    assert window[0]["order"] == "order_date"


# ── Filtered measure round trip (d5n) ────────────────────────────────────────

def test_calculate_filter_flows_into_valid_metric_view_yaml():
    t = DAXTranslator()
    tr = t.translate(
        "CALCULATE(SUM(FactSales[SalesAmount]), FactSales[Region] = \"West\")",
        "FactSales")
    spec = MetricViewSpec(source="main.gold.fact_sales",
                          view_name="main.gold.fact_sales_metric_view")
    spec.measures.append(MetricViewMeasure(name="West Sales",
                                            expr=tr.translated_sql))
    yaml_str = _gen().generate_yaml(spec)
    assert "FILTER (WHERE" in yaml_str
    # The generated YAML must validate cleanly (no residual-DAX warning on the
    # SQL FILTER(WHERE) clause, no structural errors).
    result = MetricViewValidator().validate_yaml(yaml_str)
    assert result.valid
    residual = [i for i in result.issues if i.category == "residual_dax"]
    assert residual == []


# ── Generated YAML validates end to end ──────────────────────────────────────

def test_generated_star_schema_yaml_validates():
    _, yaml_str, ddl = _gen().generate_from_model(_star_model(), "main", "gold")[0]
    assert MetricViewValidator().validate_yaml(yaml_str).valid
    assert MetricViewValidator().validate_ddl(ddl).valid


# ── Dependency-ordered emission (topological sort of generated views) ─────────

from backend.yaml_generator import MetricViewJoin


def _spec(view_name, source, joins=None):
    return MetricViewSpec(source=source, view_name=view_name, joins=joins or [])


def _results(*specs):
    # DDL text mirrors what generate_ddl emits (source line + view name) so the
    # text-scan dependency path is also exercised.
    return [
        (s, "", f"CREATE OR REPLACE VIEW {s.view_name}\nAS $$\n  source: {s.source}\n$$")
        for s in specs
    ]


def test_topo_orders_dependent_view_after_its_referenced_view():
    """View B whose source IS view A's fully-qualified name must be emitted
    AFTER A (A is created first)."""
    a = _spec("cat.sch.a_metric_view", "cat.sch.a")
    b = _spec("cat.sch.b_metric_view", "cat.sch.a_metric_view")  # B sources A
    ordered = MetricViewYAMLGenerator._topo_order_results(_results(b, a))
    names = [spec.view_name for spec, _y, _d in ordered]
    assert names.index("cat.sch.a_metric_view") < names.index("cat.sch.b_metric_view")


def test_topo_orders_dependency_via_join_source():
    """A dependency expressed through a (nested) join source is respected too."""
    a = _spec("cat.sch.dim_metric_view", "cat.sch.dim")
    b = _spec("cat.sch.fact_metric_view", "cat.sch.fact",
              joins=[MetricViewJoin(name="dim", source="cat.sch.dim_metric_view",
                                    on="dim.k = source.k")])
    ordered = MetricViewYAMLGenerator._topo_order_results(_results(b, a))
    names = [spec.view_name for spec, _y, _d in ordered]
    assert names.index("cat.sch.dim_metric_view") < names.index("cat.sch.fact_metric_view")


def test_topo_stable_for_independent_views():
    """Views that reference only physical tables (no cross-view deps) keep their
    original order — the common case for star-schema fact tables."""
    a = _spec("cat.sch.a_metric_view", "cat.sch.a_fact")
    b = _spec("cat.sch.b_metric_view", "cat.sch.b_fact")
    c = _spec("cat.sch.c_metric_view", "cat.sch.c_fact")
    ordered = MetricViewYAMLGenerator._topo_order_results(_results(a, b, c))
    assert [spec.view_name for spec, _y, _d in ordered] == [
        "cat.sch.a_metric_view", "cat.sch.b_metric_view", "cat.sch.c_metric_view"]


def test_topo_is_cycle_tolerant():
    """A mutual reference must not hang or drop views — fall back to a stable
    order that still contains every view exactly once."""
    a = _spec("cat.sch.a_metric_view", "cat.sch.b_metric_view")  # A -> B
    b = _spec("cat.sch.b_metric_view", "cat.sch.a_metric_view")  # B -> A (cycle)
    ordered = MetricViewYAMLGenerator._topo_order_results(_results(a, b))
    names = sorted(spec.view_name for spec, _y, _d in ordered)
    assert names == ["cat.sch.a_metric_view", "cat.sch.b_metric_view"]


# ── CREATE TABLE IF NOT EXISTS for referenced tables (dependency order) ───────

def _model_with_cols():
    """Star model whose fact table also has physical columns (so it gets a
    CREATE TABLE), for the dependency-table generation tests."""
    m = _star_model()
    m["tables"][0]["columns"] = [
        {"name": "Amount", "dataType": "double"},
        {"name": "CustomerKey", "dataType": "int64"},
    ]
    return m


def test_build_table_entries_emits_create_table_for_referenced_tables():
    gen = _gen()
    model = _model_with_cols()
    views = gen.generate_from_model(model, "main", "gold")
    entries = gen.build_table_entries(views, model["tables"])
    ddls = [ddl for _s, _y, ddl in entries]
    assert ddls, "should emit at least one CREATE TABLE"
    assert all(d.lstrip().upper().startswith("CREATE OR REPLACE TABLE") for d in ddls)
    joined = "\n".join(ddls)
    # Both the fact source and the joined dimension are created.
    assert "main.gold.factsales" in joined
    assert "main.gold.dimcustomer" in joined
    # Table entries carry SQL but no metric-view YAML.
    assert all(y == "" for _s, y, _d in entries)


def test_table_entry_columns_match_view_column_normalization_and_types():
    gen = _gen()
    model = _model_with_cols()
    views = gen.generate_from_model(model, "main", "gold")
    entries = gen.build_table_entries(views, model["tables"])
    ddl_by_tbl = {s.view_name.split(".")[-1]: ddl for s, _y, ddl in entries}
    # DimCustomer: a string column normalized to `customername`, an int64 key -> BIGINT.
    dim = ddl_by_tbl["dimcustomer"]
    assert "customername STRING" in dim
    assert "customerkey BIGINT" in dim  # int64 -> BIGINT


def test_referenced_table_missing_from_model_is_skipped():
    gen = _gen()
    model = _model_with_cols()
    views = gen.generate_from_model(model, "main", "gold")
    # Drop DimCustomer from the model tables passed to build_table_entries.
    tables = [t for t in model["tables"] if t["name"] != "DimCustomer"]
    entries = gen.build_table_entries(views, tables)
    names = {s.view_name.split(".")[-1] for s, _y, _d in entries}
    assert "dimcustomer" not in names  # skipped: not in the provided model tables
    assert "factsales" in names


def test_type_mapping_covers_common_tmdl_types():
    from backend.yaml_generator import _map_sql_type
    assert _map_sql_type("int64") == "BIGINT"
    assert _map_sql_type("double") == "DOUBLE"
    assert _map_sql_type("string") == "STRING"
    assert _map_sql_type("boolean") == "BOOLEAN"
    assert _map_sql_type("dateTime") == "TIMESTAMP"
    assert _map_sql_type("date") == "DATE"
    assert _map_sql_type("decimal").startswith("DECIMAL")
    assert _map_sql_type(None) == "STRING"      # missing -> safe default
    assert _map_sql_type("mystery") == "STRING"  # unknown -> safe default


# ── Aggregate inside FILTER (WHERE ...) must be excluded ──────────────────────

def test_filter_contains_aggregate_detection():
    from backend.yaml_generator import _filter_contains_aggregate
    # Aggregate in the FILTER predicate -> invalid (INVALID_AGGREGATE_FILTER).
    assert _filter_contains_aggregate(
        "COUNT(*) FILTER (WHERE source.k <= max(calendar.key))")
    # Nested / OR'd aggregates still caught.
    assert _filter_contains_aggregate(
        "COUNT(*) FILTER (WHERE (source.a <= max(c.k)) OR (source.b IS NULL))")
    # A clean predicate (no aggregate) is fine.
    assert not _filter_contains_aggregate(
        "COUNT(*) FILTER (WHERE source.endkey IS NULL)")
    # No FILTER at all.
    assert not _filter_contains_aggregate("SUM(source.amt)")


def test_measure_with_aggregate_in_filter_is_excluded():
    """A point-in-time/SCD measure translating to `agg FILTER (WHERE ... max(...))`
    has no valid metric-view equivalent and must be excluded (not emitted, which
    would fail the whole view's deploy with INVALID_AGGREGATE_FILTER)."""
    gen = _gen()
    model = _model_with_cols()
    fact = next(t for t in model["tables"] if t.get("measures"))
    fact["measures"] = [{
        "name": "Historical Count",
        "translated_sql": "COUNT(*) FILTER (WHERE source.addkey <= max(dimdate.datekey))",
        "status": "converted",
    }]
    spec = gen.build_spec(
        model_name=model["name"], fact_table=fact["name"],
        catalog="main", schema="gold",
        tables=model["tables"], relationships=model.get("relationships", []),
        translated_measures=fact["measures"],
    )
    names = [m.name for m in spec.measures]
    assert "Historical Count" not in names
    assert "FILTER (WHERE" in spec.excluded_measures.get("Historical Count", "")
