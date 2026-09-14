"""
evaluator.py — Evaluation and reporting for the PBI-to-Databricks migration pipeline.

Generates per-measure evaluations, fact-group summaries, pipeline-level statistics,
and a full JSON migration manifest suitable for API consumption or archiving.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import yaml  # PyYAML; stdlib-only fallback handled in to_yaml_report

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass definitions
# ---------------------------------------------------------------------------


@dataclass
class MeasureEvaluation:
    """Detailed evaluation result for a single DAX measure."""

    name: str
    """The original measure name from the Power BI model."""

    original_dax: str
    """The raw DAX expression before any transformation."""

    translated_sql: Optional[str]
    """The translated SQL expression, or *None* when conversion failed entirely."""

    status: str
    """
    Lifecycle status of this measure:
    - ``"converted"``        — fully translated, ready to deploy
    - ``"partial"``          — translated with caveats; manual review recommended
    - ``"unsupported"``      — DAX construct has no SQL equivalent; blocked
    - ``"manual_override"``  — a human-supplied SQL was applied instead of auto-translation
    - ``"excluded"``         — intentionally skipped (e.g. private / calculated-table measure)
    """

    confidence: int
    """Translation confidence score in the range 0–100."""

    applied_transformations: list[str] = field(default_factory=list)
    """
    Tags describing each transformation that was applied, e.g.
    ``["table_column_refs", "function_rename_SUM", "DIVIDE_to_COALESCE"]``.
    """

    issues: list[str] = field(default_factory=list)
    """
    Hard problems that prevent deployment, e.g.
    ``["CALCULATE with complex filter requires manual review"]``.
    """

    warnings: list[str] = field(default_factory=list)
    """
    Non-blocking observations, e.g.
    ``["SAMEPERIODLASTYEAR partially supported — fiscal-year offset ignored"]``.
    """

    window_spec: Optional[dict] = None
    """
    For time-intelligence measures, a structured window specification produced
    by the DAX translator, e.g. ``{"type": "LAG", "periods": 1, "grain": "YEAR"}``.
    """

    display_folder: Optional[str] = None
    """The Power BI display folder this measure belonged to, if available."""

    format_string: Optional[str] = None
    """The Power BI format string for the measure, e.g. ``"$#,##0.00"``."""

    deployed: bool = True
    """Whether this measure made it into the generated/deployable metric view.
    ``False`` when the generator had to exclude it (see ``exclusion_reason``)."""

    exclusion_reason: Optional[str] = None
    """If not deployed, why the generator excluded it from the view."""

    rls_applied: bool = False
    """True when this measure carried in-measure row-level-security (a security
    gate / security-table filter) that was removed for conversion. Surfaced to
    the UI as an asterisk; access must be enforced by the view's row filters."""


@dataclass
class FactGroupEvaluation:
    """Aggregated evaluation results for one business-domain / fact-table group."""

    name: str
    """Human-readable name of the fact group, e.g. ``"Sales"``."""

    source_table: str
    """The primary fact table driving this group, e.g. ``"FactSales"``."""

    total_measures: int
    """Total number of measures in this group."""

    converted: int
    """Count of measures with status ``"converted"``."""

    partial: int
    """Count of measures with status ``"partial"``."""

    unsupported: int
    """Count of measures with status ``"unsupported"``."""

    manual_overrides: int
    """Count of measures with status ``"manual_override"``."""

    excluded: int
    """Count of measures with status ``"excluded"``."""

    conversion_rate: float
    """
    Percentage of measures successfully migrated into the deployed metric view:
    ``(converted + manual_overrides that are actually in the view) /
    (total - status-excluded) * 100``. A measure that translated but had to be
    excluded from the view (``deployed=False``) does NOT count — it lowers the
    rate — because it is not a usable result.
    """

    deployed: int = 0
    """Count of successfully-migrated measures actually in the deployed view
    (converted / manual-override *and* not excluded) — the conversion-rate
    numerator."""

    not_deployed: int = 0
    """Count of translated measures the generator excluded from the view
    (candidates that couldn't be expressed as a metric-view measure). Distinct
    from ``excluded``, which is intentional status-based skips."""

    measures: list[MeasureEvaluation] = field(default_factory=list)
    """Individual measure evaluations belonging to this group."""

    dimensions_count: int = 0
    """Number of dimension tables linked to this fact group."""

    dimensions: list = field(default_factory=list)
    """Converted dimension columns exposed by the view, as
    ``[{"name": <dimension name>, "expr": <source column ref>}]``."""

    joins_count: int = 0
    """Number of join relationships defined for this fact group."""

    validation_status: str = "OK"
    """
    Overall validation status: ``"OK"``, ``"WARNINGS"``, or ``"ERRORS"``.
    """

    validation_messages: list[str] = field(default_factory=list)
    """
    Human-readable validation messages produced during schema / SQL validation.
    """


