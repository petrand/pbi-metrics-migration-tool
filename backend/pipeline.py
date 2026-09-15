"""
Migration Pipeline Orchestrator

End-to-end pipeline that connects extraction, translation, validation,
YAML generation, evaluation reporting, and deployment.
"""

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.dax_translator import DAXTranslator
from backend.evaluator import EvaluationReporter, MeasureEvaluation
from backend.overrides import Overrides, OverridesManager
from backend.validator import MetricViewValidator
from backend.yaml_generator import MetricViewYAMLGenerator, _sanitize_name
from backend.dashboard_generator import DashboardGenerator
from backend.lakeview_client import LakeviewClient
from backend import rls_generator

logger = logging.getLogger(__name__)


@dataclass
class MigrationConfig:
    """Configuration for a migration run."""
    catalog: str = "main"
    schema: str = "default"
    warehouse_id: str = ""
    deploy: bool = False
    dry_run: bool = False
    validate_only: bool = False
    overrides: Optional[Overrides] = None
    convert_nested_windows: bool = True
    generate_dashboard: bool = False
    dashboard_name: str = ""


@dataclass
class MigrationStep:
    """Represents a single step in the migration pipeline."""
    name: str
    status: str = "pending"  # pending, running, completed, failed, skipped
    message: str = ""
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: int = 0
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MigrationResult:
    """Full result of a migration run."""
    migration_id: str = ""
    status: str = "pending"
    steps: List[MigrationStep] = field(default_factory=list)
    model_name: str = ""
    fact_groups: List[dict] = field(default_factory=list)
    generated_yaml: Dict[str, str] = field(default_factory=dict)
    generated_sql: Dict[str, str] = field(default_factory=dict)
    pipeline_summary: Optional[dict] = None
    manifest: Optional[dict] = None
    validation_result: Optional[dict] = None
    deployment_results: List[dict] = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    duration_ms: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    dashboard_id: str = ""
    dashboard_url: str = ""
    dashboard_spec: Optional[dict] = None
    rls_notes: List[str] = field(default_factory=list)
    rls_scaffolding: str = ""
    # Tables with no measures (no DAX) — not converted to a metric view; they are
    # pass-through Delta tables (dimensions / lookups). Each: {name, columns, note}.
    passthrough_tables: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "migration_id": self.migration_id,
            "status": self.status,
            "model_name": self.model_name,
            "steps": [{"name": s.name, "status": s.status, "message": s.message,
                       "duration_ms": s.duration_ms} for s in self.steps],
            "fact_groups_count": len(self.fact_groups),
            "generated_yaml": self.generated_yaml,
            "generated_sql": self.generated_sql,
            "pipeline_summary": self.pipeline_summary,
            "validation_result": self.validation_result,
            "deployment_results": self.deployment_results,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms,
            "errors": self.errors,
            "warnings": self.warnings,
            "dashboard_id": self.dashboard_id,
            "dashboard_url": self.dashboard_url,
            "rls_notes": self.rls_notes,
            "rls_scaffolding": self.rls_scaffolding,
            "passthrough_tables": self.passthrough_tables,
        }


