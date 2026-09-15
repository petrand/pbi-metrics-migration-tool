"""Calculated columns (``column <name> = <DAX>``) must be captured by the parser.

Relationships/joins can key on a calculated column (e.g. a computed composite
key). If the parser drops it, the generated CREATE TABLE omits the column and the
metric-view join references a column that cannot be resolved at deploy time.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.tmdl_parser import TMDLParser

MODEL = """
model Sales

table Orders
	measure 'Total' = SUM(Orders[Amount])
	column Amount
		dataType: double
	column OrderNumber
		dataType: double
	column OrderNumberLine = 'Orders'[OrderNumber] & "|" & 'Orders'[SourceKey]
		dataType: string
		isHidden
	column SourceKey
		dataType: int64
"""


def test_calculated_column_is_parsed_with_datatype():
    model = TMDLParser().parse_string(MODEL)
    orders = next(t for t in model.tables if t.name == "Orders")
    by_name = {c.name: c for c in orders.columns}
    assert "OrderNumberLine" in by_name, "calculated column must be captured"
    assert by_name["OrderNumberLine"].data_type == "string"
    # Plain columns are unaffected.
    assert {"Amount", "OrderNumber", "SourceKey"} <= set(by_name)


def test_calculated_column_reaches_generated_table_ddl():
    from backend.pipeline import MigrationPipeline, MigrationConfig
    model = TMDLParser().parse_string(MODEL).to_dict()
    res = MigrationPipeline().run(
        model, MigrationConfig(catalog="c", schema="s", deploy=False,
                               dry_run=False, validate_only=True), None)
    orders_ddl = res.generated_sql.get("orders", "")
    assert "ordernumberline" in orders_ddl.lower()
