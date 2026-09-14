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