@dataclass
class PipelineSummary:
    """Top-level summary of a complete migration pipeline run."""

    total_measures: int
    converted: int
    partial: int
    unsupported: int
    manual_overrides: int
    excluded: int
    overall_conversion_rate: float
    """Percentage: ``(converted + manual_overrides) / total_measures * 100``."""

    total_tables: int
    total_relationships: int
    total_dimensions: int

    deployed: int = 0
    """Measures migrated into a deployed view (conversion-rate numerator)."""

    not_deployed: int = 0
    """Translated measures excluded from the deployed view (candidates that
    couldn't be expressed as a metric-view measure)."""

    fact_groups: list[FactGroupEvaluation] = field(default_factory=list)

    duration_seconds: float = 0.0
    started_at: str = ""
    completed_at: str = ""

    model_name: str = ""
    target_catalog: str = ""
    target_schema: str = ""


@dataclass
class MigrationManifest:
    """
    Full migration manifest — the single source of truth for one migration run.
    Serialisable to JSON for API responses or archival storage.
    """

    id: str
    """UUID v4 identifier for this manifest."""

    version: str
    """Manifest schema version, currently ``"2.0"``."""

    pipeline_summary: PipelineSummary

    generated_sql: dict[str, str] = field(default_factory=dict)
    """Mapping of ``fact_group_name → SQL DDL`` strings."""

    generated_yaml: dict[str, str] = field(default_factory=dict)
    """Mapping of ``fact_group_name → YAML`` strings."""

    overrides_applied: dict = field(default_factory=dict)
    """Summary of manual overrides that were incorporated during the run."""

    errors: list[str] = field(default_factory=list)
    """Any top-level pipeline errors (not measure-specific)."""


# ---------------------------------------------------------------------------
# Reporter implementation
# ---------------------------------------------------------------------------


