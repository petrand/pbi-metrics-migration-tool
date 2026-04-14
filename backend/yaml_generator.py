"""
Metric View YAML Generator

Generates Databricks Metric View YAML (v1.1 spec) and CREATE VIEW DDL
from translated semantic model components.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import yaml
except ImportError:
    yaml = None
    logger.warning("PyYAML not installed; YAML generation will use manual formatting")


@dataclass
class MetricViewDimension:
    name: str
    expr: str
    display_name: Optional[str] = None
    comment: Optional[str] = None
    description: Optional[str] = None


@dataclass
class MetricViewMeasure:
    name: str
    expr: str
    display_name: Optional[str] = None
    comment: Optional[str] = None
    description: Optional[str] = None
    window: Optional[dict] = None


@dataclass
class MetricViewJoin:
    name: str
    source: str
    on: str
    join_type: str = "LEFT"


@dataclass
class MetricViewSpec:
    """Complete specification for a Databricks Metric View."""
    version: str = "1.1"
    source: str = ""
    comment: str = ""
    joins: list = field(default_factory=list)
    dimensions: list = field(default_factory=list)
    measures: list = field(default_factory=list)
    view_name: str = ""


def _sanitize_name(name: str) -> str:
    """Convert a display name to a valid SQL identifier."""
    s = re.sub(r'[^a-zA-Z0-9_]', '_', name.strip().lower())
    s = re.sub(r'_+', '_', s).strip('_')
    return s


def _escape_yaml_string(s: str) -> str:
    """Escape a string for safe YAML embedding."""
    if not s:
        return '""'
    if any(c in s for c in (':', '{', '}', '[', ']', ',', '&', '*', '#', '?',
                             '|', '-', '<', '>', '=', '!', '%', '@', '`', '"')):
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    if s.startswith(("'", '"')) or s != s.strip():
        return f'"{s}"'
    return s


class MetricViewYAMLGenerator:
    """Generates Databricks Metric View YAML and DDL from migration components."""

    def build_spec(
        self,
        model_name: str,
        fact_table: str,
        catalog: str,
        schema: str,
        tables: list,
        relationships: list,
        translated_measures: list,
        overrides=None,
    ) -> MetricViewSpec:
        """Build a MetricViewSpec from semantic model components.

        Args:
            model_name: Name of the semantic model.
            fact_table: Name of the primary fact table.
            catalog: Target Databricks catalog.
            schema: Target Databricks schema.
            tables: List of table dicts with 'name', 'columns', 'measures'.
            relationships: List of relationship dicts.
            translated_measures: List of dicts with 'name', 'expr', 'status',
                                 'comment', 'window'.
            overrides: Optional Overrides object from the overrides module.

        Returns:
            MetricViewSpec ready for YAML rendering.
        """
        fact_table_lower = _sanitize_name(fact_table)
        source_fqn = f"{catalog}.{schema}.{fact_table_lower}"
        view_name = f"{catalog}.{schema}.{fact_table_lower}_metric_view"

        spec = MetricViewSpec(
            source=source_fqn,
            comment=f"Migrated from Power BI: {model_name}",
            view_name=view_name,
        )

        # Build joins from relationships
        seen_joins = set()
        for rel in relationships:
            from_parts = rel.get("from", "").split(".")
            to_parts = rel.get("to", "").split(".")
            if len(from_parts) != 2 or len(to_parts) != 2:
                continue

            to_table = to_parts[0]
            to_table_lower = _sanitize_name(to_table)
            if to_table_lower in seen_joins or to_table_lower == fact_table_lower:
                continue
            seen_joins.add(to_table_lower)

            from_col = _sanitize_name(from_parts[1])
            to_col = _sanitize_name(to_parts[1])

            # Apply overrides for table name mapping if available
            join_source = f"{catalog}.{schema}.{to_table_lower}"
            if overrides and hasattr(overrides, 'apply_table_mapping'):
                mapped = overrides.apply_table_mapping(to_table)
                if mapped and mapped != to_table.lower():
                    join_source = mapped if '.' in mapped else f"{catalog}.{schema}.{mapped}"

            spec.joins.append(MetricViewJoin(
                name=to_table_lower,
                source=join_source,
                on=f"{to_table_lower}.{to_col} = source.{from_col}",
            ))

        # Build dimensions from dimension tables
        fact_table_names = {_sanitize_name(fact_table)}
        for t in tables:
            t_lower = _sanitize_name(t.get("name", ""))
            if t_lower in fact_table_names:
                continue
            alias = t_lower
            if alias not in seen_joins:
                continue  # skip tables without a join

            for col in t.get("columns", []):
                col_name = col.get("name", "")
                col_type = col.get("dataType", "")
                # Skip key columns from dimensions
                if col_type == "int64" and col_name.lower().endswith("key"):
                    continue
                if col.get("isHidden"):
                    continue
                col_lower = _sanitize_name(col_name)
                dim = MetricViewDimension(
                    name=col_name,
                    expr=f"{alias}.{col_lower}",
                )
                if col.get("description"):
                    dim.comment = col["description"]
                spec.dimensions.append(dim)

        # Build measures from translated results
        for tm in translated_measures:
            if tm.get("status") in ("excluded",):
                continue
            m = MetricViewMeasure(
                name=tm["name"],
                expr=tm.get("translated_sql") or tm.get("expr", ""),
            )
            if tm.get("comment") or tm.get("description"):
                m.comment = tm.get("comment") or tm.get("description")
            if tm.get("window"):
                m.window = tm["window"]
            spec.measures.append(m)

        return spec

    def generate_yaml(self, spec: MetricViewSpec) -> str:
        """Generate YAML string from a MetricViewSpec.

        Returns a properly formatted YAML string matching the Databricks
        Metric View v1.1 specification.
        """
        lines = [
            f"version: '{spec.version}'",
            f"source: {spec.source}",
        ]
        if spec.comment:
            lines.append(f"comment: {_escape_yaml_string(spec.comment)}")

        # Joins
        if spec.joins:
            lines.append("joins:")
            for j in spec.joins:
                lines.append(f"  - name: {j.name}")
                lines.append(f"    source: {j.source}")
                lines.append(f"    on: {j.on}")
                if j.join_type and j.join_type.upper() != "LEFT":
                    lines.append(f"    type: {j.join_type}")

        # Dimensions
        if spec.dimensions:
            lines.append("dimensions:")
            for d in spec.dimensions:
                lines.append(f"  - name: {d.name}")
                lines.append(f"    expr: {d.expr}")
                if d.display_name:
                    lines.append(f"    display_name: {_escape_yaml_string(d.display_name)}")
                if d.comment:
                    lines.append(f"    comment: {_escape_yaml_string(d.comment)}")

        # Measures
        if spec.measures:
            lines.append("measures:")
            for m in spec.measures:
                lines.append(f"  - name: {m.name}")
                lines.append(f"    expr: {m.expr}")
                if m.display_name:
                    lines.append(f"    display_name: {_escape_yaml_string(m.display_name)}")
                if m.comment:
                    lines.append(f"    comment: {_escape_yaml_string(m.comment)}")
                if m.window:
                    lines.append("    window:")
                    for k, v in m.window.items():
                        lines.append(f"      {k}: {v}")

        return "\n".join(lines)

    def generate_ddl(self, spec: MetricViewSpec) -> str:
        """Generate a CREATE OR REPLACE VIEW ... WITH METRICS DDL statement."""
        yaml_content = self.generate_yaml(spec)
        return (
            f"CREATE OR REPLACE VIEW {spec.view_name}\n"
            f"COMMENT '{spec.comment}'\n"
            f"WITH METRICS\n"
            f"LANGUAGE YAML\n"
            f"AS $$\n"
            f"  {yaml_content.replace(chr(10), chr(10) + '  ')}\n"
            f"$$;"
        )

    def generate_from_model(
        self,
        model: dict,
        catalog: str,
        schema: str,
        overrides=None,
    ) -> list:
        """Generate metric view specs for all fact groups in a model.

        Args:
            model: Semantic model dict with 'name', 'tables', 'relationships'.
            catalog: Target catalog.
            schema: Target schema.
            overrides: Optional overrides object.

        Returns:
            List of (MetricViewSpec, yaml_str, ddl_str) tuples.
        """
        results = []
        tables = model.get("tables", [])
        relationships = model.get("relationships", [])

        # Identify fact tables (tables with measures)
        fact_tables = [t for t in tables if t.get("measures")]
        if not fact_tables:
            fact_tables = tables[:1] if tables else []

        for fact in fact_tables:
            # Build translated measures (pass-through if already translated)
            measures = []
            for m in fact.get("measures", []):
                measures.append({
                    "name": m.get("name", ""),
                    "translated_sql": m.get("translated_sql") or m.get("expression", ""),
                    "status": m.get("status", "converted"),
                    "comment": m.get("description", ""),
                    "window": m.get("window"),
                })

            spec = self.build_spec(
                model_name=model.get("name", "Unknown"),
                fact_table=fact["name"],
                catalog=catalog,
                schema=schema,
                tables=tables,
                relationships=relationships,
                translated_measures=measures,
                overrides=overrides,
            )
            yaml_str = self.generate_yaml(spec)
            ddl_str = self.generate_ddl(spec)
            results.append((spec, yaml_str, ddl_str))

        return results
