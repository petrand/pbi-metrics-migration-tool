"""
Tests for backend/overrides.py

Covers loading from dict, table/column mapping, exclusions,
extra joins, merge groups, and validation warnings.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.overrides import (
    Overrides,
    OverridesManager,
    JoinOverride,
    MergeGroup,
    MeasureOverride,
    DimensionOverride,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def mgr():
    return OverridesManager()


FULL_OVERRIDES_DICT = {
    "target": {"catalog": "my_catalog", "schema": "my_schema"},
    "table_mappings": {
        "FactSales": "fact_sales",
        "DimProduct": "dim_product",
    },
    "column_mappings": {
        "FactSales": {
            "SalesAmount": "sales_amount",
            "OrderDate": "order_date",
        }
    },
    "extra_joins": [
        {
            "from_table": "FactSales",
            "from_column": "ProductKey",
            "to_table": "DimProduct",
            "to_column": "ProductKey",
            "join_type": "LEFT",
        }
    ],
    "exclude_tables": ["TempTable", "StagingTable"],
    "exclude_measures": ["Debug Measure", "Test KPI"],
    "exclude_columns": {
        "FactSales": ["InternalNote", "LoadTimestamp"],
    },
    "merge_fact_groups": [
        {
            "name": "CombinedSales",
            "source_tables": ["FactSalesRegion1", "FactSalesRegion2"],
            "target_source": "fact_sales_combined",
        }
    ],
    "measure_overrides": {
        "Total Revenue": {
            "expr": "SUM(source.sales_amount)",
            "comment": "Manual override for precision",
        }
    },
    "dimension_overrides": {
        "region": {
            "display_name": "Sales Region",
            "description": "Geographic region for sales",
        }
    },
}


# ── Load from dict ────────────────────────────────────────────────────────────

def test_load_from_dict_returns_overrides():
    result = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert isinstance(result, Overrides)


def test_load_from_dict_empty_returns_empty_overrides():
    result = mgr().load_from_dict({})
    assert isinstance(result, Overrides)
    assert result.table_mappings == {}


def test_load_from_dict_target_catalog_loaded():
    result = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert result.target.catalog == "my_catalog"


def test_load_from_dict_target_schema_loaded():
    result = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert result.target.schema == "my_schema"


# ── Table mapping ─────────────────────────────────────────────────────────────

def test_table_mapping_fact_sales_resolved():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    result = mgr().apply_table_mapping(overrides, "FactSales")
    assert result == "fact_sales"


def test_table_mapping_case_insensitive():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    result = mgr().apply_table_mapping(overrides, "factsales")
    assert result == "fact_sales"


def test_table_mapping_unmapped_returns_lowercased():
    overrides = mgr().load_from_dict({})
    result = mgr().apply_table_mapping(overrides, "MyTable")
    assert result == "mytable"


def test_table_mapping_spaces_converted_to_underscores():
    overrides = mgr().load_from_dict({})
    result = mgr().apply_table_mapping(overrides, "My Table")
    assert "_" in result


# ── Column mapping ────────────────────────────────────────────────────────────

def test_column_mapping_sales_amount_resolved():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    result = mgr().apply_column_mapping(overrides, "FactSales", "SalesAmount")
    assert result == "sales_amount"


def test_column_mapping_case_insensitive():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    result = mgr().apply_column_mapping(overrides, "factsales", "salesamount")
    assert result == "sales_amount"


def test_column_mapping_unmapped_returns_lowercased():
    overrides = mgr().load_from_dict({})
    result = mgr().apply_column_mapping(overrides, "FactSales", "ProductKey")
    assert result == "productkey"


# ── Exclude tables ────────────────────────────────────────────────────────────

def test_should_exclude_temp_table():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_table(overrides, "TempTable") is True


def test_should_not_exclude_fact_sales():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_table(overrides, "FactSales") is False


def test_should_exclude_table_case_insensitive():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_table(overrides, "temptable") is True


# ── Exclude measures ──────────────────────────────────────────────────────────

def test_should_exclude_debug_measure():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_measure(overrides, "Debug Measure") is True


def test_should_not_exclude_total_revenue():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_measure(overrides, "Total Revenue") is False


def test_should_exclude_measure_case_insensitive():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_measure(overrides, "debug measure") is True


# ── Exclude columns ───────────────────────────────────────────────────────────

def test_should_exclude_internal_note_column():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_column(overrides, "FactSales", "InternalNote") is True


def test_should_not_exclude_sales_amount_column():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_column(overrides, "FactSales", "SalesAmount") is False


def test_should_exclude_column_case_insensitive():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    assert mgr().should_exclude_column(overrides, "factsales", "internalnote") is True


# ── Extra joins ───────────────────────────────────────────────────────────────

def test_extra_joins_loaded_correctly():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    joins = mgr().get_extra_joins(overrides)
    assert len(joins) == 1


def test_extra_join_from_table_correct():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    join = mgr().get_extra_joins(overrides)[0]
    assert join.from_table == "FactSales"


def test_extra_join_to_table_correct():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    join = mgr().get_extra_joins(overrides)[0]
    assert join.to_table == "DimProduct"


def test_extra_join_type_default_is_left():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    join = mgr().get_extra_joins(overrides)[0]
    assert join.join_type == "LEFT"


# ── Merge groups ──────────────────────────────────────────────────────────────

def test_merge_groups_loaded_correctly():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    groups = mgr().get_merge_groups(overrides)
    assert len(groups) == 1


def test_merge_group_name_correct():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    group = mgr().get_merge_groups(overrides)[0]
    assert group.name == "CombinedSales"


def test_merge_group_source_tables_count():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    group = mgr().get_merge_groups(overrides)[0]
    assert len(group.source_tables) == 2


def test_merge_group_target_source_correct():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    group = mgr().get_merge_groups(overrides)[0]
    assert group.target_source == "fact_sales_combined"


# ── Measure overrides ─────────────────────────────────────────────────────────

def test_measure_override_resolved_by_name():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    override = mgr().get_measure_override(overrides, "Total Revenue")
    assert override is not None
    assert override.expr == "SUM(source.sales_amount)"


def test_measure_override_case_insensitive_lookup():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    override = mgr().get_measure_override(overrides, "total revenue")
    assert override is not None


def test_measure_override_not_found_returns_none():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    override = mgr().get_measure_override(overrides, "Nonexistent KPI")
    assert override is None


# ── Validate warnings ─────────────────────────────────────────────────────────

def test_validate_no_warnings_for_valid_overrides():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    warnings = mgr().validate(overrides)
    assert warnings == []


def test_validate_warns_when_catalog_set_but_schema_empty():
    data = {"target": {"catalog": "my_catalog", "schema": ""}}
    overrides = mgr().load_from_dict(data)
    warnings = mgr().validate(overrides)
    assert any("schema" in w.lower() for w in warnings)


def test_validate_warns_when_schema_set_but_catalog_empty():
    data = {"target": {"catalog": "", "schema": "my_schema"}}
    overrides = mgr().load_from_dict(data)
    warnings = mgr().validate(overrides)
    assert any("catalog" in w.lower() for w in warnings)


def test_validate_warns_when_merge_group_has_one_source_table():
    data = {
        "merge_fact_groups": [
            {
                "name": "SingleMerge",
                "source_tables": ["FactSales"],  # only 1 — should warn
                "target_source": "fact_sales",
            }
        ]
    }
    overrides = mgr().load_from_dict(data)
    warnings = mgr().validate(overrides)
    assert any("fewer than 2" in w for w in warnings)


def test_validate_warns_when_merge_group_missing_target_source():
    data = {
        "merge_fact_groups": [
            {
                "name": "MissingTarget",
                "source_tables": ["FactSalesA", "FactSalesB"],
                "target_source": "",  # empty
            }
        ]
    }
    overrides = mgr().load_from_dict(data)
    warnings = mgr().validate(overrides)
    assert any("target_source" in w for w in warnings)


# ── to_dict ───────────────────────────────────────────────────────────────────

def test_overrides_to_dict_has_target():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    d = overrides.to_dict()
    assert "target" in d
    assert d["target"]["catalog"] == "my_catalog"


def test_overrides_to_dict_has_table_mappings():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    d = overrides.to_dict()
    assert "table_mappings" in d
    assert d["table_mappings"]["FactSales"] == "fact_sales"


def test_overrides_to_dict_has_exclude_tables():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    d = overrides.to_dict()
    assert "TempTable" in d["exclude_tables"]


def test_overrides_to_dict_has_extra_joins_count():
    overrides = mgr().load_from_dict(FULL_OVERRIDES_DICT)
    d = overrides.to_dict()
    assert d["extra_joins_count"] == 1
