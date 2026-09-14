"""
Shared pytest fixtures for pbi-metrics-migration-tool tests.
"""
import os
import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolated_migration_store():
    """Persist migration records to a throwaway dir so tests don't pollute the
    repo's data/ directory."""
    with tempfile.TemporaryDirectory() as d:
        os.environ["MIGRATION_STORE_DIR"] = d
        yield


@pytest.fixture
def sample_model():
    """Sales Analytics sample Power BI model."""
    return {
        "id": "sales",
        "name": "Sales Analytics",
        "tables": [
            {
                "name": "FactSales",
                "columns": [
                    {"name": "SalesKey", "dataType": "int64"},
                    {"name": "OrderDate", "dataType": "dateTime"},
                    {"name": "ProductKey", "dataType": "int64"},
                    {"name": "CustomerKey", "dataType": "int64"},
                    {"name": "SalesAmount", "dataType": "decimal"},
                    {"name": "Quantity", "dataType": "int64"},
                    {"name": "DiscountAmount", "dataType": "decimal"},
                ],
                "measures": [
                    {
                        "name": "Total Revenue",
                        "expression": "SUM(FactSales[SalesAmount])",
                        "description": "Sum of all sales amounts",
                    },
                    {
                        "name": "Total Quantity",
                        "expression": "SUM(FactSales[Quantity])",
                        "description": "Total units sold",
                    },
                    {
                        "name": "Avg Order Value",
                        "expression": "DIVIDE(SUM(FactSales[SalesAmount]), DISTINCTCOUNT(FactSales[SalesKey]), 0)",
                        "description": "Average revenue per order",
                    },
                    {
                        "name": "Customer Count",
                        "expression": "DISTINCTCOUNT(FactSales[CustomerKey])",
                        "description": "Unique customers",
                    },
                    {
                        "name": "Revenue per Customer",
                        "expression": "DIVIDE(SUM(FactSales[SalesAmount]), DISTINCTCOUNT(FactSales[CustomerKey]), 0)",
                        "description": "Revenue per unique customer",
                    },
                    {
                        "name": "Net Revenue",
                        "expression": "SUM(FactSales[SalesAmount]) - SUM(FactSales[DiscountAmount])",
                        "description": "Revenue after discounts",
                    },
                ],
            },
            {
                "name": "DimProduct",
                "columns": [
                    {"name": "ProductKey", "dataType": "int64"},
                    {"name": "ProductName", "dataType": "string"},
                    {"name": "Category", "dataType": "string"},
                ],
            },
            {
                "name": "DimCustomer",
                "columns": [
                    {"name": "CustomerKey", "dataType": "int64"},
                    {"name": "CustomerName", "dataType": "string"},
                    {"name": "Region", "dataType": "string"},
                ],
            },
        ],
        "relationships": [
            {"from": "FactSales.ProductKey", "to": "DimProduct.ProductKey", "type": "manyToOne"},
            {"from": "FactSales.CustomerKey", "to": "DimCustomer.CustomerKey", "type": "manyToOne"},
        ],
    }


@pytest.fixture
def sample_dax_expressions():
    """List of (dax_expression, expected_sql_fragment) pairs for basic translation checks."""
    return [
        # Aggregations
        ("SUM(FactSales[SalesAmount])", "SUM(source.salesamount)"),
        ("COUNT(FactSales[SalesKey])", "COUNT(source.saleskey)"),
        ("DISTINCTCOUNT(FactSales[CustomerKey])", "COUNT(DISTINCT source.customerkey)"),
        ("AVERAGE(FactSales[Quantity])", "AVG(source.quantity)"),
        ("MIN(FactSales[SalesAmount])", "MIN(source.salesamount)"),
        ("MAX(FactSales[SalesAmount])", "MAX(source.salesamount)"),
        ("COUNTROWS(FactSales)", "COUNT(*)"),
        # Conditional
        ("IF(1 > 0, 1, 0)", "CASE WHEN"),
        ("ISBLANK(source.col)", "IS NULL"),
        # Date
        ("TODAY()", "CURRENT_DATE()"),
        # Logical operators
        ("a > 1 && b < 2", "AND"),
        ("a > 1 || b < 2", "OR"),
    ]