class MigrationPipeline:
    """Orchestrates the full Power BI to Databricks migration pipeline."""

    def __init__(self):
        self.translator = DAXTranslator()
        self.yaml_gen = MetricViewYAMLGenerator()
        self.validator = MetricViewValidator()
        self.evaluator = EvaluationReporter()
        self.overrides_mgr = OverridesManager()
        self._callbacks: List = []

    def on_progress(self, callback):
        """Register a progress callback: callback(step_name, pct, message)."""
        self._callbacks.append(callback)

    def _notify(self, step: str, pct: int, msg: str):
        for cb in self._callbacks:
            try:
                cb(step, pct, msg)
            except Exception:
                pass

    def run(self, model: dict, config: MigrationConfig,
            dbx_client=None) -> MigrationResult:
        """Run the full migration pipeline.

        Args:
            model: Semantic model dict with 'name', 'tables', 'relationships'.
            config: Migration configuration.
            dbx_client: Optional DatabricksClient for deployment.

        Returns:
            MigrationResult with all outputs.
        """
        result = MigrationResult(
            migration_id=str(uuid.uuid4()),
            model_name=model.get("name", "Unknown"),
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        pipeline_start = time.monotonic()

        overrides = config.overrides or Overrides()

        # ── Step 1: Extract / Validate Input ──
        step = self._start_step("extract")
        result.steps.append(step)
        try:
            tables = model.get("tables", [])
            relationships = model.get("relationships", [])
            total_measures = sum(len(t.get("measures", [])) for t in tables)
            self._notify("extract", 15, f"Model loaded: {len(tables)} tables, {total_measures} measures")
            step.details = {"tables": len(tables), "measures": total_measures,
                           "relationships": len(relationships)}
            self._complete_step(step, f"Extracted {len(tables)} tables, {total_measures} measures")
        except Exception as e:
            self._fail_step(step, str(e))
            result.errors.append(str(e))
            result.status = "failed"
            return result

        # ── Step 2: Apply Overrides ──
        step = self._start_step("overrides")
        result.steps.append(step)
        try:
            # Filter excluded tables
            filtered_tables = [
                t for t in tables
                if not self.overrides_mgr.should_exclude_table(overrides, t.get("name", ""))
            ]
            # Filter excluded measures within tables
            for t in filtered_tables:
                if "measures" in t:
                    t["measures"] = [
                        m for m in t["measures"]
                        if not self.overrides_mgr.should_exclude_measure(overrides, m.get("name", ""))
                    ]
            self._notify("overrides", 25, "Overrides applied")
            self._complete_step(step, f"Filtered to {len(filtered_tables)} tables")
        except Exception as e:
            self._fail_step(step, str(e))
            filtered_tables = tables

        if config.dry_run:
            self._notify("dry_run", 100, "Dry run complete")
            result.status = "dry_run"
            result.completed_at = datetime.now(timezone.utc).isoformat()
            result.duration_ms = int((time.monotonic() - pipeline_start) * 1000)
            return result

        # ── Step 3: Translate DAX ──
        step = self._start_step("translate")
        result.steps.append(step)
        all_translations = {}
        # Row-level-security / measure-security tables: those named in RLS role
        # permissions, plus tables whose name signals security (e.g. "Measure
        # Security", "User Security"). Measures whose home table is one of these
        # are permission-check measures used to gate other measures in-DAX.
        security_tables = {
            tname for role in model.get("roles", [])
            for tname in (role.get("tablePermissions", {}) or {})
        }
        security_tables |= {
            t.get("name", "") for t in model.get("tables", [])
            if re.search(r'security|permission', t.get("name", ""), re.IGNORECASE)
        }
        security_measures = {
            m["name"] for t in model.get("tables", []) if t.get("name", "") in security_tables
            for m in t.get("measures", [])
        }
        # Tables with no measures carry no DAX to convert — they are not turned
        # into a metric view. They are pass-through Delta tables (dimensions /
        # lookups) consumed as-is (and joined into the fact views).
        result.passthrough_tables = [
            {
                "name": _sanitize_name(t.get("name", "")),
                "display_name": t.get("name", ""),
                "columns": len(t.get("columns", []) or []),
                "note": "No DAX found — pass-through Delta table (no metric view needed)",
            }
            for t in model.get("tables", []) if not t.get("measures")
        ]
        try:
            fact_tables = [t for t in filtered_tables if t.get("measures")]
            for fact in fact_tables:
                measures = fact.get("measures", [])
                translator = DAXTranslator(
                    relationships=relationships,
                    known_measures={},
                    security_tables=security_tables,
                    security_measures=security_measures,
                )
                translations = translator.translate_batch(measures, fact.get("name", ""))
                for m, tr in zip(measures, translations):
                    all_translations[m["name"]] = tr
                    m["translated_sql"] = tr.translated_sql
                    m["translation_status"] = tr.status
                    m["confidence"] = tr.confidence
                    m["window"] = tr.window_spec
                    m["rls_applied"] = tr.rls_applied

            converted = sum(1 for t in all_translations.values() if t.status == "converted")
            self._notify("translate", 50, f"Translated {converted}/{len(all_translations)} measures")
            step.details = {"total": len(all_translations), "converted": converted}
            self._complete_step(step, f"Translated {converted}/{len(all_translations)} measures")
        except Exception as e:
            self._fail_step(step, str(e))
            result.errors.append(f"Translation error: {e}")

        # ── Step 4: Generate YAML + SQL ──
        step = self._start_step("generate")
        result.steps.append(step)
        try:
            # Real column sets per table (sanitized to match emitted refs) so the
            # generator can prune measures/dimensions/joins referencing columns
            # that don't exist on the physically-loaded tables — the TMDL model
            # can drift from what actually gets created/queried.
            known_columns = {
                _sanitize_name(t.get("name", "")): {
                    _sanitize_name(c.get("name", ""))
                    for c in t.get("columns", []) if c.get("name")
                }
                for t in filtered_tables if t.get("name")
            }
            gen_results = self.yaml_gen.generate_from_model(
                model={"name": model.get("name", ""), "tables": filtered_tables,
                       "relationships": relationships},
                catalog=config.catalog,
                schema=config.schema,
                convert_nested_windows=config.convert_nested_windows,
                known_columns=known_columns,
            )
            # The generated metric views read from physical tables that must
            # exist first. Prepend `CREATE TABLE IF NOT EXISTS` DDL for every
            # table a view references (source + joins), so `generated_sql` is
            # dependency-ordered: tables first, then the views that depend on them.
            table_entries = self.yaml_gen.build_table_entries(gen_results, filtered_tables)
            gen_results = table_entries + gen_results
            # Keyed by sanitized source table so the evaluate step can attach
            # per-view info: excluded measures + converted dimension columns.
            excluded_by_source = {}
            dims_by_source = {}
            deployed_by_source = {}
            for spec, yaml_str, ddl_str in gen_results:
                group_name = spec.view_name.split(".")[-1] if spec.view_name else "default"
                result.generated_sql[group_name] = ddl_str
                # Dependency table entries (CREATE TABLE the views read from) carry
                # SQL but no metric-view YAML — record their DDL (kept ahead of the
                # views in insertion order) and skip the metric-view bookkeeping.
                if not yaml_str:
                    continue
                result.generated_yaml[group_name] = yaml_str
                src_key = spec.source.split(".")[-1] if spec.source else group_name
                excluded_by_source[src_key] = dict(getattr(spec, "excluded_measures", {}) or {})
                dims_by_source[src_key] = [
                    {"name": d.name, "expr": d.expr} for d in getattr(spec, "dimensions", [])
                ]
                # Measures that actually made it into this deployable view.
                deployed_by_source[src_key] = {m.name for m in getattr(spec, "measures", [])}
                # Surface build-time notes (e.g. offset-pushdown calendar-alignment
                # verify warnings) so they aren't lost.
                for note in getattr(spec, "build_warnings", []) or []:
                    result.warnings.append(f"{group_name}: {note}")
            # Fact groups whose view was skipped entirely still carry exclusions
            # that must be surfaced — merge them so no drop goes unreported.
            for src_key, excl in getattr(self.yaml_gen, "skipped_exclusions", {}).items():
                excluded_by_source.setdefault(src_key, {}).update(excl)
                deployed_by_source.setdefault(src_key, set())
            self._excluded_by_source = excluded_by_source
            self._dims_by_source = dims_by_source
            self._deployed_by_source = deployed_by_source

            self._notify("generate", 65, f"Generated {len(gen_results)} metric view(s)")
            self._complete_step(step, f"Generated {len(gen_results)} metric view DDL(s)")
        except Exception as e:
            self._fail_step(step, str(e))
            result.errors.append(f"Generation error: {e}")

        # ── Step 5: Validate ──
        step = self._start_step("validate")
        result.steps.append(step)
        try:
            all_issues = []
            for name, ddl in result.generated_sql.items():
                vr = self.validator.validate_ddl(ddl)
                all_issues.extend(vr.issues)

            errors = [i for i in all_issues if i.severity == "error"]
            warnings = [i for i in all_issues if i.severity == "warning"]
            result.validation_result = {
                "valid": len(errors) == 0,
                "errors": len(errors),
                "warnings": len(warnings),
                "issues": [{"severity": i.severity, "category": i.category,
                           "message": i.message, "location": i.location}
                          for i in all_issues],
            }
            self._notify("validate", 75, f"Validation: {len(errors)} errors, {len(warnings)} warnings")
            self._complete_step(step, f"{len(errors)} errors, {len(warnings)} warnings")
        except Exception as e:
            self._fail_step(step, str(e))

        if config.validate_only:
            result.status = "validated"
            result.completed_at = datetime.now(timezone.utc).isoformat()
            result.duration_ms = int((time.monotonic() - pipeline_start) * 1000)
            return result

        # ── Step 6: Evaluate ──
        step = self._start_step("evaluate")
        result.steps.append(step)
        try:
            fact_group_evals = []
            excluded_by_source = getattr(self, "_excluded_by_source", {})
            deployed_by_source = getattr(self, "_deployed_by_source", None)
            # Only enforce the per-measure "accounted for" invariant when the
            # generate step actually completed; if it failed outright, that error
            # is already recorded and per-measure errors would be redundant noise.
            gen_ok = deployed_by_source is not None
            deployed_by_source = deployed_by_source or {}
            for fact in [t for t in filtered_tables if t.get("measures")]:
                # Use the same sanitizer the generator keys on, so the lookup
                # never misses for names with spaces/special characters.
                src_key = _sanitize_name(fact.get("name", ""))
                excluded = excluded_by_source.get(src_key, {})
                deployed = deployed_by_source.get(src_key, set())
                measure_evals = []
                for m in fact.get("measures", []):
                    name = m["name"]
                    tr = all_translations.get(name)
                    if tr is None:
                        # No translation was produced — the measure is dropped.
                        # Never let that happen silently.
                        msg = f"{fact.get('name', '')}.{name}: dropped — no translation was produced"
                        result.warnings.append(msg)
                        logger.warning("Measure dropped: %s", msg)
                        continue
                    me = self.evaluator.evaluate_measure(name, m.get("expression", ""), tr)
                    if name in excluded:
                        # Generator excluded it from the deployable view.
                        me.deployed = False
                        me.exclusion_reason = excluded[name]
                        me.warnings.append(f"Excluded from deployed view: {excluded[name]}")
                        msg = f"{fact.get('name', '')}.{name}: excluded from view — {excluded[name]}"
                        result.warnings.append(msg)
                        logger.warning("Measure excluded: %s", msg)
                    elif gen_ok and name not in deployed:
                        # Backstop: generation completed but this measure is
                        # neither in the deployed view nor explicitly excluded.
                        # That is an unaccounted drop — surface it as an error,
                        # never silently.
                        me.deployed = False
                        me.exclusion_reason = "dropped without a recorded reason (unexpected)"
                        msg = f"{fact.get('name', '')}.{name}: dropped from view with no recorded reason"
                        result.errors.append(msg)
                        logger.error("Measure dropped without reason: %s", msg)
                    measure_evals.append(me)

                dims = getattr(self, "_dims_by_source", {}).get(src_key, [])
                fg = self.evaluator.evaluate_fact_group(
                    group_name=fact.get("name", ""),
                    source_table=f"{config.catalog}.{config.schema}.{fact.get('name', '').lower().replace(' ', '_')}",
                    measures=measure_evals,
                    dims_count=len(dims),
                    joins_count=len(relationships),
                )
                fg.dimensions = dims
                fact_group_evals.append(fg)

            summary = self.evaluator.generate_pipeline_summary(
                fact_groups=fact_group_evals,
                model_name=model.get("name", ""),
                catalog=config.catalog,
                schema=config.schema,
                duration=time.monotonic() - pipeline_start,
                started_at=result.started_at,
                total_relationships=len(relationships),
            )
            result.pipeline_summary = self.evaluator.to_dict(summary)
            text_summary = self.evaluator.to_text_summary(summary)
            self._notify("evaluate", 85, "Evaluation report generated")
            logger.info("Pipeline summary:\n%s", text_summary)
            self._complete_step(step, "Report generated")
        except Exception as e:
            self._fail_step(step, str(e))

        # ── Step 6a: Row-Level Security (report + scaffolding) ──
        roles = model.get("roles", [])
        if rls_generator.roles_with_rls(roles):
            step = self._start_step("rls")
            result.steps.append(step)
            try:
                result.rls_notes = rls_generator.rls_notes(roles)
                result.rls_scaffolding = rls_generator.generate_rls_scaffolding(
                    roles, config.catalog, config.schema
                )
                for note in result.rls_notes:
                    logger.warning("RLS: %s", note)
                self._complete_step(
                    step,
                    f"{len(result.rls_notes)} role(s) with RLS need manual UC row filters",
                )
            except Exception as e:
                self._fail_step(step, str(e))

        # ── Step 7: Deploy (optional) ──
        if config.deploy and dbx_client and config.warehouse_id:
            step = self._start_step("deploy")
            result.steps.append(step)
            try:
                for name, ddl in result.generated_sql.items():
                    self._notify("deploy", 90, f"Deploying {name}...")
                    dep_result = dbx_client.deploy_metric_view(
                        sql=ddl,
                        warehouse_id=config.warehouse_id,
                        catalog=config.catalog,
                        schema=config.schema,
                    )
                    result.deployment_results.append(dep_result.to_dict())

                all_ok = all(d.get("status") == "success" for d in result.deployment_results)
                self._notify("deploy", 100, "Deployment complete" if all_ok else "Some deployments failed")
                self._complete_step(step, f"Deployed {len(result.deployment_results)} view(s)")
            except Exception as e:
                self._fail_step(step, str(e))
                result.errors.append(f"Deployment error: {e}")
        else:
            self._notify("complete", 100, "Pipeline complete (no deployment)")

        # ── Step 7a: Generate Dashboard (optional) ──
        if config.generate_dashboard and dbx_client:
            step = self._start_step("dashboard")
            result.steps.append(step)
            try:
                dash_gen = DashboardGenerator()
                # Build metric view spec list from generated YAML
                mv_specs = []
                for fg in result.fact_groups:
                    fg_name = fg.get("name", "") if isinstance(fg, dict) else str(fg)
                    mv_specs.append({
                        "fact_group": fg_name,
                        "yaml": result.generated_yaml.get(fg_name, ""),
                        "sql": result.generated_sql.get(fg_name, ""),
                    })
                spec = dash_gen.generate_from_metric_views(
                    metric_view_specs=mv_specs,
                    model_name=result.model_name,
                    catalog=config.catalog,
                    schema=config.schema,
                )
                result.dashboard_spec = spec.to_dict()

                if not config.dry_run:
                    lv_client = LakeviewClient(dbx_client)
                    dash_name = config.dashboard_name or f"{result.model_name} Dashboard"
                    dash = lv_client.deploy_dashboard(
                        display_name=dash_name,
                        serialized_dashboard=spec.to_json(),
                        warehouse_id=config.warehouse_id,
                        publish=True,
                    )
                    result.dashboard_id = dash.dashboard_id
                    result.dashboard_url = dash.published_url or lv_client.get_published_url(dash.dashboard_id)
                    self._complete_step(step, f"Dashboard deployed: {result.dashboard_url}")
                else:
                    self._complete_step(step, "Dashboard spec generated (dry run)")
                self._notify("dashboard", 95, "Dashboard ready")
            except Exception as e:
                self._fail_step(step, str(e))
                result.errors.append(f"Dashboard generation failed: {e}")
                logger.warning("Dashboard step failed: %s", e)

        # Finalize
        result.completed_at = datetime.now(timezone.utc).isoformat()
        result.duration_ms = int((time.monotonic() - pipeline_start) * 1000)
        result.status = "failed" if result.errors else "complete"
        return result

    def _start_step(self, name: str) -> MigrationStep:
        return MigrationStep(
            name=name, status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
        )

    def _complete_step(self, step: MigrationStep, message: str):
        step.status = "completed"
        step.message = message
        now = datetime.now(timezone.utc).isoformat()
        step.completed_at = now
        if step.started_at:
            try:
                start = datetime.fromisoformat(step.started_at)
                end = datetime.fromisoformat(now)
                step.duration_ms = int((end - start).total_seconds() * 1000)
            except Exception:
                pass

    def _fail_step(self, step: MigrationStep, message: str):
        step.status = "failed"
        step.message = message
        step.completed_at = datetime.now(timezone.utc).isoformat()
