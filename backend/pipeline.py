"""
Migration Pipeline Orchestrator

End-to-end pipeline that connects extraction, translation, validation,
YAML generation, evaluation reporting, and deployment.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from backend.dax_translator import DAXTranslator
from backend.evaluator import EvaluationReporter, MeasureEvaluation
from backend.overrides import Overrides, OverridesManager
from backend.validator import MetricViewValidator
from backend.yaml_generator import MetricViewYAMLGenerator
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
    dashboard_id: str = ""
    dashboard_url: str = ""
    dashboard_spec: Optional[dict] = None
    rls_notes: List[str] = field(default_factory=list)
    rls_scaffolding: str = ""

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
            "dashboard_id": self.dashboard_id,
            "dashboard_url": self.dashboard_url,
            "rls_notes": self.rls_notes,
            "rls_scaffolding": self.rls_scaffolding,
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
        try:
            fact_tables = [t for t in filtered_tables if t.get("measures")]
            for fact in fact_tables:
                measures = fact.get("measures", [])
                translator = DAXTranslator(
                    relationships=relationships,
                    known_measures={},
                )
                translations = translator.translate_batch(measures, fact.get("name", ""))
                for m, tr in zip(measures, translations):
                    all_translations[m["name"]] = tr
                    m["translated_sql"] = tr.translated_sql
                    m["translation_status"] = tr.status
                    m["confidence"] = tr.confidence
                    m["window"] = tr.window_spec

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
            gen_results = self.yaml_gen.generate_from_model(
                model={"name": model.get("name", ""), "tables": filtered_tables,
                       "relationships": relationships},
                catalog=config.catalog,
                schema=config.schema,
            )
            # Keyed by sanitized source table so the evaluate step can attach
            # per-view info: excluded measures + converted dimension columns.
            excluded_by_source = {}
            dims_by_source = {}
            for spec, yaml_str, ddl_str in gen_results:
                group_name = spec.view_name.split(".")[-1] if spec.view_name else "default"
                result.generated_yaml[group_name] = yaml_str
                result.generated_sql[group_name] = ddl_str
                src_key = spec.source.split(".")[-1] if spec.source else group_name
                excluded_by_source[src_key] = dict(getattr(spec, "excluded_measures", {}) or {})
                dims_by_source[src_key] = [
                    {"name": d.name, "expr": d.expr} for d in getattr(spec, "dimensions", [])
                ]
            self._excluded_by_source = excluded_by_source
            self._dims_by_source = dims_by_source

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
            for fact in [t for t in filtered_tables if t.get("measures")]:
                src_key = fact.get("name", "").lower().replace(" ", "_")
                excluded = excluded_by_source.get(src_key, {})
                measure_evals = []
                for m in fact.get("measures", []):
                    tr = all_translations.get(m["name"])
                    if tr:
                        me = self.evaluator.evaluate_measure(m["name"], m.get("expression", ""), tr)
                        # Flag measures the generator dropped from the deployable view.
                        if m["name"] in excluded:
                            me.deployed = False
                            me.exclusion_reason = excluded[m["name"]]
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