class EvaluationReporter:
    """
    Builds evaluation objects from raw translation results and renders them into
    human-readable or machine-readable report formats.

    Usage example::

        reporter = EvaluationReporter()

        measure_evals = [
            reporter.evaluate_measure(name, dax, translation_result)
            for name, dax, translation_result in translations
        ]

        group_eval = reporter.evaluate_fact_group(
            "Sales", "FactSales", measure_evals, dims_count=6, joins_count=5
        )

        summary = reporter.generate_pipeline_summary(
            [group_eval], model_name="Sales Analytics Model",
            catalog="hls_amer_catalog", schema="metrics",
            duration=14.2, started_at="2026-04-14T10:00:00Z"
        )

        manifest = reporter.generate_manifest(summary, sql_dict, yaml_dict)
        print(reporter.to_text_summary(summary))
    """

    # ------------------------------------------------------------------
    # Core evaluation methods
    # ------------------------------------------------------------------

    def evaluate_measure(
        self,
        name: str,
        original_dax: str,
        translation_result: dict,
    ) -> MeasureEvaluation:
        """
        Convert a raw translation result (as produced by the DAX translator) into
        a :class:`MeasureEvaluation`.

        Parameters
        ----------
        name:
            The measure name.
        original_dax:
            The original DAX expression string.
        translation_result:
            Dictionary returned by the DAX translator.  Expected keys (all
            optional; the method degrades gracefully when keys are absent):

            - ``sql``           — translated SQL expression
            - ``status``        — one of the status constants
            - ``confidence``    — int 0–100
            - ``transformations`` / ``applied_transformations`` — list[str]
            - ``issues``        — list[str]
            - ``warnings``      — list[str]
            - ``window_spec``   — dict or None
            - ``display_folder`` — str or None
            - ``format_string`` — str or None

        Returns
        -------
        MeasureEvaluation
        """
        r = translation_result or {}

        # Handle both dict and dataclass (TranslationResult) inputs
        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        translated_sql: Optional[str] = _get(r, "sql") or _get(r, "translated_sql")
        raw_status: str = _get(r, "status", "")

        # Normalise status
        status = self._normalise_status(raw_status, translated_sql)

        # Pull transformations from either key name the translator may use
        transformations: list[str] = (
            _get(r, "applied_transformations")
            or _get(r, "transformations")
            or []
        )

        issues: list[str] = _get(r, "issues") or []
        warnings: list[str] = _get(r, "warnings") or []

        # Compute confidence from translator value, then adjust for issues/warnings
        raw_confidence: int = int(_get(r, "confidence", 0) or 0)
        confidence = self._compute_confidence(
            raw_confidence, status, issues, warnings
        )

        eval_obj = MeasureEvaluation(
            name=name,
            original_dax=original_dax,
            translated_sql=translated_sql,
            status=status,
            confidence=confidence,
            applied_transformations=list(transformations),
            issues=list(issues),
            warnings=list(warnings),
            window_spec=_get(r, "window_spec"),
            display_folder=_get(r, "display_folder"),
            format_string=_get(r, "format_string"),
            rls_applied=bool(_get(r, "rls_applied", False)),
        )

        logger.debug(
            "Evaluated measure '%s': status=%s confidence=%d issues=%d",
            name,
            status,
            confidence,
            len(issues),
        )
        return eval_obj

    def evaluate_fact_group(
        self,
        group_name: str,
        source_table: str,
        measures: list[MeasureEvaluation],
        dims_count: int = 0,
        joins_count: int = 0,
    ) -> FactGroupEvaluation:
        """
        Aggregate a list of :class:`MeasureEvaluation` objects into a
        :class:`FactGroupEvaluation`.

        Parameters
        ----------
        group_name:
            Human-readable name for the fact group.
        source_table:
            Primary fact table name.
        measures:
            Per-measure evaluation objects belonging to this group.
        dims_count:
            Number of dimension tables.
        joins_count:
            Number of join relationships.

        Returns
        -------
        FactGroupEvaluation
        """
        total = len(measures)
        converted = sum(1 for m in measures if m.status == "converted")
        partial = sum(1 for m in measures if m.status == "partial")
        unsupported = sum(1 for m in measures if m.status == "unsupported")
        overrides = sum(1 for m in measures if m.status == "manual_override")
        excluded = sum(1 for m in measures if m.status == "excluded")

        # A measure counts toward the conversion rate only if it both
        # translated successfully AND actually landed in the deployed view. A
        # converted measure the generator had to exclude (deployed=False) is not
        # a usable result, so it lowers the rate.
        deployable = sum(
            1 for m in measures
            if m.deployed is not False and m.status in ("converted", "manual_override")
        )
        # Candidates dropped from the view (not intentional status-based skips).
        not_deployed = sum(1 for m in measures if m.deployed is False and m.status != "excluded")
        effective_total = total - excluded
        conversion_rate = (
            (deployable / effective_total * 100) if effective_total > 0 else 0.0
        )

        # Derive validation status from individual measure issues
        all_issues: list[str] = []
        all_warnings: list[str] = []
        for m in measures:
            all_issues.extend(m.issues)
            all_warnings.extend(m.warnings)

        validation_messages: list[str] = []
        if all_issues:
            validation_messages.extend(all_issues)
        if all_warnings:
            validation_messages.extend(all_warnings)

        if all_issues:
            validation_status = "ERRORS"
        elif all_warnings:
            validation_status = "WARNINGS"
        else:
            validation_status = "OK"

        group = FactGroupEvaluation(
            name=group_name,
            source_table=source_table,
            total_measures=total,
            converted=converted,
            partial=partial,
            unsupported=unsupported,
            manual_overrides=overrides,
            excluded=excluded,
            conversion_rate=round(conversion_rate, 1),
            measures=measures,
            deployed=deployable,
            not_deployed=not_deployed,
            dimensions_count=dims_count,
            joins_count=joins_count,
            validation_status=validation_status,
            validation_messages=validation_messages,
        )

        logger.info(
            "Fact group '%s': %d measures, conversion_rate=%.1f%%, validation=%s",
            group_name,
            total,
            conversion_rate,
            validation_status,
        )
        return group

    def generate_pipeline_summary(
        self,
        fact_groups: list[FactGroupEvaluation],
        model_name: str,
        catalog: str,
        schema: str,
        duration: float,
        started_at: str,
        total_relationships: Optional[int] = None,
    ) -> PipelineSummary:
        """
        Roll up a collection of :class:`FactGroupEvaluation` objects into a
        :class:`PipelineSummary`.

        Parameters
        ----------
        fact_groups:
            All fact-group evaluations produced in this pipeline run.
        model_name:
            The source Power BI model name.
        catalog:
            Target Databricks catalog.
        schema:
            Target Databricks schema.
        duration:
            Elapsed pipeline time in seconds.
        started_at:
            ISO-8601 timestamp when the pipeline started.

        Returns
        -------
        PipelineSummary
        """
        total = sum(g.total_measures for g in fact_groups)
        converted = sum(g.converted for g in fact_groups)
        partial = sum(g.partial for g in fact_groups)
        unsupported = sum(g.unsupported for g in fact_groups)
        overrides = sum(g.manual_overrides for g in fact_groups)
        excluded = sum(g.excluded for g in fact_groups)
        total_dims = sum(g.dimensions_count for g in fact_groups)
        # `joins_count` is per-fact-group; summing it counts each shared model
        # relationship once per fact group (N relationships x M fact groups).
        # When the caller passes the model's distinct relationship count, report
        # that instead of the inflated sum.
        total_joins = (
            total_relationships
            if total_relationships is not None
            else sum(g.joins_count for g in fact_groups)
        )

        # Deployment-aware conversion: only measures that landed in a view count.
        deployed_total = sum(g.deployed for g in fact_groups)
        not_deployed_total = sum(g.not_deployed for g in fact_groups)
        effective_total = total - excluded
        overall_rate = (
            (deployed_total / effective_total * 100) if effective_total > 0 else 0.0
        )

        completed_at = datetime.now(timezone.utc).isoformat()

        summary = PipelineSummary(
            total_measures=total,
            converted=converted,
            partial=partial,
            unsupported=unsupported,
            manual_overrides=overrides,
            excluded=excluded,
            overall_conversion_rate=round(overall_rate, 1),
            total_tables=len(fact_groups),
            total_relationships=total_joins,
            total_dimensions=total_dims,
            deployed=deployed_total,
            not_deployed=not_deployed_total,
            fact_groups=fact_groups,
            duration_seconds=round(duration, 3),
            started_at=started_at,
            completed_at=completed_at,
            model_name=model_name,
            target_catalog=catalog,
            target_schema=schema,
        )

        logger.info(
            "Pipeline summary: %d measures across %d fact groups, "
            "overall_conversion_rate=%.1f%%, duration=%.3fs",
            total,
            len(fact_groups),
            overall_rate,
            duration,
        )
        return summary

    def generate_manifest(
        self,
        summary: PipelineSummary,
        generated_sql: dict[str, str],
        generated_yaml: dict[str, str],
        overrides: Optional[dict] = None,
    ) -> MigrationManifest:
        """
        Assemble the final :class:`MigrationManifest` for a pipeline run.

        Parameters
        ----------
        summary:
            The :class:`PipelineSummary` for this run.
        generated_sql:
            Mapping of ``fact_group_name → SQL DDL`` strings.
        generated_yaml:
            Mapping of ``fact_group_name → YAML`` strings.
        overrides:
            Optional dictionary summarising manual overrides that were applied.

        Returns
        -------
        MigrationManifest
        """
        manifest_id = str(uuid.uuid4())

        # Collect any top-level errors from unsupported measures
        errors: list[str] = []
        for group in summary.fact_groups:
            for m in group.measures:
                if m.status == "unsupported":
                    errors.append(
                        f"[{group.name}] Measure '{m.name}' could not be translated."
                    )

        manifest = MigrationManifest(
            id=manifest_id,
            version="2.0",
            pipeline_summary=summary,
            generated_sql=generated_sql,
            generated_yaml=generated_yaml,
            overrides_applied=overrides or {},
            errors=errors,
        )

        logger.info(
            "Generated manifest %s (version=%s, errors=%d)",
            manifest_id,
            manifest.version,
            len(errors),
        )
        return manifest

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_dict(self, obj: Any) -> Any:
        """
        Recursively serialise *obj* to a JSON-compatible dictionary.

        Handles:

        - dataclasses → ``dict``
        - ``datetime`` → ISO-8601 string
        - ``list`` / ``tuple`` → recursively converted list
        - ``dict`` → recursively converted dict
        - Enums (``value`` attribute) → their value
        - Primitives (str, int, float, bool, None) → unchanged
        """
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {
                k: self.to_dict(v)
                for k, v in dataclasses.asdict(obj).items()
            }
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, (list, tuple)):
            return [self.to_dict(item) for item in obj]
        if isinstance(obj, dict):
            return {k: self.to_dict(v) for k, v in obj.items()}
        # Handle enums that carry a .value attribute
        if hasattr(obj, "value") and hasattr(type(obj), "__mro__"):
            for base in type(obj).__mro__:
                if base.__name__ == "Enum":
                    return obj.value
        return obj

    # ------------------------------------------------------------------
    # Report renderers
    # ------------------------------------------------------------------

    def to_yaml_report(self, fact_group: FactGroupEvaluation) -> str:
        """
        Render a :class:`FactGroupEvaluation` as a YAML-formatted per-measure
        report, mirroring the transpiler's output style.

        The report includes one entry per measure with its status, confidence,
        applied transformations, issues, and warnings.

        Returns
        -------
        str
            YAML text.
        """
        report_data = {
            "fact_group": fact_group.name,
            "source_table": fact_group.source_table,
            "summary": {
                "total_measures": fact_group.total_measures,
                "converted": fact_group.converted,
                "partial": fact_group.partial,
                "unsupported": fact_group.unsupported,
                "manual_overrides": fact_group.manual_overrides,
                "excluded": fact_group.excluded,
                "conversion_rate": f"{fact_group.conversion_rate:.1f}%",
                "validation_status": fact_group.validation_status,
                "dimensions_count": fact_group.dimensions_count,
                "joins_count": fact_group.joins_count,
            },
            "measures": [
                self._measure_to_yaml_dict(m) for m in fact_group.measures
            ],
        }

        try:
            return yaml.dump(
                report_data,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
                width=120,
            )
        except Exception:
            # Fallback: inline JSON-as-YAML (always parseable)
            return json.dumps(report_data, indent=2, ensure_ascii=False)

    def to_text_summary(self, summary: PipelineSummary) -> str:
        """
        Render a :class:`PipelineSummary` as a fixed-width ASCII table.

        Example output::

            PIPELINE SUMMARY
              Fact Group          Total   Conv  Part  Unsup  Over    Rate  Validation
              ---------------------------------------------------------------------------
              Sales                 114     89     5     15      5   78.1%  OK (3w)
              Service Feedback       96     72     3     18      3   75.0%  OK (5w)
              Global Measures        15     15     0      0      0  100.0%  OK
              ---------------------------------------------------------------------------
              TOTAL                 225    176     8     33      8   78.2%

        Column widths adapt to the longest fact-group name.

        Returns
        -------
        str
        """
        groups = summary.fact_groups

        # Determine dynamic name column width (minimum 12 characters)
        max_name_len = max(
            (len(g.name) for g in groups), default=12
        )
        max_name_len = max(max_name_len, len("Fact Group"), 12)
        name_w = max_name_len + 2  # padding

        # Column widths for numeric/text columns
        col_total = 6
        col_conv = 5
        col_part = 5
        col_unsup = 6
        col_over = 5
        col_rate = 7
        col_val = 12

        sep_len = (
            2 + name_w + col_total + col_conv + col_part
            + col_unsup + col_over + col_rate + col_val + 10
        )
        separator = "  " + "-" * (sep_len - 2)

        header = (
            "  "
            + "Fact Group".ljust(name_w)
            + "Total".rjust(col_total)
            + "Conv".rjust(col_conv)
            + "Part".rjust(col_part)
            + "Unsup".rjust(col_unsup)
            + "Over".rjust(col_over)
            + "Rate".rjust(col_rate)
            + "  Validation"
        )

        rows = []
        for g in groups:
            # Append warning count to validation string e.g. "OK (3w)"
            warn_count = sum(len(m.warnings) for m in g.measures)
            val_str = g.validation_status
            if warn_count:
                val_str += f" ({warn_count}w)"
            row = (
                "  "
                + g.name.ljust(name_w)
                + str(g.total_measures).rjust(col_total)
                + str(g.converted).rjust(col_conv)
                + str(g.partial).rjust(col_part)
                + str(g.unsupported).rjust(col_unsup)
                + str(g.manual_overrides).rjust(col_over)
                + f"{g.conversion_rate:.1f}%".rjust(col_rate)
                + f"  {val_str}"
            )
            rows.append(row)

        total_row = (
            "  "
            + "TOTAL".ljust(name_w)
            + str(summary.total_measures).rjust(col_total)
            + str(summary.converted).rjust(col_conv)
            + str(summary.partial).rjust(col_part)
            + str(summary.unsupported).rjust(col_unsup)
            + str(summary.manual_overrides).rjust(col_over)
            + f"{summary.overall_conversion_rate:.1f}%".rjust(col_rate)
        )

        lines = [
            "PIPELINE SUMMARY",
            f"  Model  : {summary.model_name}",
            f"  Target : {summary.target_catalog}.{summary.target_schema}",
            f"  Ran in : {summary.duration_seconds:.3f}s  "
            f"({summary.started_at} -> {summary.completed_at})",
            "",
            header,
            separator,
            *rows,
            separator,
            total_row,
        ]

        return "\n".join(lines)

    def to_html_report(self, summary: PipelineSummary) -> str:
        """
        Render a :class:`PipelineSummary` as a self-contained HTML report
        suitable for embedding in email notifications or a web dashboard.

        The output uses inline CSS for maximum compatibility (no external
        stylesheets required).

        Returns
        -------
        str
            Full HTML document string.
        """
        # ---- styles ----
        style = """
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               margin: 0; padding: 24px; background: #f5f5f5; color: #1a1a1a; }
        h1   { font-size: 1.5rem; margin-bottom: 4px; }
        .meta { color: #555; font-size: 0.85rem; margin-bottom: 20px; }
        table { border-collapse: collapse; width: 100%; background: #fff;
                border-radius: 8px; overflow: hidden;
                box-shadow: 0 1px 3px rgba(0,0,0,.12); }
        th   { background: #1b3a5c; color: #fff; padding: 10px 14px;
               text-align: left; font-size: 0.82rem; white-space: nowrap; }
        th.num, td.num { text-align: right; }
        td   { padding: 9px 14px; font-size: 0.88rem;
               border-bottom: 1px solid #eee; }
        tr:last-child td { border-bottom: none; }
        tr.total-row td  { font-weight: 700; background: #f0f4f8; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
                 font-size: 0.78rem; font-weight: 600; }
        .ok      { background: #d1fae5; color: #065f46; }
        .warnings{ background: #fef3c7; color: #92400e; }
        .errors  { background: #fee2e2; color: #991b1b; }
        .rate-high  { color: #065f46; font-weight: 700; }
        .rate-med   { color: #92400e; font-weight: 700; }
        .rate-low   { color: #991b1b; font-weight: 700; }
        """

        def badge(status: str, extra: str = "") -> str:
            css = status.lower()
            label = f"{status} {extra}".strip()
            return f'<span class="badge {css}">{label}</span>'

        def rate_class(r: float) -> str:
            if r >= 90:
                return "rate-high"
            if r >= 70:
                return "rate-med"
            return "rate-low"

        # ---- table rows ----
        tbody_rows = []
        for g in summary.fact_groups:
            warn_count = sum(len(m.warnings) for m in g.measures)
            val_extra = f"({warn_count}w)" if warn_count else ""
            val_badge = badge(g.validation_status, val_extra)
            rc = rate_class(g.conversion_rate)
            tbody_rows.append(
                f"<tr>"
                f"<td>{g.name}</td>"
                f"<td>{g.source_table}</td>"
                f'<td class="num">{g.total_measures}</td>'
                f'<td class="num">{g.converted}</td>'
                f'<td class="num">{g.partial}</td>'
                f'<td class="num">{g.unsupported}</td>'
                f'<td class="num">{g.manual_overrides}</td>'
                f'<td class="num">{g.excluded}</td>'
                f'<td class="num"><span class="{rc}">{g.conversion_rate:.1f}%</span></td>'
                f"<td>{val_badge}</td>"
                f"</tr>"
            )

        total_rc = rate_class(summary.overall_conversion_rate)
        total_row = (
            f'<tr class="total-row">'
            f'<td colspan="2">TOTAL ({len(summary.fact_groups)} groups)</td>'
            f'<td class="num">{summary.total_measures}</td>'
            f'<td class="num">{summary.converted}</td>'
            f'<td class="num">{summary.partial}</td>'
            f'<td class="num">{summary.unsupported}</td>'
            f'<td class="num">{summary.manual_overrides}</td>'
            f'<td class="num">{summary.excluded}</td>'
            f'<td class="num"><span class="{total_rc}">'
            f"{summary.overall_conversion_rate:.1f}%</span></td>"
            f"<td></td>"
            f"</tr>"
        )

        # ---- per-group detail sections ----
        detail_sections = []
        for g in summary.fact_groups:
            measure_rows = []
            for m in g.measures:
                issues_html = (
                    "<ul style='margin:0;padding-left:16px'>"
                    + "".join(f"<li>{i}</li>" for i in m.issues)
                    + "</ul>"
                ) if m.issues else "&#8212;"
                warnings_html = (
                    "<ul style='margin:0;padding-left:16px'>"
                    + "".join(f"<li>{w}</li>" for w in m.warnings)
                    + "</ul>"
                ) if m.warnings else "&#8212;"
                status_badge = badge(
                    m.status.replace("_", " ").title()
                )
                measure_rows.append(
                    f"<tr>"
                    f"<td><code style='font-size:.82rem'>{m.name}</code></td>"
                    f"<td>{status_badge}</td>"
                    f'<td class="num">{m.confidence}</td>'
                    f"<td style='font-size:.78rem;color:#555'>"
                    f"{', '.join(m.applied_transformations) or '&#8212;'}</td>"
                    f"<td style='font-size:.78rem'>{issues_html}</td>"
                    f"<td style='font-size:.78rem'>{warnings_html}</td>"
                    f"</tr>"
                )
            detail_sections.append(
                f"<h2 style='margin-top:32px;font-size:1.1rem'>{g.name}"
                f" <span style='font-size:.8rem;color:#777'>({g.source_table})</span></h2>"
                f"<table>"
                f"<thead><tr>"
                f"<th>Measure</th><th>Status</th>"
                f"<th class='num'>Confidence</th><th>Transformations</th>"
                f"<th>Issues</th><th>Warnings</th>"
                f"</tr></thead>"
                f"<tbody>{''.join(measure_rows)}</tbody>"
                f"</table>"
            )

        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        html = (
            "<!DOCTYPE html>\n"
            '<html lang="en">\n'
            "<head>\n"
            '  <meta charset="UTF-8" />\n'
            '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
            f"  <title>Migration Report \u2014 {summary.model_name}</title>\n"
            f"  <style>{style}</style>\n"
            "</head>\n"
            "<body>\n"
            "  <h1>Power BI \u2192 Databricks Migration Report</h1>\n"
            '  <p class="meta">\n'
            f"    Model: <strong>{summary.model_name}</strong> &nbsp;|&nbsp;\n"
            f"    Target: <strong>{summary.target_catalog}.{summary.target_schema}</strong> &nbsp;|&nbsp;\n"
            f"    Duration: <strong>{summary.duration_seconds:.3f}s</strong> &nbsp;|&nbsp;\n"
            f"    Generated: {generated}\n"
            "  </p>\n"
            "\n"
            '  <h2 style="font-size:1.1rem">Pipeline Summary</h2>\n'
            "  <table>\n"
            "    <thead>\n"
            "      <tr>\n"
            "        <th>Fact Group</th>\n"
            "        <th>Source Table</th>\n"
            '        <th class="num">Total</th>\n'
            '        <th class="num">Conv</th>\n'
            '        <th class="num">Part</th>\n'
            '        <th class="num">Unsup</th>\n'
            '        <th class="num">Overrides</th>\n'
            '        <th class="num">Excluded</th>\n'
            '        <th class="num">Rate</th>\n'
            "        <th>Validation</th>\n"
            "      </tr>\n"
            "    </thead>\n"
            "    <tbody>\n"
            f"      {''.join(tbody_rows)}\n"
            f"      {total_row}\n"
            "    </tbody>\n"
            "  </table>\n"
            "\n"
            f"  {''.join(detail_sections)}\n"
            "</body>\n"
            "</html>"
        )
        return html

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise_status(raw: str, translated_sql: Optional[str]) -> str:
        """
        Map a raw status string (potentially with different casing / aliases)
        to one of the canonical status values.
        """
        normalised = (raw or "").lower().strip().replace(" ", "_").replace("-", "_")

        canonical = {
            "converted": "converted",
            "success": "converted",
            "ok": "converted",
            "partial": "partial",
            "partial_success": "partial",
            "unsupported": "unsupported",
            "failed": "unsupported",
            "error": "unsupported",
            "manual_override": "manual_override",
            "override": "manual_override",
            "overridden": "manual_override",
            "excluded": "excluded",
            "skipped": "excluded",
            "ignored": "excluded",
        }

        if normalised in canonical:
            return canonical[normalised]

        # Infer from presence of SQL when status is ambiguous
        if translated_sql:
            return "converted"
        return "unsupported"

    @staticmethod
    def _compute_confidence(
        base: int,
        status: str,
        issues: list[str],
        warnings: list[str],
    ) -> int:
        """
        Derive the final confidence score from the translator's base score,
        the normalised status, and any issues / warnings.

        Rules applied in order:

        1. If the translator gave a non-zero base, start from it.
        2. Otherwise, pick a reasonable default per status.
        3. Deduct 10 per issue and 3 per warning (floors at 0).
        4. Clamp to [0, 100].
        """
        if base > 0:
            score = base
        else:
            defaults = {
                "converted": 90,
                "partial": 60,
                "unsupported": 0,
                "manual_override": 85,
                "excluded": 100,
            }
            score = defaults.get(status, 50)

        score -= len(issues) * 10
        score -= len(warnings) * 3

        # Hard caps per status
        caps = {
            "unsupported": 0,
            "partial": 79,
            "converted": 100,
            "manual_override": 100,
            "excluded": 100,
        }
        cap = caps.get(status, 100)
        return max(0, min(cap, score))

    @staticmethod
    def _measure_to_yaml_dict(m: MeasureEvaluation) -> dict:
        """Flatten a :class:`MeasureEvaluation` to a plain dict for YAML output."""
        d: dict[str, Any] = {
            "name": m.name,
            "status": m.status,
            "confidence": m.confidence,
        }
        if m.display_folder:
            d["display_folder"] = m.display_folder
        if m.format_string:
            d["format_string"] = m.format_string
        d["original_dax"] = m.original_dax
        d["translated_sql"] = m.translated_sql or ""
        if m.applied_transformations:
            d["applied_transformations"] = m.applied_transformations
        if m.issues:
            d["issues"] = m.issues
        if m.warnings:
            d["warnings"] = m.warnings
        if m.window_spec:
            d["window_spec"] = m.window_spec
        return d
