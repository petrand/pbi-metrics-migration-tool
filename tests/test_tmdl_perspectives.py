"""Tests for TMDL perspective extraction (backend/tmdl_parser.py).

Perspectives are named subsets of a semantic model; the Explore page's key-metrics
bar reports how many a model defines, so the parser must surface them.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.tmdl_parser import TMDLParser

MODEL_WITH_PERSPECTIVES = """
model Sales
	culture: en-US

table FactSales
	measure 'Total Sales' = SUM(FactSales[Amount])
	column Amount
	column ProductKey

table DimProduct
	column ProductKey
	column ProductName

perspective 'Executive View'
	perspectiveTable FactSales
		perspectiveMeasure 'Total Sales'
	perspectiveTable DimProduct

perspective 'Sales Ops'
	perspectiveTable FactSales
"""


def test_perspectives_parsed_and_counted():
    model = TMDLParser().parse_string(MODEL_WITH_PERSPECTIVES)
    names = [p.name for p in model.perspectives]
    assert names == ["Executive View", "Sales Ops"]


def test_perspective_table_membership_captured():
    model = TMDLParser().parse_string(MODEL_WITH_PERSPECTIVES)
    by_name = {p.name: p for p in model.perspectives}
    assert by_name["Executive View"].tables == ["FactSales", "DimProduct"]
    assert by_name["Sales Ops"].tables == ["FactSales"]


def test_perspectives_serialised_in_to_dict():
    d = TMDLParser().parse_string(MODEL_WITH_PERSPECTIVES).to_dict()
    assert "perspectives" in d
    assert len(d["perspectives"]) == 2
    assert d["perspectives"][0]["tableCount"] == 2


def test_model_without_perspectives_yields_empty_list():
    tmdl = """
model Bare
table FactOnly
	measure 'M' = SUM(FactOnly[X])
	column X
"""
    d = TMDLParser().parse_string(tmdl).to_dict()
    assert d["perspectives"] == []
