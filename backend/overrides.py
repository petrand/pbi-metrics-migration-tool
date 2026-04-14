"""
Overrides System

Configuration layer for mapping Power BI table/column names to Databricks,
adding/removing joins, excluding objects, and merging fact groups.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None


@dataclass
class TargetConfig:
    catalog: str = ""
    schema: str = ""


@dataclass
class JoinOverride:
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    join_type: str = "LEFT"


@dataclass
class JoinExclusion:
    from_table: str
    to_table: str


@dataclass
class MeasureOverride:
    name: str
    expr: str
    comment: str = ""
    window: Optional[dict] = None


@dataclass
class DimensionOverride:
    name: str
    display_name: str = ""
    description: str = ""


@dataclass
class MergeGroup:
    name: str
    source_tables: List[str] = field(default_factory=list)
    target_source: str = ""


@dataclass
class Overrides:
    """Container for all override configuration."""
    target: TargetConfig = field(default_factory=TargetConfig)
    table_mappings: Dict[str, str] = field(default_factory=dict)
    column_mappings: Dict[str, Dict[str, str]] = field(default_factory=dict)
    extra_joins: List[JoinOverride] = field(default_factory=list)
    exclude_joins: List[JoinExclusion] = field(default_factory=list)
    exclude_tables: List[str] = field(default_factory=list)
    exclude_columns: Dict[str, List[str]] = field(default_factory=dict)
    exclude_measures: List[str] = field(default_factory=list)
    merge_fact_groups: List[MergeGroup] = field(default_factory=list)
    measure_overrides: Dict[str, MeasureOverride] = field(default_factory=dict)
    dimension_overrides: Dict[str, DimensionOverride] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "target": {"catalog": self.target.catalog, "schema": self.target.schema},
            "table_mappings": self.table_mappings,
            "column_mappings": self.column_mappings,
            "exclude_tables": self.exclude_tables,
            "exclude_measures": self.exclude_measures,
            "extra_joins_count": len(self.extra_joins),
            "exclude_joins_count": len(self.exclude_joins),
            "merge_groups_count": len(self.merge_fact_groups),
            "measure_overrides_count": len(self.measure_overrides),
        }


class OverridesManager:
    """Manages loading, applying, and validating migration overrides."""

    def load_from_file(self, path: str) -> Overrides:
        """Load overrides from a YAML or JSON file."""
        with open(path, 'r') as f:
            content = f.read()

        if path.endswith('.json'):
            data = json.loads(content)
        elif yaml:
            data = yaml.safe_load(content)
        else:
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                logger.error("PyYAML not installed and file is not JSON")
                return Overrides()

        return self.load_from_dict(data or {})

    def load_from_string(self, yaml_string: str) -> Overrides:
        """Load overrides from a YAML string."""
        if yaml:
            data = yaml.safe_load(yaml_string) or {}
        else:
            try:
                data = json.loads(yaml_string)
            except json.JSONDecodeError:
                return Overrides()
        return self.load_from_dict(data)

    def load_from_dict(self, data: dict) -> Overrides:
        """Load overrides from a dict (parsed YAML/JSON)."""
        o = Overrides()

        # Target
        target = data.get("target", {})
        o.target = TargetConfig(
            catalog=target.get("catalog", ""),
            schema=target.get("schema", ""),
        )

        # Table mappings
        o.table_mappings = {k: v for k, v in data.get("table_mappings", {}).items()}

        # Column mappings
        o.column_mappings = {}
        for table, cols in data.get("column_mappings", {}).items():
            if isinstance(cols, dict):
                o.column_mappings[table] = cols

        # Extra joins
        for j in data.get("extra_joins", []):
            if isinstance(j, dict):
                o.extra_joins.append(JoinOverride(
                    from_table=j.get("from_table", ""),
                    from_column=j.get("from_column", ""),
                    to_table=j.get("to_table", ""),
                    to_column=j.get("to_column", ""),
                    join_type=j.get("join_type", "LEFT"),
                ))

        # Exclude joins
        for j in data.get("exclude_joins", []):
            if isinstance(j, dict):
                o.exclude_joins.append(JoinExclusion(
                    from_table=j.get("from_table", ""),
                    to_table=j.get("to_table", ""),
                ))

        # Excludes
        o.exclude_tables = list(data.get("exclude_tables", []))
        o.exclude_measures = list(data.get("exclude_measures", []))
        o.exclude_columns = {}
        for table, cols in data.get("exclude_columns", {}).items():
            if isinstance(cols, list):
                o.exclude_columns[table] = cols

        # Merge groups
        for mg in data.get("merge_fact_groups", []):
            if isinstance(mg, dict):
                o.merge_fact_groups.append(MergeGroup(
                    name=mg.get("name", ""),
                    source_tables=list(mg.get("source_tables", [])),
                    target_source=mg.get("target_source", ""),
                ))

        # Measure overrides
        for name, spec in data.get("measure_overrides", {}).items():
            if isinstance(spec, dict):
                o.measure_overrides[name] = MeasureOverride(
                    name=name,
                    expr=spec.get("expr", ""),
                    comment=spec.get("comment", ""),
                    window=spec.get("window"),
                )

        # Dimension overrides
        for name, spec in data.get("dimension_overrides", {}).items():
            if isinstance(spec, dict):
                o.dimension_overrides[name] = DimensionOverride(
                    name=name,
                    display_name=spec.get("display_name", ""),
                    description=spec.get("description", ""),
                )

        return o

    def apply_table_mapping(self, overrides: Overrides, pbi_table_name: str) -> str:
        """Resolve a Power BI table name to a Databricks table name."""
        # Case-insensitive lookup
        for key, val in overrides.table_mappings.items():
            if key.lower().strip("'") == pbi_table_name.lower().strip("'"):
                return val
        # Default: lowercase with underscores
        return pbi_table_name.lower().replace(" ", "_").strip("'")

    def apply_column_mapping(self, overrides: Overrides, pbi_table: str, pbi_col: str) -> str:
        """Resolve a Power BI column name to a Databricks column name."""
        for table_key, col_map in overrides.column_mappings.items():
            if table_key.lower().strip("'") == pbi_table.lower().strip("'"):
                for col_key, col_val in col_map.items():
                    if col_key.lower() == pbi_col.lower():
                        return col_val
        return pbi_col.lower().replace(" ", "_")

    def should_exclude_table(self, overrides: Overrides, table_name: str) -> bool:
        return any(t.lower().strip("'") == table_name.lower().strip("'")
                   for t in overrides.exclude_tables)

    def should_exclude_join(self, overrides: Overrides, from_table: str, to_table: str) -> bool:
        return any(
            e.from_table.lower().strip("'") == from_table.lower().strip("'") and
            e.to_table.lower().strip("'") == to_table.lower().strip("'")
            for e in overrides.exclude_joins
        )

    def should_exclude_measure(self, overrides: Overrides, measure_name: str) -> bool:
        return any(m.lower() == measure_name.lower() for m in overrides.exclude_measures)

    def should_exclude_column(self, overrides: Overrides, table_name: str, col_name: str) -> bool:
        for t, cols in overrides.exclude_columns.items():
            if t.lower().strip("'") == table_name.lower().strip("'"):
                return any(c.lower() == col_name.lower() for c in cols)
        return False

    def get_extra_joins(self, overrides: Overrides) -> List[JoinOverride]:
        return overrides.extra_joins

    def get_measure_override(self, overrides: Overrides, name: str) -> Optional[MeasureOverride]:
        for key, val in overrides.measure_overrides.items():
            if key.lower() == name.lower():
                return val
        return None

    def get_dimension_override(self, overrides: Overrides, name: str) -> Optional[DimensionOverride]:
        for key, val in overrides.dimension_overrides.items():
            if key.lower() == name.lower():
                return val
        return None

    def get_merge_groups(self, overrides: Overrides) -> List[MergeGroup]:
        return overrides.merge_fact_groups

    def validate(self, overrides: Overrides) -> List[str]:
        """Validate overrides and return list of warnings."""
        warnings = []
        if overrides.target.catalog and not overrides.target.schema:
            warnings.append("Target catalog set but schema is empty")
        if overrides.target.schema and not overrides.target.catalog:
            warnings.append("Target schema set but catalog is empty")
        for mg in overrides.merge_fact_groups:
            if len(mg.source_tables) < 2:
                warnings.append(f"Merge group '{mg.name}' has fewer than 2 source tables")
            if not mg.target_source:
                warnings.append(f"Merge group '{mg.name}' missing target_source")
        return warnings
