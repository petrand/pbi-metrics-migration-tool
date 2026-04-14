"""
Lakeview AI/BI Dashboard Generator

Generates Databricks Lakeview AI/BI Dashboard JSON specifications from
Power BI semantic model data (metric view specs) and optional report layout
information.

Dashboard JSON targets the Lakeview REST API format:
  - datasets: SQL queries against metric views using MEASURE() syntax
  - pages: canvas pages containing widget layouts
  - 6-column grid (x: 0-5, width: 1-6)
  - Counter/table widgets use spec version 2; chart widgets use spec version 3
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PBI visual type -> Lakeview widget type mapping
# ---------------------------------------------------------------------------

_PBI_VISUAL_MAP: Dict[str, str] = {
    # Bar / column charts
    "barChart": "bar",
    "columnChart": "bar",
    "clusteredBarChart": "bar",
    "clusteredColumnChart": "bar",
    "stackedBarChart": "bar",
    "stackedColumnChart": "bar",
    "hundredPercentStackedBarChart": "bar",
    "hundredPercentStackedColumnChart": "bar",
    # Line / area charts
    "lineChart": "line",
    "areaChart": "line",
    "stackedAreaChart": "line",
    "lineClusteredColumnComboChart": "line",
    "lineStackedColumnComboChart": "line",
    # Pie / donut
    "pieChart": "pie",
    "donutChart": "pie",
    "treemap": "pie",
    "funnel": "pie",
    # Scatter / bubble
    "scatterChart": "bar",
    "waterfallChart": "bar",
    # Table / matrix / card
    "tableEx": "table",
    "matrix": "table",
    "pivotTable": "table",
    "card": "counter",
    "multiRowCard": "counter",
    "kpi": "counter",
    "singleValue": "counter",
}

# Widget types that require spec version 2 (non-chart)
_VERSION_2_TYPES = {"counter", "table"}

# Grid constants
_GRID_COLS = 6
_CHART_WIDTH = 3
_CHART_HEIGHT = 4
_COUNTER_HEIGHT = 2
_TABLE_HEIGHT = 4


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _sanitize_name(name: str) -> str:
    """Convert a display name to a safe SQL identifier / widget name."""
    s = re.sub(r"[^a-zA-Z0-9_]", "_", name.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "field"


def _short_id() -> str:
    """Return a short unique hex ID suitable for widget / dataset names."""
    return uuid.uuid4().hex[:6]


# ---------------------------------------------------------------------------
# DashboardSpec dataclass
# ---------------------------------------------------------------------------

@dataclass
class DashboardSpec:
    """Complete specification for a Databricks Lakeview AI/BI Dashboard."""

    display_name: str
    datasets: List[dict] = field(default_factory=list)
    pages: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict matching the Lakeview API format."""
        return {
            "datasets": self.datasets,
            "pages": self.pages,
        }

    def to_json(self, **kwargs) -> str:
        """Convenience wrapper returning a JSON string."""
        return json.dumps(self.to_dict(), **kwargs)


# ---------------------------------------------------------------------------
# DashboardGenerator
# ---------------------------------------------------------------------------

