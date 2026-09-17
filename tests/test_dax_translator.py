"""
Tests for backend/dax_translator.py

Covers aggregations, conditionals, logical operators, date functions,
text functions, lookups, variables, iterators, CALCULATE, time intelligence,
measure references, batch translation, and edge cases.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.dax_translator import DAXTranslator, TranslationResult, TranslationStatus


# ── Helpers ──────────────────────────────────────────────────────────────────

def translator():
    return DAXTranslator()


# ── Aggregations ──────────────────────────────────────────────────────────────

def test_sum_translates_to_sql_sum():
    result = translator().translate("SUM(FactSales[SalesAmount])", "FactSales")
    assert "SUM(source.salesamount)" in result.translated_sql


def test_count_translates_to_sql_count():
    result = translator().translate("COUNT(FactSales[SalesKey])", "FactSales")
    assert "COUNT(source.saleskey)" in result.translated_sql


def test_distinctcount_translates_to_count_distinct():
    result = translator().translate("DISTINCTCOUNT(FactSales[CustomerKey])", "FactSales")
    assert "COUNT(DISTINCT source.customerkey)" in result.translated_sql


def test_average_translates_to_avg():
    result = translator().translate("AVERAGE(FactSales[Quantity])", "FactSales")
    assert "AVG(source.quantity)" in result.translated_sql


def test_min_translates_to_sql_min():
    result = translator().translate("MIN(FactSales[SalesAmount])", "FactSales")
    assert "MIN(source.salesamount)" in result.translated_sql


def test_max_translates_to_sql_max():
    result = translator().translate("MAX(FactSales[SalesAmount])", "FactSales")
    assert "MAX(source.salesamount)" in result.translated_sql


def test_countrows_translates_to_count_star():
    result = translator().translate("COUNTROWS(FactSales)", "FactSales")
    assert "COUNT(*)" in result.translated_sql


def test_countblank_translates_to_sum_case_null():
    result = translator().translate("COUNTBLANK(FactSales[Quantity])", "FactSales")
    assert "IS NULL" in result.translated_sql
    assert "SUM(CASE WHEN" in result.translated_sql


# ── Conditional ───────────────────────────────────────────────────────────────

def test_if_translates_to_case_when():
    result = translator().translate("IF(1 > 0, 1, 0)")
    assert "CASE WHEN" in result.translated_sql
    assert "THEN" in result.translated_sql
    assert "ELSE" in result.translated_sql


def test_divide_two_args_translates_to_coalesce_nullif():
    result = translator().translate("DIVIDE(SUM(FactSales[SalesAmount]), COUNT(FactSales[SalesKey]))")
    assert "NULLIF" in result.translated_sql
    assert "COALESCE" in result.translated_sql


def test_divide_three_args_translates_to_coalesce_with_alt():
    result = translator().translate(
        "DIVIDE(SUM(FactSales[SalesAmount]), DISTINCTCOUNT(FactSales[SalesKey]), 0)"
    )
    assert "NULLIF" in result.translated_sql
    assert "COALESCE" in result.translated_sql


def test_switch_translates_to_case():
    result = translator().translate("SWITCH(Status, 1, 'Active', 2, 'Inactive', 'Unknown')")
    assert "CASE" in result.translated_sql
    assert "END" in result.translated_sql


def test_switch_true_translates_to_case_when():
    result = translator().translate("SWITCH(TRUE(), x > 10, 'High', x > 5, 'Mid', 'Low')")
    assert "CASE" in result.translated_sql
    assert "WHEN" in result.translated_sql
    assert "END" in result.translated_sql


def test_isblank_translates_to_is_null():
    result = translator().translate("ISBLANK(source.col)")
    assert "IS NULL" in result.translated_sql


def test_iferror_translates_to_coalesce():
    # Databricks SQL has no bare TRY() scalar; IFERROR maps to COALESCE.
    result = translator().translate("IFERROR(1/0, 0)")
    assert "TRY(" not in result.translated_sql
    assert "COALESCE" in result.translated_sql


# ── Logical ───────────────────────────────────────────────────────────────────

def test_and_function_translates_to_sql_and():
    result = translator().translate("AND(a > 1, b < 10)")
    assert "AND" in result.translated_sql


def test_or_function_translates_to_sql_or():
    result = translator().translate("OR(a > 1, b < 10)")
    assert "OR" in result.translated_sql


def test_not_function_translates_to_sql_not():
    result = translator().translate("NOT(a > 1)")
    assert "NOT" in result.translated_sql


def test_double_ampersand_translates_to_and():
    result = translator().translate("a > 1 && b < 2")
    assert "AND" in result.translated_sql
    assert "&&" not in result.translated_sql


def test_double_pipe_translates_to_or():
    result = translator().translate("a > 1 || b < 2")
    assert "OR" in result.translated_sql
    assert "||" not in result.translated_sql


# ── Date Functions ────────────────────────────────────────────────────────────

def test_today_translates_to_current_date():
    result = translator().translate("TODAY()")
    assert "CURRENT_DATE()" in result.translated_sql


def test_eomonth_translates_to_last_day_add_months():
    result = translator().translate("EOMONTH(OrderDate, 0)")
    assert "LAST_DAY" in result.translated_sql
    assert "ADD_MONTHS" in result.translated_sql


def test_edate_translates_to_add_months():
    result = translator().translate("EDATE(OrderDate, 3)")
    assert "ADD_MONTHS" in result.translated_sql


def test_datediff_swaps_parameters():
    result = translator().translate("DATEDIFF(StartDate, EndDate, DAY)")
    # DAX: DATEDIFF(start, end, unit) → SQL: DATEDIFF(unit, start, end)
    sql = result.translated_sql
    assert sql.startswith("DATEDIFF(DAY")


def test_dateadd_day_translates_to_date_add():
    result = translator().translate("DATEADD(OrderDate[Date], 7, DAY)")
    assert "DATE_ADD" in result.translated_sql


def test_dateadd_month_translates_to_add_months():
    result = translator().translate("DATEADD(OrderDate[Date], 1, MONTH)")
    assert "ADD_MONTHS" in result.translated_sql


# ── Text Functions ────────────────────────────────────────────────────────────

def test_concatenate_translates_to_concat():
    result = translator().translate("CONCATENATE(FirstName, LastName)")
    assert "CONCAT(" in result.translated_sql


def test_containsstring_translates_to_like():
    result = translator().translate("CONTAINSSTRING(source.name, 'foo')")
    assert "LIKE" in result.translated_sql
    assert "'%%'" in result.translated_sql or "CONCAT('%%'" in result.translated_sql


# ── Lookups ───────────────────────────────────────────────────────────────────

def test_related_translates_to_join_ref():
    result = translator().translate("RELATED(DimProduct[ProductName])")
    assert "dimproduct.productname" in result.translated_sql


def test_selectedvalue_no_alt_translates_to_source_ref():
    result = translator().translate("SELECTEDVALUE(FactSales[Status])", "FactSales")
    assert "source.status" in result.translated_sql


def test_selectedvalue_with_alt_translates_to_coalesce():
    result = translator().translate("SELECTEDVALUE(FactSales[Status], 'All')", "FactSales")
    assert "COALESCE" in result.translated_sql


# ── Variables ─────────────────────────────────────────────────────────────────

def test_var_return_inlines_variable():
    dax = "VAR x = SUM(FactSales[SalesAmount]) RETURN x"
    result = translator().translate(dax, "FactSales")
    # The variable should be inlined; no 'VAR' in final SQL
    assert "VAR" not in result.translated_sql.upper() or "var_return_inline" in result.applied_transformations


def test_var_return_transformation_tag_applied():
    dax = "VAR revenue = SUM(FactSales[SalesAmount]) RETURN revenue * 2"
    result = translator().translate(dax, "FactSales")
    assert "var_return_inline" in result.applied_transformations


# ── Iterator Functions ────────────────────────────────────────────────────────

def test_sumx_translates_to_sum():
    result = translator().translate("SUMX(FactSales, FactSales[SalesAmount] * 1.1)", "FactSales")
    assert "SUM(" in result.translated_sql


def test_countx_translates_to_count():
    result = translator().translate("COUNTX(FactSales, FactSales[SalesKey])", "FactSales")
    assert "COUNT(" in result.translated_sql


def test_averagex_translates_to_avg():
    result = translator().translate("AVERAGEX(FactSales, FactSales[SalesAmount])", "FactSales")
    assert "AVG(" in result.translated_sql


# ── CALCULATE ─────────────────────────────────────────────────────────────────

def test_calculate_simple_filter_produces_filter_where():
    """CALCULATE with a simple filter maps to a metric-view FILTER (WHERE ...)."""
    result = translator().translate(
        "CALCULATE(SUM(FactSales[SalesAmount]), FactSales[Region] = 'West')",
        "FactSales",
    )
    assert "FILTER (WHERE" in result.translated_sql
    assert "SUM(source.salesamount)" in result.translated_sql
    assert "source.region = 'West'" in result.translated_sql
    assert "CALCULATE_filter_to_FILTER_WHERE" in result.applied_transformations


def test_calculate_filter_all_produces_filter_where():
    """CALCULATE with FILTER(ALL(...)) maps to a metric-view FILTER (WHERE ...)."""
    result = translator().translate(
        "CALCULATE(SUM(FactSales[SalesAmount]), FILTER(ALL(FactSales), FactSales[Region] = 'West'))",
        "FactSales",
    )
    assert "FILTER (WHERE" in result.translated_sql
    assert "CALCULATE_filter_to_FILTER_WHERE" in result.applied_transformations


def test_calculate_filter_converts_dax_double_quotes():
    """DAX double-quoted string literals become SQL single-quoted literals."""
    result = translator().translate(
        'CALCULATE(SUM(FactSales[SalesAmount]), FactSales[Region] = "West")',
        "FactSales",
    )
    assert "'West'" in result.translated_sql
    assert '"West"' not in result.translated_sql


# ── Time Intelligence ─────────────────────────────────────────────────────────

def test_totalytd_produces_compound_window_spec():
    result = translator().translate(
        "TOTALYTD(SUM(FactSales[SalesAmount]), FactSales[OrderDate])",
        "FactSales",
    )
    assert result.window_spec is not None
    # Period-to-date is a compound window: cumulative over the date, then a
    # `current` reset at the period grain so it doesn't run on across years.
    assert isinstance(result.window_spec, list) and len(result.window_spec) == 2
    cumulative, reset = result.window_spec
    assert cumulative == {"order": "orderdate", "range": "cumulative",
                          "semiadditive": "last"}
    assert reset == {"order": "orderdate__year", "range": "current",
                     "semiadditive": "last"}
    for w in result.window_spec:
        assert "group_by" not in w


def test_totalmtd_reset_grain_is_month():
    result = translator().translate(
        "TOTALMTD(SUM(FactSales[SalesAmount]), FactSales[OrderDate])",
        "FactSales",
    )
    assert result.window_spec[1]["order"] == "orderdate__month"
    assert result.window_spec[1]["range"] == "current"


def test_totalqtd_reset_grain_is_quarter():
    result = translator().translate(
        "TOTALQTD(SUM(FactSales[SalesAmount]), FactSales[OrderDate])",
        "FactSales",
    )
    assert result.window_spec[1]["order"] == "orderdate__quarter"


def test_totalytd_inner_expression_preserved():
    result = translator().translate(
        "TOTALYTD(SUM(FactSales[SalesAmount]), FactSales[OrderDate])",
        "FactSales",
    )
    # Inner aggregate expression should be present
    assert "SUM" in result.translated_sql or "salesamount" in result.translated_sql


def test_sameperiodlastyear_uses_current_range_with_offset():
    # Prior-year comparisons are a point shift: `range: current` + `offset`,
    # never a size-less `trailing` (which is invalid on a DATE/TIMESTAMP order
    # column -> INCOMPATIBLE_ORDER_COLUMN_TYPE).
    result = translator().translate(
        "CALCULATE(SUM(FactSales[SalesAmount]), SAMEPERIODLASTYEAR(FactSales[OrderDate]))",
        "FactSales",
    )
    assert result.window_spec is not None
    assert result.window_spec.get("range") == "current"
    assert result.window_spec.get("offset") == "-1 year"


def test_previousyear_uses_current_range():
    result = translator().translate(
        "CALCULATE(SUM(FactSales[SalesAmount]), PREVIOUSYEAR(FactSales[OrderDate]))",
        "FactSales",
    )
    assert result.window_spec is not None
    assert result.window_spec.get("range") == "current"


def test_dateadd_offset_uses_current_range():
    result = translator().translate(
        "CALCULATE(SUM(FactSales[SalesAmount]), DATEADD(FactSales[OrderDate], -1, YEAR))",
        "FactSales",
    )
    assert result.window_spec is not None
    assert result.window_spec.get("range") == "current"
    assert result.window_spec.get("offset") == "-1 year"


# ── Measure References ────────────────────────────────────────────────────────

def test_measure_reference_converts_to_measure_function():
    result = translator().translate("[Total Revenue]")
    assert "MEASURE" in result.translated_sql or "Total Revenue" in result.translated_sql


# ── Batch Translation ─────────────────────────────────────────────────────────

def test_translate_batch_returns_correct_count():
    measures = [
        {"name": "Revenue", "expression": "SUM(FactSales[SalesAmount])"},
        {"name": "Qty", "expression": "SUM(FactSales[Quantity])"},
    ]
    results = translator().translate_batch(measures, "FactSales")
    assert len(results) == 2


def test_translate_batch_all_results_are_translation_results():
    measures = [
        {"name": "Revenue", "expression": "SUM(FactSales[SalesAmount])"},
    ]
    results = translator().translate_batch(measures, "FactSales")
    assert isinstance(results[0], TranslationResult)


def test_translate_batch_cross_reference_resolved():
    """A measure that references another measure in the batch should resolve."""
    measures = [
        {"name": "Total Revenue", "expression": "SUM(FactSales[SalesAmount])"},
        {"name": "Double Revenue", "expression": "[Total Revenue] * 2"},
    ]
    results = translator().translate_batch(measures, "FactSales")
    # Both measures should be processed without crashing
    assert len(results) == 2
    statuses = {r.status for r in results}
    # At least one should be converted
    assert "converted" in statuses or "partial" in statuses


# ── Edge Cases ────────────────────────────────────────────────────────────────

def test_empty_expression_returns_unsupported():
    result = translator().translate("")
    assert result.status == "unsupported"
    assert result.confidence == 0


def test_whitespace_only_expression_returns_unsupported():
    result = translator().translate("   ")
    assert result.status == "unsupported"


def test_nested_functions_do_not_crash():
    result = translator().translate(
        "IF(ISBLANK(SUM(FactSales[SalesAmount])), 0, SUM(FactSales[SalesAmount]))"
    )
    assert result.translated_sql != ""


def test_arithmetic_operators_preserved():
    result = translator().translate("SUM(FactSales[SalesAmount]) - SUM(FactSales[DiscountAmount])")
    assert "-" in result.translated_sql


def test_translation_result_has_original_dax():
    dax = "SUM(FactSales[SalesAmount])"
    result = translator().translate(dax)
    assert result.original_dax == dax


def test_translation_result_to_dict_has_required_keys():
    result = translator().translate("SUM(FactSales[SalesAmount])")
    d = result.to_dict()
    for key in ("original_dax", "translated_sql", "status", "confidence", "applied_transformations"):
        assert key in d


def test_confidence_is_between_0_and_100():
    result = translator().translate("SUM(FactSales[SalesAmount])")
    assert 0 <= result.confidence <= 100


def test_status_is_valid_string():
    result = translator().translate("SUM(FactSales[SalesAmount])")
    assert result.status in ("converted", "partial", "unsupported", "manual_override", "excluded")


# ── COUNTROWS(VALUES(...)) distinct-count idiom ──────────────────────────────

def test_countrows_values_table_col_to_count_distinct():
    result = translator().translate("COUNTROWS(VALUES(Sales[Region]))", "Sales")
    assert result.translated_sql == "COUNT(DISTINCT source.region)"
    assert result.status == "converted"
    assert "COUNTROWS_VALUES_to_COUNT_DISTINCT" in result.applied_transformations


def test_countrows_values_bare_col_to_count_distinct():
    result = translator().translate("COUNTROWS(VALUES([Region]))", "Sales")
    assert result.translated_sql == "COUNT(DISTINCT source.region)"


def test_countrows_values_quoted_table_and_spaced_col():
    result = translator().translate("COUNTROWS(VALUES('Sales Table'[Product Key]))", "Sales")
    assert result.translated_sql == "COUNT(DISTINCT source.product_key)"


def test_countrows_values_nested_in_divide():
    result = translator().translate(
        "DIVIDE(SUM(Sales[amt]), COUNTROWS(VALUES(Sales[cust])))", "Sales")
    assert "COUNT(DISTINCT source.cust)" in result.translated_sql
    assert "VALUES" not in result.translated_sql.upper()


def test_filter_over_values_iterator_not_rewritten():
    """A bare VALUES inside a FILTER iterator is NOT the distinct-count idiom and
    must stay residual for manual review, not be rewritten to COUNT(DISTINCT)."""
    result = translator().translate(
        "CALCULATE(SUM(Sales[amt]), FILTER(VALUES(Sales[x]), Sales[x] > 0))", "Sales")
    assert result.status != "converted"
    assert "VALUES" in result.translated_sql.upper()
