"""
Metric View YAML Generator

Generates Databricks Metric View YAML (v1.1 spec) and CREATE VIEW DDL
from translated semantic model components.

Emits the current (DBR 17.x–18.x) Metric View feature set:
  - join cardinality / rely (at_most_one_match) inferred from PBI relationships
  - recursive (snowflake) joins
  - list-form window measures (order / range / semiadditive / offset / inclusive)
  - agent metadata: display_name, synonyms, format
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
    synonyms: Optional[list] = None


@dataclass
class MetricViewMeasure:
    name: str
    expr: str
    display_name: Optional[str] = None
    comment: Optional[str] = None
    description: Optional[str] = None
    window: Optional[dict] = None
    format: Optional[dict] = None
    synonyms: Optional[list] = None


@dataclass
class MetricViewJoin:
    name: str
    source: str
    on: str
    # Cardinality of the join, e.g. "one_to_many". Metric View joins are
    # many-to-one by default (source rows -> one join row) so this is only
    # emitted when explicitly known.
    cardinality: Optional[str] = None
    # When True, emit `rely: {at_most_one_match: true}` — a query speedup that
    # asserts each source row matches at most one join row (safe for the
    # many-to-one relationships Power BI models use by default).
    rely_at_most_one_match: bool = False
    # Nested joins for snowflake schemas (recursive `joins:` field).
    joins: list = field(default_factory=list)


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
    # measure name -> reason it was excluded from the deployed view
    excluded_measures: dict = field(default_factory=dict)


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


_RESIDUAL_DAX_FUNCS = re.compile(
    r'\b(CALCULATE|CALCULATETABLE|FILTER|ALL|ALLEXCEPT|VALUES|ADDCOLUMNS|'
    r'SELECTCOLUMNS|EARLIER|RELATEDTABLE|USERELATIONSHIP|DATESINPERIOD)\s*\(',
    re.IGNORECASE,
)


def _quote_ident(seg: str) -> str:
    """Backtick-quote a SQL identifier segment that isn't a valid bare name
    (e.g. starts with a digit, like ``2ic_name`` from the column "2IC Name")."""
    if seg and not re.match(r'^[A-Za-z_]\w*$', seg):
        return f"`{seg}`"
    return seg


def _has_residual_dax(expr: str) -> bool:
    """True when *expr* still contains DAX that is not valid Databricks SQL.

    Used to keep partial/untranslated measures out of the deployable metric
    view (they would otherwise fail to parse). ``[...]`` bracket references and
    leftover code fences also count as residual.
    """
    if not expr:
        return True
    if '```' in expr or '[' in expr:
        return True
    return bool(_RESIDUAL_DAX_FUNCS.search(expr))


_AGG_FUNCS = re.compile(
    r'\b(SUM|COUNT|AVG|AVERAGE|MIN|MAX|MEDIAN|STDDEV|VARIANCE|'
    r'APPROX_COUNT_DISTINCT|ANY_VALUE|COLLECT_LIST|COLLECT_SET|'
    r'PERCENTILE|FIRST|LAST)\s*\(|\bMEASURE\s*\(',
    re.IGNORECASE,
)


def _has_aggregation(expr: str) -> bool:
    """True if *expr* aggregates (an aggregate function or a MEASURE() ref).

    Metric-view measures must aggregate; a bare column or column arithmetic
    (Power BI implicit aggregation) is not valid and must be excluded.
    """
    return bool(expr) and bool(_AGG_FUNCS.search(expr))


def _yaml_scalar(s) -> str:
    """Render an arbitrary value as a safe single-line YAML scalar.

    Metric View ``name`` and ``expr`` values can carry characters that break a
    bare YAML scalar — backticks from ``MEASURE(`x`)`` refs, ``%``/``$`` in
    names, embedded newlines/tabs from multi-line DAX. Folds internal
    whitespace to single spaces (SQL is whitespace-insensitive) and
    double-quotes the value whenever a plain scalar would be misparsed.
    """
    if s is None:
        return '""'
    s = re.sub(r'\s+', ' ', str(s)).strip()
    if s == "":
        return '""'
    special = set(':#`%&*!|>@,[]{}"\'')
    needs_quote = (
        s[0] in special or s[0] in '-? ' or s[-1] in ' :' or
        ': ' in s or ' #' in s or any(c in special for c in s)
    )
    if needs_quote:
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def pbi_format_to_metric_format(format_string: str) -> Optional[dict]:
    """Map a Power BI format string to a Metric View ``format`` object.

    Metric View formats accept ``type`` values of number / currency /
    percentage (plus ``currency_code`` for currency). We map conservatively —
    only fields the spec documents — so the generated YAML always deploys.

    Args:
        format_string: A Power BI/DAX format string (e.g. "$#,##0.00", "0.0%").

    Returns:
        A ``format`` dict, or None when no confident mapping exists.
    """
    if not format_string:
        return None
    f = format_string.strip().lower()
    if not f:
        return None
    # Percentage: any '%' in the format.
    if '%' in f:
        return {"type": "percentage"}
    # Currency: a currency symbol or the word "currency".
    if any(sym in f for sym in ('$', '€', '£', '¥')) or 'currency' in f:
        return {"type": "currency", "currency_code": "USD"}
    # Numeric: digit placeholders (#, 0). Keyword-only formats such as
    # "General Date" are intentionally left unmapped.
    if any(ch in f for ch in ('#', '0')):
        return {"type": "number"}
    return None


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
                                 'comment', 'window', 'format_string'.
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

        # ── Build joins (with cardinality/rely + snowflake nesting) ──
        join_nodes, alias_path = self._build_joins(
            fact_table_lower, relationships, catalog, schema, overrides
        )
        # Attach nested joins to their parents (snowflake schemas).
        for node in join_nodes.values():
            parent = node["_parent"]
            if parent is not None and parent in join_nodes:
                join_nodes[parent]["obj"].joins.append(node["obj"])
        # Top-level joins are those rooted at the fact table.
        spec.joins = [n["obj"] for n in join_nodes.values() if n["_parent"] is None]

        # Only dimensions whose join alias is actually reachable from this fact
        # table's join tree may be emitted. `join_nodes` contains a node per
        # relationship *target* across the whole model, including aliases that
        # never connect back to this fact — emitting their dimensions produces
        # `expr` references to join aliases that don't exist in this view
        # (the dominant source of validation warnings). Restrict to the aliases
        # actually present in spec.joins (walked recursively for snowflakes).
        reachable_aliases = self._reachable_join_aliases(spec.joins)

        # ── Build dimensions from dimension tables ──
        fact_table_names = {fact_table_lower}
        # Track emitted dimension names so collisions across joined dims (the
        # same attribute on multiple tables) can be disambiguated — a flat
        # `dimensions:` namespace requires unique names.
        used_dim_names: dict = {}
        for t in tables:
            t_lower = _sanitize_name(t.get("name", ""))
            if t_lower in fact_table_names:
                continue
            if t_lower not in reachable_aliases:
                continue  # skip tables without a reachable join
            # Dotted reference prefix (snowflake path), e.g. "customer.nation".
            ref_prefix = alias_path.get(t_lower, t_lower)

            for col in t.get("columns", []):
                col_name = col.get("name", "")
                col_type = col.get("dataType", "") or col.get("data_type", "")
                # Skip key columns from dimensions
                if col_type == "int64" and col_name.lower().endswith("key"):
                    continue
                if col.get("isHidden") or col.get("is_hidden"):
                    continue
                col_lower = _sanitize_name(col_name)
                dim = MetricViewDimension(
                    name=self._unique_dim_name(col_name, t.get("name", ""),
                                               used_dim_names),
                    expr=f"{ref_prefix}.{_quote_ident(col_lower)}",
                )
                if col.get("description"):
                    dim.comment = col["description"]
                self._apply_dimension_override(dim, col_name, overrides)
                spec.dimensions.append(dim)

        # ── Build measures from translated results ──
        # measure name -> reason it was excluded from the deployable view.
        excluded: dict = {}
        for tm in translated_measures:
            if tm.get("status") in ("excluded",):
                continue
            expr = tm.get("translated_sql") or tm.get("expr", "")
            # A partial translation can still contain residual DAX that is not
            # valid SQL (unstripped CALCULATE/FILTER(ALL)/VALUES, bracket refs,
            # code fences). Emitting it produces a metric view that fails to
            # parse, so exclude it from the deployable view and flag it for a
            # manual measure_override.
            if _has_residual_dax(expr):
                excluded[tm["name"]] = "residual DAX with no clean SQL/window equivalent"
                continue
            # A metric-view measure must aggregate. Power BI implicitly
            # aggregates a bare column measure; here a measure with neither an
            # aggregate function nor a MEASURE() reference (e.g. "source.x" or
            # "source.a + source.b") is not valid SQL for a measure — exclude it.
            if not _has_aggregation(expr):
                excluded[tm["name"]] = "non-aggregating (bare column / arithmetic)"
                continue
            m = MetricViewMeasure(
                name=tm["name"],
                expr=expr,
            )
            if tm.get("comment") or tm.get("description"):
                m.comment = tm.get("comment") or tm.get("description")
            if tm.get("window"):
                m.window = tm["window"]
            # Agent metadata: format derived from the PBI format string.
            fmt = tm.get("format") or pbi_format_to_metric_format(
                tm.get("format_string") or tm.get("formatString") or ""
            )
            if fmt:
                m.format = fmt
            if tm.get("synonyms"):
                m.synonyms = list(tm["synonyms"])
            self._apply_measure_override(m, tm["name"], overrides)
            spec.measures.append(m)

        # A window measure's `order` must reference a declared dimension. Time
        # -intelligence measures order by a fact-table date column, which is not
        # otherwise exposed as a dimension, so add one where it is missing.
        # A window's `order` must reference an existing dimension by its exact
        # name (the engine does not case-fold the reference). Names are
        # case-insensitively unique though, so: if a dimension already matches
        # the order case-insensitively (e.g. calendar "Date" vs order "date"),
        # rewrite the order to that dimension's real name; otherwise expose the
        # fact-table order column as a new dimension.
        dim_by_lower = {d.name.lower(): d.name for d in spec.dimensions}
        for m in spec.measures:
            for w in (m.window if isinstance(m.window, list) else [m.window]):
                if not isinstance(w, dict):
                    continue
                order = w.get("order") or w.get("order_by")
                if not order or "." in str(order):
                    continue
                match = dim_by_lower.get(str(order).lower())
                if match:
                    w["order"] = match  # align to the existing dimension name
                    continue
                spec.dimensions.append(MetricViewDimension(
                    name=order, expr=f"source.{order}"))
                dim_by_lower[str(order).lower()] = order

        # Metric Views forbid a window measure from referencing another window
        # measure (chained time-intelligence like LY-of-MTD). Exclude such
        # measures iteratively so no retained measure references a dropped one;
        # they are flagged for manual measure_override elsewhere.
        spec.measures, dropped_window = self._drop_window_chains(spec.measures)
        for n in dropped_window:
            excluded[n] = "window-over-window (chained time-intelligence, e.g. LY of MTD)"
        # Resolve MEASURE() references: rewrite to the exact kept-measure name
        # (DAX refs are case-insensitive; metric-view MEASURE() is not) and drop
        # any measure that references a measure not present in the view (e.g.
        # one excluded as residual DAX). Iterate to a fixed point.
        spec.measures, dropped_dangling = self._resolve_measure_refs(spec.measures)
        for n in dropped_dangling:
            excluded[n] = "references a measure that was excluded from the view"
        # Metric Views require a referenced measure to be defined before it is
        # used, so order measures topologically by their MEASURE() dependencies
        # (base measures first). Independents keep their original order; cycles
        # fall back to original order.
        spec.measures = self._topo_order_measures(spec.measures)
        spec.excluded_measures = excluded

        return spec

    @staticmethod
    def _topo_order_measures(measures: list) -> list:
        """Return measures ordered so each is defined after the measures it
        references via MEASURE(`...`). Stable for independents; cycle-tolerant."""
        ref_re = re.compile(r"MEASURE\(`([^`]+)`\)")
        by_name = {m.name: m for m in measures}
        ordered: list = []
        placed: set = set()

        def visit(m, stack):
            if m.name in placed:
                return
            for ref in ref_re.findall(m.expr or ""):
                dep = by_name.get(ref)
                if dep is not None and dep.name not in placed and dep.name not in stack:
                    visit(dep, stack | {m.name})
            if m.name not in placed:
                ordered.append(m)
                placed.add(m.name)

        for m in measures:
            visit(m, set())
        return ordered

    @staticmethod
    def _resolve_measure_refs(measures: list) -> tuple:
        """Fix MEASURE() reference casing and drop measures with dangling refs."""
        kept = list(measures)
        dropped: list = []
        ref_re = re.compile(r"MEASURE\(`([^`]+)`\)")
        changed = True
        while changed:
            changed = False
            by_lower = {m.name.lower(): m.name for m in kept}
            names = set(by_lower.values())
            invalid = set()
            for m in kept:
                for ref in ref_re.findall(m.expr or ""):
                    if ref in names:
                        continue
                    actual = by_lower.get(ref.lower())
                    if actual:
                        m.expr = m.expr.replace(f"MEASURE(`{ref}`)", f"MEASURE(`{actual}`)")
                    else:
                        invalid.add(m.name)
            if invalid:
                dropped.extend(sorted(invalid))
                kept = [m for m in kept if m.name not in invalid]
                changed = True
        return kept, dropped

    @staticmethod
    def _drop_window_chains(measures: list) -> tuple:
        """Return (kept, dropped_names) removing invalid window→window chains.

        A window measure that references any window measure is invalid, as is
        any measure that then references a dropped one — so removal iterates to
        a fixed point.
        """
        kept = list(measures)
        dropped: list = []
        changed = True
        while changed:
            changed = False
            window_names = {m.name for m in kept if m.window}
            dropped_names = {d for d in dropped}
            invalid = set()
            for m in kept:
                refs = set(re.findall(r"MEASURE\(`([^`]+)`\)", m.expr or ""))
                # window measure referencing a window measure, or any measure
                # referencing an already-dropped measure.
                if (m.window and refs & window_names) or (refs & dropped_names):
                    invalid.add(m.name)
            if invalid:
                dropped.extend(sorted(invalid))
                kept = [m for m in kept if m.name not in invalid]
                changed = True
        return kept, dropped

    def _build_joins(self, fact_lower, relationships, catalog, schema, overrides):
        """Build join nodes keyed by sanitized alias, resolving snowflake nesting.

        Returns (nodes, alias_path) where:
          - nodes[alias] = {"obj": MetricViewJoin, "_parent": <alias or None>}
          - alias_path[alias] = dotted reference path for dimension exprs
        """
        nodes = {}
        alias_path = {}
        # First pass: create a join node per relationship target.
        for rel in relationships:
            from_parts = rel.get("from", "").split(".")
            to_parts = rel.get("to", "").split(".")
            if len(from_parts) != 2 or len(to_parts) != 2:
                continue

            from_table_lower = _sanitize_name(from_parts[0])
            to_table_lower = _sanitize_name(to_parts[0])
            if to_table_lower == fact_lower or to_table_lower in nodes:
                continue

            from_col = _sanitize_name(from_parts[1])
            to_col = _sanitize_name(to_parts[1])

            # Resolve the join source table, applying table-name overrides.
            join_source = f"{catalog}.{schema}.{to_table_lower}"
            if overrides is not None:
                mapped = self._map_table(overrides, to_parts[0])
                if mapped and mapped != to_table_lower:
                    join_source = mapped if '.' in mapped else f"{catalog}.{schema}.{mapped}"

            # Parent alias: fact -> top-level; another dim -> snowflake nesting.
            parent = None if from_table_lower == fact_lower else from_table_lower
            parent_alias = "source" if parent is None else from_table_lower

            rel_type = (rel.get("type") or "manyToOne").lower()
            # Power BI many-to-one (fact -> dim): each source row matches at most
            # one dim row, so the RELY / at_most_one_match speedup is safe.
            rely = rel_type in ("manytoone", "many_to_one")

            join = MetricViewJoin(
                name=to_table_lower,
                source=join_source,
                on=f"{to_table_lower}.{to_col} = {parent_alias}.{from_col}",
                rely_at_most_one_match=rely,
            )
            nodes[to_table_lower] = {"obj": join, "_parent": parent}

        # Second pass: compute dotted reference paths for (possibly nested) joins.
        def _path(alias, seen):
            if alias in alias_path:
                return alias_path[alias]
            node = nodes.get(alias)
            if node is None or node["_parent"] is None or alias in seen:
                alias_path[alias] = alias
                return alias
            seen.add(alias)
            parent = node["_parent"]
            if parent in nodes:
                alias_path[alias] = f"{_path(parent, seen)}.{alias}"
            else:
                alias_path[alias] = alias
            return alias_path[alias]

        for alias in nodes:
            _path(alias, set())

        return nodes, alias_path

    @staticmethod
    def _reachable_join_aliases(joins) -> set:
        """Collect every join alias present in a (possibly nested) join tree."""
        reachable = set()

        def _walk(nodes):
            for j in nodes:
                reachable.add(j.name)
                if j.joins:
                    _walk(j.joins)

        _walk(joins)
        return reachable

    @staticmethod
    def _unique_dim_name(col_name: str, table_name: str, used: dict) -> str:
        """Return a dimension name unique within the view.

        First use of a name is kept as-is. A collision is disambiguated by
        prefixing the source table name (e.g. "Customer Number" ->
        "Customer Orders Customer Number"); if that also collides, a numeric
        suffix is appended.
        """
        if col_name not in used:
            used[col_name] = 1
            return col_name
        candidate = f"{table_name} {col_name}".strip() if table_name else col_name
        if candidate and candidate not in used:
            used[candidate] = 1
            return candidate
        base = candidate or col_name
        n = used.get(base, 1) + 1
        used[base] = n
        while f"{base} {n}" in used:
            n += 1
        used[f"{base} {n}"] = 1
        return f"{base} {n}"

    @staticmethod
    def _map_table(overrides, table_name):
        """Best-effort table-name mapping via an Overrides object or manager."""
        try:
            if hasattr(overrides, "apply_table_mapping"):
                return overrides.apply_table_mapping(table_name)
            # OverridesManager-style API: manager.apply_table_mapping(overrides, name)
            table_mappings = getattr(overrides, "table_mappings", {}) or {}
            for k, v in table_mappings.items():
                if k.lower().strip("'") == table_name.lower().strip("'"):
                    return v
        except Exception:  # pragma: no cover - defensive
            return None
        return None

    @staticmethod
    def _apply_dimension_override(dim, col_name, overrides):
        d = getattr(overrides, "dimension_overrides", None) if overrides else None
        if not d:
            return
        ov = d.get(col_name) or next(
            (v for k, v in d.items() if k.lower() == col_name.lower()), None
        )
        if not ov:
            return
        if getattr(ov, "display_name", ""):
            dim.display_name = ov.display_name
        if getattr(ov, "description", ""):
            dim.comment = ov.description
        if getattr(ov, "synonyms", None):
            dim.synonyms = list(ov.synonyms)

    @staticmethod
    def _apply_measure_override(measure, name, overrides):
        d = getattr(overrides, "measure_overrides", None) if overrides else None
        if not d:
            return
        ov = d.get(name) or next(
            (v for k, v in d.items() if k.lower() == name.lower()), None
        )
        if not ov:
            return
        if getattr(ov, "display_name", ""):
            measure.display_name = ov.display_name
        if getattr(ov, "synonyms", None):
            measure.synonyms = list(ov.synonyms)
        if getattr(ov, "format", None):
            measure.format = ov.format

    # ── YAML rendering ───────────────────────────────────────────────────

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

        # Joins (recursive for snowflake schemas)
        if spec.joins:
            lines.append("joins:")
            for j in spec.joins:
                lines.extend(self._render_join(j, indent=1))

        # Dimensions
        if spec.dimensions:
            lines.append("dimensions:")
            for d in spec.dimensions:
                lines.append(f"  - name: {_yaml_scalar(d.name)}")
                lines.append(f"    expr: {_yaml_scalar(d.expr)}")
                if d.display_name:
                    lines.append(f"    display_name: {_escape_yaml_string(d.display_name)}")
                if d.comment:
                    lines.append(f"    comment: {_escape_yaml_string(d.comment)}")
                lines.extend(self._render_synonyms(d.synonyms, base_indent=4))

        # Measures
        if spec.measures:
            lines.append("measures:")
            for m in spec.measures:
                lines.append(f"  - name: {_yaml_scalar(m.name)}")
                lines.append(f"    expr: {_yaml_scalar(m.expr)}")
                if m.display_name:
                    lines.append(f"    display_name: {_escape_yaml_string(m.display_name)}")
                if m.comment:
                    lines.append(f"    comment: {_escape_yaml_string(m.comment)}")
                lines.extend(self._render_format(m.format, base_indent=4))
                lines.extend(self._render_synonyms(m.synonyms, base_indent=4))
                lines.extend(self._render_window(m.window, base_indent=4))

        return "\n".join(lines)

    def _render_join(self, j: MetricViewJoin, indent: int) -> list:
        """Render a (possibly nested) join. `indent` is the list-item depth."""
        pad = "  " * indent
        field_pad = "  " * (indent + 1)
        lines = [
            f"{pad}- name: {j.name}",
            f"{field_pad}source: {j.source}",
            # Quote the `on` key: YAML 1.1 parsers read a bare `on` as boolean.
            f"{field_pad}'on': {j.on}",
        ]
        if j.cardinality:
            lines.append(f"{field_pad}cardinality: {j.cardinality}")
        if j.rely_at_most_one_match:
            lines.append(f"{field_pad}rely:")
            lines.append(f"{field_pad}  at_most_one_match: true")
        if j.joins:
            lines.append(f"{field_pad}joins:")
            for child in j.joins:
                lines.extend(self._render_join(child, indent=indent + 2))
        return lines

    @staticmethod
    def _render_format(fmt, base_indent: int) -> list:
        if not fmt or not isinstance(fmt, dict):
            return []
        pad = " " * base_indent
        lines = [f"{pad}format:"]
        for k, v in fmt.items():
            lines.append(f"{pad}  {k}: {v}")
        return lines

    @staticmethod
    def _render_synonyms(synonyms, base_indent: int) -> list:
        if not synonyms:
            return []
        pad = " " * base_indent
        lines = [f"{pad}synonyms:"]
        for s in synonyms:
            lines.append(f"{pad}  - {_escape_yaml_string(str(s))}")
        return lines

    @staticmethod
    def _render_window(window, base_indent: int) -> list:
        """Render window measures as a YAML *list* (the spec-compliant shape).

        Accepts either a single window dict or a list of window dicts. Legacy
        keys (`order_by`) are mapped to the current `order` key.
        """
        if not window:
            return []
        windows = window if isinstance(window, list) else [window]
        pad = " " * base_indent
        lines = [f"{pad}window:"]
        # Ordered so the emitted keys read naturally.
        key_order = ["order", "range", "semiadditive", "offset", "inclusive"]
        for w in windows:
            if not isinstance(w, dict):
                continue
            order = w.get("order", w.get("order_by"))
            item = {}
            if order is not None:
                item["order"] = order
            for k in ("range", "semiadditive", "offset", "inclusive"):
                if w.get(k) is not None:
                    item[k] = w[k]
            first = True
            for k in key_order:
                if k not in item:
                    continue
                prefix = f"{pad}  - " if first else f"{pad}    "
                lines.append(f"{prefix}{k}: {item[k]}")
                first = False
        return lines

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
                    "format_string": m.get("formatString") or m.get("format_string", ""),
                    "synonyms": m.get("synonyms"),
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
            # A Metric View must define at least one measure or dimension.
            # After excluding residual/window measures a fact group can end up
            # empty — skip it rather than emit an invalid view.
            if not spec.measures and not spec.dimensions:
                logger.info("Skipping empty metric view for fact group '%s'", fact["name"])
                continue

            yaml_str = self.generate_yaml(spec)
            ddl_str = self.generate_ddl(spec)
            results.append((spec, yaml_str, ddl_str))

        return results