class DashboardGenerator:
    """Generates Databricks Lakeview AI/BI Dashboard JSON from PBI metric view specs.

    Two entry points:
      - generate_from_metric_views(): auto-layout dashboard from metric view specs
        (no PBI report layout required).
      - generate_from_report(): mirror an existing PBI report's page/visual layout.
    """

    def __init__(self):
        # PBI visual type -> Lakeview widget type
        self._visual_map: Dict[str, str] = dict(_PBI_VISUAL_MAP)

    # ------------------------------------------------------------------
    # Public: auto-generate from metric view specs
    # ------------------------------------------------------------------

    def generate_from_metric_views(
        self,
        metric_view_specs: List[dict],
        model_name: str,
        catalog: str,
        schema: str,
    ) -> DashboardSpec:
        """Auto-generate a dashboard from metric view specs without report layout.

        Layout algorithm (6-column grid):
          Row 0: counter widgets — one per measure, width=1, height=2, up to 6/row.
          Next row(s): bar chart widgets — one per (measure x first dimension),
                       width=3, height=4 (2 per row).
          Final row: table widget — width=6, height=4, all dimensions + measures.

        Args:
            metric_view_specs: List of metric view spec dicts.  Each dict is
                expected to contain at minimum:
                  {
                    "view_name": str,          # e.g. "cat.schema.sales_metric_view"
                    "fact_table": str,          # e.g. "Sales"
                    "measures": [{"name": str, "expr": str}, ...],
                    "dimensions": [{"name": str, "expr": str}, ...],
                  }
                Fields may also arrive in the shape produced by
                MetricViewYAMLGenerator.build_spec().
            model_name: Human-readable name for the PBI model (used as page title).
            catalog: Databricks catalog.
            schema: Databricks schema.

        Returns:
            DashboardSpec ready for JSON serialisation.
        """
        display_name = f"{model_name} Dashboard"
        spec = DashboardSpec(display_name=display_name)

        for mv_spec in metric_view_specs:
            fact_group_name = self._fact_group_name(mv_spec)
            measures = self._extract_measures(mv_spec)
            dimensions = self._extract_dimensions(mv_spec)

            if not measures:
                logger.warning(
                    "Metric view spec for '%s' has no measures — skipping.", fact_group_name
                )
                continue

            first_dim = dimensions[0]["name"] if dimensions else None

            # Build a single dataset for this fact group
            ds = self._build_dataset(
                fact_group_name=fact_group_name,
                measures=[m["name"] for m in measures],
                dimensions=[d["name"] for d in dimensions],
                catalog=catalog,
                schema=schema,
            )
            spec.datasets.append(ds)
            ds_name = ds["name"]

            widgets: List[dict] = []  # (widget_dict, position_dict) pairs

            # ── Row 0: counter widgets ──────────────────────────────────
            counter_y = 0
            for i, m in enumerate(measures):
                col = i % _GRID_COLS
                row_offset = (i // _GRID_COLS) * _COUNTER_HEIGHT
                pos = {"x": col, "y": counter_y + row_offset,
                       "width": 1, "height": _COUNTER_HEIGHT}
                widget = self._build_counter_widget(
                    dataset_name=ds_name,
                    measure_name=m["name"],
                    measure_expr=m["expr"],
                    title=m["name"],
                    position=pos,
                )
                widgets.append(widget)

            counter_rows = (len(measures) + _GRID_COLS - 1) // _GRID_COLS
            bar_y = counter_rows * _COUNTER_HEIGHT

            # ── Bar chart rows: one chart per measure × first dimension ──
            if first_dim:
                for i, m in enumerate(measures):
                    col_slot = i % (_GRID_COLS // _CHART_WIDTH)  # 2 charts per row
                    row_offset = (i // (_GRID_COLS // _CHART_WIDTH)) * _CHART_HEIGHT
                    pos = {
                        "x": col_slot * _CHART_WIDTH,
                        "y": bar_y + row_offset,
                        "width": _CHART_WIDTH,
                        "height": _CHART_HEIGHT,
                    }
                    widget = self._build_bar_widget(
                        dataset_name=ds_name,
                        x_field=first_dim,
                        y_field=m["name"],
                        y_agg="SUM",
                        title=f"{m['name']} by {first_dim}",
                        position=pos,
                    )
                    widgets.append(widget)
                chart_rows = (len(measures) + 1) // 2  # 2 charts per row, ceil
                table_y = bar_y + chart_rows * _CHART_HEIGHT
            else:
                table_y = bar_y

            # ── Final row: table widget ─────────────────────────────────
            all_columns = [d["name"] for d in dimensions] + [m["name"] for m in measures]
            table_pos = {"x": 0, "y": table_y, "width": _GRID_COLS, "height": _TABLE_HEIGHT}
            table_widget = self._build_table_widget(
                dataset_name=ds_name,
                columns=all_columns,
                title=f"{fact_group_name.replace('_', ' ').title()} Summary",
                position=table_pos,
            )
            widgets.append(table_widget)

            # Build page
            page = self._build_page(
                display_name=fact_group_name.replace("_", " ").title(),
                widgets=widgets,
            )
            spec.pages.append(page)

        if not spec.pages:
            logger.warning("No pages generated — metric_view_specs may be empty or malformed.")

        return spec

    # ------------------------------------------------------------------
    # Public: generate from PBI report layout
    # ------------------------------------------------------------------

    def generate_from_report(
        self,
        metric_view_specs: List[dict],
        report_pages: list,
        model_name: str,
        catalog: str,
        schema: str,
    ) -> DashboardSpec:
        """Generate a dashboard that mirrors a PBI report's page/visual layout.

        Maps each PBI page to a Lakeview canvas page, and each PBI visual to the
        closest Lakeview widget type.  Unmappable visuals (slicers, images, etc.)
        are skipped with a debug log.

        Args:
            metric_view_specs: Metric view specs (same shape as
                generate_from_metric_views).
            report_pages: List of PBI report page dicts, typically from
                ``report_parser.py``.  Each page dict is expected to contain:
                  {
                    "name": str,
                    "displayName": str,
                    "visuals": [
                      {
                        "type": str,          # PBI visual type
                        "title": str,
                        "measures": [str, ...],
                        "dimensions": [str, ...],
                        "x": int, "y": int, "width": int, "height": int,
                      }, ...
                    ]
                  }
            model_name: Human-readable PBI model name.
            catalog: Databricks catalog.
            schema: Databricks schema.

        Returns:
            DashboardSpec mirroring the PBI report layout.
        """
        display_name = f"{model_name} Dashboard"
        spec = DashboardSpec(display_name=display_name)

        # Build a lookup: fact_group_name -> (measures, dimensions, dataset)
        mv_index: Dict[str, dict] = {}
        for mv_spec in metric_view_specs:
            fg_name = self._fact_group_name(mv_spec)
            measures = self._extract_measures(mv_spec)
            dimensions = self._extract_dimensions(mv_spec)
            ds = self._build_dataset(
                fact_group_name=fg_name,
                measures=[m["name"] for m in measures],
                dimensions=[d["name"] for d in dimensions],
                catalog=catalog,
                schema=schema,
            )
            spec.datasets.append(ds)
            mv_index[fg_name] = {
                "measures": {m["name"]: m for m in measures},
                "dimensions": {d["name"]: d for d in dimensions},
                "dataset_name": ds["name"],
            }

        if not mv_index:
            logger.warning("No metric view specs provided — generating empty dashboard.")
            return spec

        # Use the first (or only) fact group as the default dataset
        default_fg = next(iter(mv_index))
        default_mv = mv_index[default_fg]

        for page_data in report_pages:
            page_display = page_data.get("displayName") or page_data.get("name", "Page")
            visuals = page_data.get("visuals", [])
            widgets: List[dict] = []

            for visual in visuals:
                # Resolve which fact group's dataset to use
                mv_entry = default_mv
                for fg_name, entry in mv_index.items():
                    v_measures = visual.get("measures", [])
                    if v_measures and v_measures[0] in entry["measures"]:
                        mv_entry = entry
                        break

                ds_name = mv_entry["dataset_name"]
                widget = self._map_visual_to_widget(visual, ds_name)
                if widget is not None:
                    widgets.append(widget)
                else:
                    logger.debug(
                        "Skipped PBI visual type '%s' (no Lakeview mapping).",
                        visual.get("type", "unknown"),
                    )

            page = self._build_page(display_name=page_display, widgets=widgets)
            spec.pages.append(page)

        return spec

    # ------------------------------------------------------------------
    # Dataset builder
    # ------------------------------------------------------------------

    def _build_dataset(
        self,
        fact_group_name: str,
        measures: List[str],
        dimensions: List[str],
        catalog: str,
        schema: str,
    ) -> dict:
        """Build a Lakeview dataset dict with MEASURE() syntax against the metric view.

        The metric view is always addressed as:
          ``{catalog}.{schema}.{fact_group_name}_metric_view``

        Args:
            fact_group_name: Sanitised fact table name (no catalog/schema prefix).
            measures: List of measure names.
            dimensions: List of dimension column names.
            catalog: Databricks catalog.
            schema: Databricks schema.

        Returns:
            Dataset dict compatible with the Lakeview API.
        """
        mv_fqn = f"{catalog}.{schema}.{fact_group_name}_metric_view"
        measure_exprs = ", ".join(f"MEASURE(`{m}`)" for m in measures) if measures else "1"
        dim_exprs = ", ".join(f"`{d}`" for d in dimensions) if dimensions else ""

        if dim_exprs:
            select_clause = f"{dim_exprs}, {measure_exprs}"
        else:
            select_clause = measure_exprs

        query = f"SELECT {select_clause} FROM {mv_fqn} GROUP BY ALL"

        ds_id = f"ds_{_short_id()}"
        return {
            "name": ds_id,
            "displayName": fact_group_name.replace("_", " ").title(),
            "queryLines": [query],
        }

    # ------------------------------------------------------------------
    # Widget builders
    # ------------------------------------------------------------------

    def _build_counter_widget(
        self,
        dataset_name: str,
        measure_name: str,
        measure_expr: str,
        title: str,
        position: dict,
    ) -> dict:
        """Build a counter (single-value KPI) widget.

        Args:
            dataset_name: Name of the dataset this widget queries.
            measure_name: Display name of the measure.
            measure_expr: SQL expression for the measure (used as field name alias).
            title: Widget title shown in the dashboard.
            position: {"x": int, "y": int, "width": int, "height": int}

        Returns:
            Layout item dict (widget + position).
        """
        field_name = _sanitize_name(measure_name)
        widget_id = f"w_{_short_id()}"
        query_id = f"q_{_short_id()}"

        return {
            "widget": {
                "name": widget_id,
                "queries": [
                    {
                        "name": query_id,
                        "query": {
                            "datasetName": dataset_name,
                            "fields": [
                                {
                                    "name": field_name,
                                    "expression": f"SUM(`{measure_name}`)",
                                }
                            ],
                            "disaggregated": False,
                        },
                    }
                ],
                "spec": {
                    "version": 2,
                    "widgetType": "counter",
                    "encodings": {
                        "value": {
                            "fieldName": field_name,
                            "displayName": measure_name,
                        }
                    },
                    "frame": {
                        "showTitle": True,
                        "title": title,
                    },
                },
            },
            "position": position,
        }

    def _build_bar_widget(
        self,
        dataset_name: str,
        x_field: str,
        y_field: str,
        y_agg: str,
        title: str,
        position: dict,
        color_field: Optional[str] = None,
    ) -> dict:
        """Build a bar/column chart widget.

        Args:
            dataset_name: Name of the dataset this widget queries.
            x_field: Dimension field name for the X axis (categorical).
            y_field: Measure field name for the Y axis.
            y_agg: Aggregation function for the Y field (e.g. "SUM", "AVG").
            title: Widget title.
            position: {"x": int, "y": int, "width": int, "height": int}
            color_field: Optional field name for color/series encoding.

        Returns:
            Layout item dict.
        """
        x_alias = _sanitize_name(x_field)
        y_alias = f"{y_agg.lower()}_{_sanitize_name(y_field)}"
        widget_id = f"w_{_short_id()}"
        query_id = f"q_{_short_id()}"

        fields = [
            {"name": x_alias, "expression": f"`{x_field}`"},
            {"name": y_alias, "expression": f"{y_agg}(`{y_field}`)"},
        ]
        encodings: dict = {
            "x": {"fieldName": x_alias, "scale": {"type": "categorical"}},
            "y": {"fieldName": y_alias, "scale": {"type": "quantitative"}},
        }

        if color_field:
            color_alias = _sanitize_name(color_field)
            fields.append({"name": color_alias, "expression": f"`{color_field}`"})
            encodings["color"] = {"fieldName": color_alias, "scale": {"type": "categorical"}}

        return {
            "widget": {
                "name": widget_id,
                "queries": [
                    {
                        "name": query_id,
                        "query": {
                            "datasetName": dataset_name,
                            "fields": fields,
                            "disaggregated": False,
                        },
                    }
                ],
                "spec": {
                    "version": 3,
                    "widgetType": "bar",
                    "encodings": encodings,
                    "frame": {
                        "showTitle": True,
                        "title": title,
                    },
                },
            },
            "position": position,
        }

    def _build_line_widget(
        self,
        dataset_name: str,
        x_field: str,
        y_field: str,
        y_agg: str,
        title: str,
        position: dict,
    ) -> dict:
        """Build a line chart widget.

        Args:
            dataset_name: Name of the dataset this widget queries.
            x_field: Dimension field name for the X axis (typically temporal).
            y_field: Measure field name for the Y axis.
            y_agg: Aggregation function (e.g. "SUM").
            title: Widget title.
            position: {"x": int, "y": int, "width": int, "height": int}

        Returns:
            Layout item dict.
        """
        x_alias = _sanitize_name(x_field)
        y_alias = f"{y_agg.lower()}_{_sanitize_name(y_field)}"
        widget_id = f"w_{_short_id()}"
        query_id = f"q_{_short_id()}"

        return {
            "widget": {
                "name": widget_id,
                "queries": [
                    {
                        "name": query_id,
                        "query": {
                            "datasetName": dataset_name,
                            "fields": [
                                {"name": x_alias, "expression": f"`{x_field}`"},
                                {"name": y_alias, "expression": f"{y_agg}(`{y_field}`)"},
                            ],
                            "disaggregated": False,
                        },
                    }
                ],
                "spec": {
                    "version": 3,
                    "widgetType": "line",
                    "encodings": {
                        "x": {"fieldName": x_alias, "scale": {"type": "categorical"}},
                        "y": {"fieldName": y_alias, "scale": {"type": "quantitative"}},
                    },
                    "frame": {
                        "showTitle": True,
                        "title": title,
                    },
                },
            },
            "position": position,
        }

    def _build_pie_widget(
        self,
        dataset_name: str,
        category_field: str,
        value_field: str,
        title: str,
        position: dict,
    ) -> dict:
        """Build a pie / donut chart widget.

        Args:
            dataset_name: Name of the dataset this widget queries.
            category_field: Dimension field for slice labels.
            value_field: Measure field for slice sizes.
            title: Widget title.
            position: {"x": int, "y": int, "width": int, "height": int}

        Returns:
            Layout item dict.
        """
        cat_alias = _sanitize_name(category_field)
        val_alias = f"sum_{_sanitize_name(value_field)}"
        widget_id = f"w_{_short_id()}"
        query_id = f"q_{_short_id()}"

        return {
            "widget": {
                "name": widget_id,
                "queries": [
                    {
                        "name": query_id,
                        "query": {
                            "datasetName": dataset_name,
                            "fields": [
                                {"name": cat_alias, "expression": f"`{category_field}`"},
                                {"name": val_alias, "expression": f"SUM(`{value_field}`)"},
                            ],
                            "disaggregated": False,
                        },
                    }
                ],
                "spec": {
                    "version": 3,
                    "widgetType": "pie",
                    "encodings": {
                        "angle": {"fieldName": val_alias, "scale": {"type": "quantitative"}},
                        "color": {"fieldName": cat_alias, "scale": {"type": "categorical"}},
                    },
                    "frame": {
                        "showTitle": True,
                        "title": title,
                    },
                },
            },
            "position": position,
        }

    def _build_table_widget(
        self,
        dataset_name: str,
        columns: List[str],
        title: str,
        position: dict,
    ) -> dict:
        """Build a table widget displaying the given columns.

        Args:
            dataset_name: Name of the dataset this widget queries.
            columns: Ordered list of column/measure names to display.
            title: Widget title.
            position: {"x": int, "y": int, "width": int, "height": int}

        Returns:
            Layout item dict.
        """
        widget_id = f"w_{_short_id()}"
        query_id = f"q_{_short_id()}"

        fields = [
            {"name": _sanitize_name(col), "expression": f"`{col}`"}
            for col in columns
        ]
        column_encodings = [
            {
                "fieldName": _sanitize_name(col),
                "displayName": col,
            }
            for col in columns
        ]

        return {
            "widget": {
                "name": widget_id,
                "queries": [
                    {
                        "name": query_id,
                        "query": {
                            "datasetName": dataset_name,
                            "fields": fields,
                            "disaggregated": False,
                        },
                    }
                ],
                "spec": {
                    "version": 2,
                    "widgetType": "table",
                    "encodings": {
                        "columns": column_encodings,
                    },
                    "frame": {
                        "showTitle": True,
                        "title": title,
                    },
                },
            },
            "position": position,
        }

    # ------------------------------------------------------------------
    # PBI visual mapper
    # ------------------------------------------------------------------

    def _map_visual_to_widget(
        self, visual: dict, dataset_name: str
    ) -> Optional[dict]:
        """Map a PBI visual dict (from report_parser) to a Lakeview widget layout item.

        Reads the following fields from *visual*:
          - ``type`` (str): PBI visual type string, e.g. "barChart"
          - ``title`` (str): display title
          - ``measures`` (list[str]): measure names referenced by the visual
          - ``dimensions`` (list[str]): dimension names referenced by the visual
          - ``x``, ``y``, ``width``, ``height`` (int): pixel or grid position
            (treated as Lakeview grid units directly if already normalised, or
            scaled to the 6-column grid if values exceed _GRID_COLS)

        Args:
            visual: PBI visual dict.
            dataset_name: Lakeview dataset name to bind this widget to.

        Returns:
            Layout item dict, or None if the visual type cannot be mapped.
        """
        visual_type = visual.get("type", "")
        widget_type = self._visual_map.get(visual_type)

        if not widget_type:
            return None

        title = visual.get("title") or visual_type
        measures = visual.get("measures", [])
        dimensions = visual.get("dimensions", [])

        # Derive position — clamp to grid bounds
        raw_x = int(visual.get("x", 0))
        raw_y = int(visual.get("y", 0))
        raw_w = int(visual.get("width", _CHART_WIDTH))
        raw_h = int(visual.get("height", _CHART_HEIGHT))

        # If the values look like pixel coordinates (very large), scale them
        # to the 6-column grid.  Otherwise treat as grid units.
        if raw_w > _GRID_COLS or raw_x > _GRID_COLS:
            # Assume PBI uses a 1200-unit page width (typical default canvas)
            pbi_page_width = max(raw_x + raw_w, 1200)
            scale = _GRID_COLS / pbi_page_width
            grid_x = min(int(raw_x * scale), _GRID_COLS - 1)
            grid_w = max(1, min(int(raw_w * scale), _GRID_COLS - grid_x))
            grid_y = max(0, int(raw_y * scale))
            grid_h = max(2, int(raw_h * scale))
        else:
            grid_x = max(0, min(raw_x, _GRID_COLS - 1))
            grid_w = max(1, min(raw_w, _GRID_COLS - grid_x))
            grid_y = max(0, raw_y)
            grid_h = max(2, raw_h)

        position = {"x": grid_x, "y": grid_y, "width": grid_w, "height": grid_h}

        primary_measure = measures[0] if measures else None
        primary_dim = dimensions[0] if dimensions else None

        if widget_type == "counter":
            if not primary_measure:
                logger.debug("Counter visual '%s' has no measures — skipping.", title)
                return None
            return self._build_counter_widget(
                dataset_name=dataset_name,
                measure_name=primary_measure,
                measure_expr=f"SUM(`{primary_measure}`)",
                title=title,
                position=position,
            )

        if widget_type == "table":
            cols = dimensions + measures
            if not cols:
                logger.debug("Table visual '%s' has no fields — skipping.", title)
                return None
            return self._build_table_widget(
                dataset_name=dataset_name,
                columns=cols,
                title=title,
                position=position,
            )

        if widget_type == "bar":
            if not primary_measure:
                logger.debug("Bar visual '%s' has no measures — skipping.", title)
                return None
            x_field = primary_dim or primary_measure
            return self._build_bar_widget(
                dataset_name=dataset_name,
                x_field=x_field,
                y_field=primary_measure,
                y_agg="SUM",
                title=title,
                position=position,
                color_field=dimensions[1] if len(dimensions) > 1 else None,
            )

        if widget_type == "line":
            if not primary_measure:
                logger.debug("Line visual '%s' has no measures — skipping.", title)
                return None
            x_field = primary_dim or primary_measure
            return self._build_line_widget(
                dataset_name=dataset_name,
                x_field=x_field,
                y_field=primary_measure,
                y_agg="SUM",
                title=title,
                position=position,
            )

        if widget_type == "pie":
            if not primary_measure:
                logger.debug("Pie visual '%s' has no measures — skipping.", title)
                return None
            cat_field = primary_dim or primary_measure
            return self._build_pie_widget(
                dataset_name=dataset_name,
                category_field=cat_field,
                value_field=primary_measure,
                title=title,
                position=position,
            )

        logger.debug("Unhandled mapped widget type '%s' for visual '%s'.", widget_type, title)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_page(self, display_name: str, widgets: List[dict]) -> dict:
        """Build a Lakeview page dict from a list of layout item dicts."""
        page_id = f"page_{_short_id()}"
        return {
            "name": page_id,
            "displayName": display_name,
            "pageType": "PAGE_TYPE_CANVAS",
            "layout": widgets,
        }

    @staticmethod
    def _fact_group_name(mv_spec: dict) -> str:
        """Derive a sanitised fact group name from a metric view spec dict.

        Accepts both MetricViewSpec-shaped dicts (with ``view_name``) and
        plain dicts with ``fact_table`` or ``source`` keys.
        """
        # MetricViewSpec.to_dict() shape: view_name = "cat.schema.table_metric_view"
        view_name = mv_spec.get("view_name", "")
        if view_name:
            base = view_name.split(".")[-1]  # last component
            # Strip trailing _metric_view suffix if present
            if base.endswith("_metric_view"):
                base = base[: -len("_metric_view")]
            return _sanitize_name(base) or "fact"

        fact_table = mv_spec.get("fact_table") or mv_spec.get("source", "")
        if fact_table:
            base = fact_table.split(".")[-1]
            return _sanitize_name(base) or "fact"

        return "fact"

    @staticmethod
    def _extract_measures(mv_spec: dict) -> List[dict]:
        """Extract measure dicts from a metric view spec.

        Handles both MetricViewSpec-style dicts (list of MetricViewMeasure-like
        objects with ``name`` / ``expr``) and raw dicts.
        """
        raw = mv_spec.get("measures", [])
        result = []
        for m in raw:
            if isinstance(m, dict):
                name = m.get("name", "")
                expr = m.get("expr") or m.get("expression") or m.get("translated_sql", "")
                if name:
                    result.append({"name": name, "expr": expr})
            else:
                # Dataclass instance (MetricViewMeasure) accessed via attributes
                try:
                    name = getattr(m, "name", "")
                    expr = getattr(m, "expr", "") or getattr(m, "expression", "")
                    if name:
                        result.append({"name": name, "expr": expr})
                except Exception:
                    pass
        return result

    @staticmethod
    def _extract_dimensions(mv_spec: dict) -> List[dict]:
        """Extract dimension dicts from a metric view spec.

        Handles both dict-style and dataclass-style entries.
        """
        raw = mv_spec.get("dimensions", [])
        result = []
        for d in raw:
            if isinstance(d, dict):
                name = d.get("name", "")
                expr = d.get("expr") or d.get("expression", "")
                if name:
                    result.append({"name": name, "expr": expr})
            else:
                try:
                    name = getattr(d, "name", "")
                    expr = getattr(d, "expr", "") or getattr(d, "expression", "")
                    if name:
                        result.append({"name": name, "expr": expr})
                except Exception:
                    pass
        return result
