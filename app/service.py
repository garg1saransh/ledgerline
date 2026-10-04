"""Plan versioning, approval gate, dry run, execute, retry, rollback, and reconciliation."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from app.agent import propose_plan
from app.catalog import (
    DATA_DIR,
    MAX_SAMPLE_SIZE,
    SOURCE_SCHEMA,
    TARGET_SCHEMA,
    TRANSFORMS,
    bounded_sample,
    source_field_names,
    target_field_map,
    transform_names,
)
from app.db import Database, dump, load, utc_now
from app.engine import COUNT_DEFINITIONS, apply_against_target, evaluate_plan, raw_source_totals, totals_for
from app.model_agent import ModelAgentError, ModelNotConfigured, model_status, propose_with_model

ALLOWED_PARAMS = {"format", "map", "separator", "value", "message"}
DATE_DIRECTIVES = set("YymdHMS")


class ServiceError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Workbench:
    def __init__(self, database: Database):
        self.db = database

    def inputs(self) -> dict:
        sample = bounded_sample()
        return {
            "source_schema": SOURCE_SCHEMA,
            "target_schema": TARGET_SCHEMA,
            "transforms": TRANSFORMS,
            "sample": sample,
            "sample_count": len(sample),
            "max_sample_size": MAX_SAMPLE_SIZE,
            "scope": {
                "sources": 1,
                "targets": 1,
                "max_sample_size": MAX_SAMPLE_SIZE,
                "mock_target": "SQLite staging table target_customers",
                "excluded": [
                    "production database access",
                    "arbitrary transformation code",
                    "distributed migration",
                    "live cloud connectors",
                ],
            },
            "count_definitions": COUNT_DEFINITIONS,
            "input_files": {
                "directory": str(DATA_DIR),
                "manifest": "data/manifest.json",
                "source_schema": "data/source_schema.json",
                "target_schema": "data/target_schema.json",
                "transforms": "data/transforms.json",
                "sample": "data/sample.json",
            },
            "agent": model_status(),
        }

    def propose(self, mode: str = "rules") -> dict:
        if mode == "rules":
            body = propose_plan()
            body["agent_mode"] = "rules"
        elif mode == "model":
            try:
                body = propose_with_model()
            except ModelNotConfigured as exc:
                raise ServiceError(409, str(exc)) from exc
            except ModelAgentError as exc:
                raise ServiceError(422, str(exc)) from exc
        else:
            raise ServiceError(400, "Agent mode must be rules or model.")
        body["mappings"] = normalize_mappings(body["mappings"])
        with self.db.session() as connection:
            plan = self._insert_plan(connection, body, parent_id=None, event="plan_proposed")
        return plan

    def list_plans(self) -> list[dict]:
        with self.db.session() as connection:
            rows = connection.execute("SELECT * FROM plans ORDER BY version").fetchall()
        return [self._plan_summary(row) for row in rows]

    def get_plan(self, plan_id: str) -> dict:
        with self.db.session() as connection:
            row = self._require_plan(connection, plan_id)
        return self._plan_detail(row)

    def save_revision(self, plan_id: str, mappings: list[dict]) -> dict:
        with self.db.session() as connection:
            current = self._require_plan(connection, plan_id)
            if current["status"] != "draft":
                raise ServiceError(409, "Only a draft plan can be revised. Approved plans stay immutable.")
            cleaned = normalize_mappings(mappings)
            body = load(current["body"])
            body["mappings"] = cleaned
            body["questions"] = sync_question_answers(body.get("questions") or [], cleaned)
            plan = self._insert_plan(connection, body, parent_id=plan_id, event="plan_revised")
        return plan

    def answer(self, plan_id: str, question_id: str, option_id: str) -> dict:
        with self.db.session() as connection:
            current = self._require_plan(connection, plan_id)
            if current["status"] != "draft":
                raise ServiceError(409, "Answers belong on a draft. Revise an approved plan before changing it.")
            body = load(current["body"])
            apply_answer(body, question_id, option_id)
            plan = self._insert_plan(
                connection,
                body,
                parent_id=plan_id,
                event="plan_revised",
                detail={"question_id": question_id, "option_id": option_id},
            )
        return plan

    def approve(self, plan_id: str, note: str | None) -> dict:
        with self.db.session() as connection:
            current = self._require_plan(connection, plan_id)
            if current["status"] != "draft":
                raise ServiceError(409, "Only a draft plan can be approved.")
            body = load(current["body"])
            open_questions = blocking_questions(body)
            if open_questions:
                names = ", ".join(question["id"] for question in open_questions)
                raise ServiceError(409, f"Answer the blocking questions before approval: {names}.")
            missing = missing_required(body["mappings"])
            if missing:
                raise ServiceError(409, "Required target fields are unmapped: " + ", ".join(missing) + ".")
            connection.execute(
                "UPDATE plans SET status = 'superseded' WHERE status = 'approved' AND id != ?",
                (plan_id,),
            )
            approved_at = utc_now()
            connection.execute(
                """
                UPDATE plans
                SET status = 'approved', approved_at = ?, approval_note = ?
                WHERE id = ?
                """,
                (approved_at, (note or "").strip() or None, plan_id),
            )
            self.db.add_history(
                connection,
                "plan_approved",
                {"version": current["version"], "note": (note or "").strip()},
                plan_id=plan_id,
            )
            row = self._require_plan(connection, plan_id)
        return self._plan_detail(row)

    def dry_run(self, plan_id: str) -> dict:
        with self.db.session() as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] == "superseded":
                raise ServiceError(409, "This plan version was superseded. Dry-run the current draft or approved plan.")
            evaluation = evaluate_plan(load(plan["body"])["mappings"])
            run = self._store_run(connection, plan, evaluation, mode="dry_run", is_retry=False)
        return run

    def execute(self, plan_id: str) -> dict:
        with self.db.session() as connection:
            plan = self._require_plan(connection, plan_id)
            if plan["status"] != "approved":
                raise ServiceError(409, "Execution requires an approved mapping and transformation plan.")
            prior = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE plan_id = ? AND mode = 'execute'",
                (plan_id,),
            ).fetchone()
            is_retry = int(prior["count"]) > 0
            evaluation = evaluate_plan(load(plan["body"])["mappings"])
            existing = {
                row["customer_id"]
                for row in connection.execute("SELECT customer_id FROM target_customers").fetchall()
            }
            applied = apply_against_target(evaluation, existing)
            run = self._store_run(connection, plan, applied, mode="execute", is_retry=is_retry)
            loaded_at = utc_now()
            for outcome in applied.inserted:
                connection.execute(
                    """
                    INSERT INTO target_customers(customer_id, payload, load_run_id, loaded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        outcome.output["customer_id"],
                        dump(outcome.output),
                        run["id"],
                        loaded_at,
                    ),
                )
        return run

    def rollback(self, run_id: str) -> dict:
        with self.db.session() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise ServiceError(404, "Run not found.")
            if run["mode"] != "execute":
                raise ServiceError(409, "Only an executed load can be rolled back.")
            if run["status"] == "rolled_back":
                raise ServiceError(409, "This load was already rolled back.")
            deleted = connection.execute(
                "DELETE FROM target_customers WHERE load_run_id = ?",
                (run_id,),
            ).rowcount
            rolled_at = utc_now()
            connection.execute(
                """
                UPDATE quarantine_cases
                SET status = 'open', released_run_id = NULL, updated_at = ?
                WHERE released_run_id = ? AND status = 'released'
                """,
                (rolled_at, run_id),
            )
            reopened = connection.execute("SELECT changes()").fetchone()[0]
            connection.execute(
                "UPDATE runs SET status = 'rolled_back', rolled_back_at = ? WHERE id = ?",
                (rolled_at, run_id),
            )
            self.db.add_history(
                connection,
                "migration_rolled_back",
                {"deleted_rows": deleted, "reopened_cases": reopened, "plan_id": run["plan_id"]},
                plan_id=run["plan_id"],
                run_id=run_id,
            )
            updated = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            errors = self._quarantine_rows(connection, run_id)
            return self._run_detail(updated, errors)

    def list_runs(self) -> list[dict]:
        with self.db.session() as connection:
            rows = connection.execute("SELECT * FROM runs ORDER BY created_at, id").fetchall()
        return [self._run_summary(row) for row in rows]

    def get_run(self, run_id: str) -> dict:
        with self.db.session() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise ServiceError(404, "Run not found.")
            errors = self._quarantine_rows(connection, run_id)
        return self._run_detail(row, errors)

    def target_rows(self) -> dict:
        with self.db.session() as connection:
            rows = connection.execute(
                "SELECT customer_id, payload, load_run_id, loaded_at FROM target_customers ORDER BY customer_id"
            ).fetchall()
        records = []
        for row in rows:
            payload = load(row["payload"])
            payload["_load_run_id"] = row["load_run_id"]
            payload["_loaded_at"] = row["loaded_at"]
            records.append(payload)
        return {"count": len(records), "rows": records}

    def reconcile(self) -> dict:
        with self.db.session() as connection:
            approved = connection.execute(
                "SELECT * FROM plans WHERE status = 'approved' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            target_rows = connection.execute("SELECT payload FROM target_customers").fetchall()
        target_payloads = [load(row["payload"]) for row in target_rows]
        target_totals = _sum_payloads(target_payloads)
        if approved is None:
            return {
                "plan_id": None,
                "source_sample_count": len(bounded_sample()),
                "target_count": len(target_payloads),
                "expected": None,
                "target": target_totals,
                "raw_source": raw_source_totals(),
                "deltas": None,
                "note": "Approve a plan to compare accepted totals with staging.",
            }
        evaluation = evaluate_plan(load(approved["body"])["mappings"])
        expected = totals_for(evaluation.outcomes)
        raw_source = raw_source_totals()
        deltas = {
            "accepted_rows": str(int(target_totals["accepted_rows"]) - int(expected["accepted_rows"])),
            "credit_limit_sum": format(Decimal(target_totals["credit_limit_sum"]) - Decimal(expected["credit_limit_sum"]), "f"),
            "loyalty_points_sum": str(int(target_totals["loyalty_points_sum"]) - int(expected["loyalty_points_sum"])),
        }
        return {
            "plan_id": approved["id"],
            "plan_version": approved["version"],
            "source_sample_count": evaluation.source_count,
            "accepted_source_count": evaluation.accepted_count,
            "rejected_source_count": evaluation.rejected_count,
            "target_count": len(target_payloads),
            "expected": expected,
            "target": target_totals,
            "raw_source": raw_source,
            "deltas": deltas,
            "in_balance": all(value in {"0", "0.00"} for value in deltas.values()),
            "note": "Expected totals are the accepted rows of the latest approved plan. Target totals are the rows still loaded.",
        }

    def history(self) -> list[dict]:
        with self.db.session() as connection:
            rows = connection.execute("SELECT * FROM history ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "at": row["at"],
                "event_type": row["event_type"],
                "plan_id": row["plan_id"],
                "run_id": row["run_id"],
                "detail": load(row["detail"]),
            }
            for row in rows
        ]

    def reset(self) -> dict:
        with self.db.session() as connection:
            for table in ("history", "quarantine", "quarantine_cases", "target_customers", "runs", "plans"):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("UPDATE counters SET value = 0")
            self.db.add_history(connection, "workspace_reset", {"max_sample_size": MAX_SAMPLE_SIZE})
        return {"ok": True}

    def _insert_plan(self, connection, body: dict, parent_id: str | None, event: str, detail: dict | None = None) -> dict:
        version = connection.execute("SELECT COALESCE(MAX(version), 0) AS version FROM plans").fetchone()["version"] + 1
        plan_id = self.db.next_id(connection, "plan")
        connection.execute("UPDATE plans SET status = 'superseded' WHERE status = 'draft'")
        connection.execute(
            """
            INSERT INTO plans(id, version, parent_id, status, body, created_at)
            VALUES (?, ?, ?, 'draft', ?, ?)
            """,
            (plan_id, version, parent_id, dump(body), utc_now()),
        )
        self.db.add_history(
            connection,
            event,
            {"version": version, "parent_id": parent_id, **(detail or {})},
            plan_id=plan_id,
        )
        return self._plan_detail(self._require_plan(connection, plan_id))

    def _store_run(self, connection, plan, evaluation, mode: str, is_retry: bool) -> dict:
        run_id = self.db.next_id(connection, "run")
        detail = {
            "definitions": COUNT_DEFINITIONS,
            "skipped": evaluation.skipped,
            "inserted_keys": [outcome.output["customer_id"] for outcome in evaluation.inserted if outcome.output],
        }
        connection.execute(
            """
            INSERT INTO runs(
                id, plan_id, mode, status, is_retry, created_at,
                source_count, transformed_count, accepted_count, rejected_count,
                duplicate_skipped_count, fingerprint, detail
            ) VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                plan["id"],
                mode,
                1 if is_retry else 0,
                utc_now(),
                evaluation.source_count,
                evaluation.transformed_count,
                evaluation.accepted_count,
                evaluation.rejected_count,
                evaluation.duplicate_skipped_count,
                evaluation.fingerprint,
                dump(detail),
            ),
        )
        for outcome in evaluation.outcomes:
            if outcome.status != "rejected":
                continue
            case_id = f"{run_id}-{outcome.index:03d}"
            connection.execute(
                """
                INSERT INTO quarantine_cases(
                    id, run_id, source_index, source_key, record_json, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    case_id,
                    run_id,
                    outcome.index,
                    outcome.source_key,
                    dump(outcome.record or (outcome.errors[0].record if outcome.errors else {})),
                    utc_now(),
                ),
            )
            for error in outcome.errors:
                connection.execute(
                    """
                    INSERT INTO quarantine(run_id, case_id, source_key, field_name, source_value, rule, message, record_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        case_id,
                        error.source_key,
                        error.field,
                        error.value,
                        error.rule,
                        error.message,
                        dump(error.record),
                    ),
                )
        event = "migration_retried" if is_retry else ("dry_run_completed" if mode == "dry_run" else "migration_executed")
        self.db.add_history(
            connection,
            event,
            {
                "mode": mode,
                "source_count": evaluation.source_count,
                "transformed_count": evaluation.transformed_count,
                "accepted_count": evaluation.accepted_count,
                "rejected_count": evaluation.rejected_count,
                "duplicate_skipped_count": evaluation.duplicate_skipped_count,
                "fingerprint": evaluation.fingerprint,
            },
            plan_id=plan["id"],
            run_id=run_id,
        )
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        errors = self._quarantine_rows(connection, run_id)
        return self._run_detail(row, errors)

    def quarantine_inbox(self, run_id: str | None = None) -> dict:
        with self.db.session() as connection:
            if run_id:
                cases = connection.execute(
                    "SELECT * FROM quarantine_cases WHERE run_id = ? ORDER BY source_index",
                    (run_id,),
                ).fetchall()
            else:
                cases = connection.execute(
                    """
                    SELECT * FROM quarantine_cases
                    ORDER BY CASE status
                        WHEN 'open' THEN 0
                        WHEN 'skipped_duplicate' THEN 1
                        ELSE 2
                    END, updated_at DESC, id
                    """
                ).fetchall()
            return {"run_id": run_id, "cases": [self._case_detail(connection, case) for case in cases]}

    def release_case(self, case_id: str, record: dict | None) -> dict:
        with self.db.session() as connection:
            case = connection.execute("SELECT * FROM quarantine_cases WHERE id = ?", (case_id,)).fetchone()
            if case is None:
                raise ServiceError(404, "Quarantine case not found.")
            if case["status"] == "released":
                raise ServiceError(409, "This quarantined row was already released into staging.")
            approved = connection.execute(
                "SELECT * FROM plans WHERE status = 'approved' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if approved is None:
                raise ServiceError(409, "Approve a mapping plan before releasing a quarantined row.")
            corrected = _clean_source_record(record) if record is not None else (
                load(case["correction_json"]) if case["correction_json"] else load(case["record_json"])
            )
            if record is not None:
                connection.execute(
                    "UPDATE quarantine_cases SET correction_json = ?, updated_at = ? WHERE id = ?",
                    (dump(corrected), utc_now(), case_id),
                )
            evaluation = evaluate_plan(load(approved["body"])["mappings"], [corrected])
            if evaluation.rejected_count:
                detail = self._case_detail(connection, connection.execute(
                    "SELECT * FROM quarantine_cases WHERE id = ?", (case_id,)
                ).fetchone())
                detail["release_errors"] = [error.evidence() for outcome in evaluation.outcomes for error in outcome.errors]
                detail["status"] = "open"
                return detail
            outcome = evaluation.inserted[0] if evaluation.inserted else evaluation.outcomes[0]
            customer_id = outcome.output["customer_id"]
            existing = {
                row["customer_id"]
                for row in connection.execute("SELECT customer_id FROM target_customers").fetchall()
            }
            if customer_id in existing:
                connection.execute(
                    "UPDATE quarantine_cases SET status = 'skipped_duplicate', updated_at = ? WHERE id = ?",
                    (utc_now(), case_id),
                )
                self.db.add_history(
                    connection,
                    "quarantine_skipped_duplicate",
                    {"case_id": case_id, "customer_id": customer_id},
                    plan_id=approved["id"],
                )
                return self._case_detail(connection, connection.execute(
                    "SELECT * FROM quarantine_cases WHERE id = ?", (case_id,)
                ).fetchone())
            applied = apply_against_target(evaluation, existing)
            run = self._store_run(connection, approved, applied, mode="execute", is_retry=False)
            loaded_at = utc_now()
            connection.execute(
                """
                INSERT INTO target_customers(customer_id, payload, load_run_id, loaded_at)
                VALUES (?, ?, ?, ?)
                """,
                (customer_id, dump(outcome.output), run["id"], loaded_at),
            )
            connection.execute(
                """
                UPDATE quarantine_cases
                SET status = 'released', released_run_id = ?, correction_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (run["id"], dump(corrected), utc_now(), case_id),
            )
            self.db.add_history(
                connection,
                "quarantine_released",
                {"case_id": case_id, "customer_id": customer_id, "run_id": run["id"]},
                plan_id=approved["id"],
                run_id=run["id"],
            )
            released = self._case_detail(connection, connection.execute(
                "SELECT * FROM quarantine_cases WHERE id = ?", (case_id,)
            ).fetchone())
            released["run"] = run
            return released

    def _case_detail(self, connection, case) -> dict:
        errors = connection.execute(
            "SELECT * FROM quarantine WHERE case_id = ? ORDER BY id",
            (case["id"],),
        ).fetchall()
        return {
            "id": case["id"],
            "run_id": case["run_id"],
            "source_index": case["source_index"],
            "source_key": case["source_key"],
            "record": load(case["record_json"]),
            "correction": load(case["correction_json"]) if case["correction_json"] else None,
            "status": case["status"],
            "released_run_id": case["released_run_id"],
            "errors": [
                {
                    "field": row["field_name"],
                    "value": row["source_value"],
                    "rule": row["rule"],
                    "message": row["message"],
                }
                for row in errors
            ],
        }

    def _require_plan(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            raise ServiceError(404, "Plan not found.")
        return row

    def _quarantine_rows(self, connection, run_id: str) -> list[dict]:
        rows = connection.execute(
            "SELECT * FROM quarantine WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [
            {
                "source_key": row["source_key"],
                "field": row["field_name"],
                "value": row["source_value"],
                "rule": row["rule"],
                "message": row["message"],
                "record": load(row["record_json"]),
            }
            for row in rows
        ]

    def _plan_summary(self, row) -> dict:
        body = load(row["body"])
        return {
            "id": row["id"],
            "version": row["version"],
            "parent_id": row["parent_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "approved_at": row["approved_at"],
            "mapping_count": len(body.get("mappings") or []),
            "open_questions": len(blocking_questions(body)),
        }

    def _plan_detail(self, row) -> dict:
        detail = self._plan_summary(row)
        detail["approval_note"] = row["approval_note"]
        detail["body"] = load(row["body"])
        return detail

    def _run_summary(self, row) -> dict:
        return {
            "id": row["id"],
            "plan_id": row["plan_id"],
            "mode": row["mode"],
            "status": row["status"],
            "is_retry": bool(row["is_retry"]),
            "created_at": row["created_at"],
            "rolled_back_at": row["rolled_back_at"],
            "source_count": row["source_count"],
            "transformed_count": row["transformed_count"],
            "accepted_count": row["accepted_count"],
            "rejected_count": row["rejected_count"],
            "duplicate_skipped_count": row["duplicate_skipped_count"],
            "fingerprint": row["fingerprint"],
        }

    def _run_detail(self, row, errors: list[dict]) -> dict:
        detail = self._run_summary(row)
        stored = load(row["detail"])
        detail["definitions"] = stored.get("definitions", COUNT_DEFINITIONS)
        detail["skipped"] = stored.get("skipped", [])
        detail["inserted_keys"] = stored.get("inserted_keys", [])
        detail["errors"] = errors
        return detail


def _clean_source_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise ServiceError(400, "A corrected record must be an object.")
    names = source_field_names()
    unknown = [key for key in record if key not in names]
    if unknown:
        raise ServiceError(400, "Unknown source fields: " + ", ".join(unknown) + ".")
    return {name: "" if record.get(name) is None else str(record.get(name)) for name in names}


def normalize_mappings(raw_mappings: list[dict]) -> list[dict]:
    if not isinstance(raw_mappings, list):
        raise ServiceError(400, "Mappings must be a list.")
    seen: set[str] = set()
    cleaned = []
    source_names = set(source_field_names())
    targets = target_field_map()
    names = transform_names()
    for raw in raw_mappings:
        if not isinstance(raw, dict):
            raise ServiceError(400, "Each mapping must be an object.")
        target = raw.get("target_field")
        if target not in targets:
            raise ServiceError(400, f"Unknown target field '{target}'.")
        if target in seen:
            raise ServiceError(400, f"Target field '{target}' is mapped twice.")
        seen.add(target)
        transform = raw.get("transform")
        if transform not in names:
            raise ServiceError(400, f"Transform '{transform}' is not in the catalog.")
        source_fields = list(raw.get("source_fields") or [])
        for field in source_fields:
            if field not in source_names:
                raise ServiceError(400, f"Unknown source field '{field}'.")
        params = {}
        for key, value in dict(raw.get("params") or {}).items():
            if key in ALLOWED_PARAMS:
                params[key] = value
        if transform in {"constant", "reject"}:
            source_fields = []
        elif transform == "concat" and len(source_fields) < 2:
            raise ServiceError(400, "Concatenate needs at least two source fields.")
        elif transform == "coalesce" and not source_fields:
            raise ServiceError(400, "Coalesce needs a source field.")
        elif transform not in {"concat", "coalesce"} and len(source_fields) != 1:
            raise ServiceError(400, f"{transform} expects one source field.")
        _validate_params(transform, params, targets[target])
        confidence = raw.get("confidence")
        if confidence is not None:
            try:
                confidence = float(confidence)
            except (TypeError, ValueError) as exc:
                raise ServiceError(400, "Confidence must be a number.") from exc
            if not 0 <= confidence <= 1:
                raise ServiceError(400, "Confidence must be between 0 and 1.")
        risk = raw.get("risk")
        cleaned.append(
            {
                "target_field": target,
                "source_fields": source_fields,
                "transform": transform,
                "params": params,
                "confidence": confidence,
                "rationale": str(raw.get("rationale") or "Edited by reviewer."),
                "risk": None if risk is None else str(risk),
            }
        )
    return cleaned


def _validate_params(transform: str, params: dict, spec: dict) -> None:
    if transform == "parse_date":
        fmt = params.get("format")
        directives = re.findall(r"%([A-Za-z])", fmt if isinstance(fmt, str) else "")
        if not directives or any(item not in DATE_DIRECTIVES for item in directives):
            raise ServiceError(400, "parse_date needs a strptime format such as %m/%d/%Y.")
        try:
            datetime.strptime(datetime.now().strftime(fmt), fmt)
        except ValueError as exc:
            raise ServiceError(400, "Date format is not usable.") from exc
    elif transform == "map_enum":
        mapping = params.get("map")
        if not isinstance(mapping, dict) or not mapping:
            raise ServiceError(400, "map_enum needs a code map.")
        allowed = set(spec.get("enum") or [])
        for key, value in mapping.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ServiceError(400, "Enum map keys and values must be text.")
            if allowed and value not in allowed:
                raise ServiceError(400, f"Mapped value '{value}' is outside the target enum.")
    elif transform == "constant":
        if not isinstance(params.get("value"), str) or not params["value"].strip():
            raise ServiceError(400, "Constant needs a non-empty text value.")
    elif transform == "reject":
        if params.get("message") is not None and not isinstance(params.get("message"), str):
            raise ServiceError(400, "Reject message must be text.")
    elif transform == "concat" and params.get("separator") is not None and not isinstance(params.get("separator"), str):
        raise ServiceError(400, "Separator must be text.")


def apply_answer(body: dict, question_id: str, option_id: str) -> None:
    question = next((item for item in body.get("questions") or [] if item["id"] == question_id), None)
    if question is None:
        raise ServiceError(404, "Question not found on this plan.")
    if option_id not in {option["id"] for option in question["options"]}:
        raise ServiceError(400, "That option is not part of the question.")
    question["answer"] = option_id
    if question_id == "q-region" and option_id == "use_constant":
        _replace_mapping(
            body,
            "region_code",
            transform="constant",
            params={"value": "UNASSIGNED"},
            source_fields=[],
            rationale="Reviewer accepted the UNASSIGNED constant for the missing region.",
        )
    elif question_id == "q-region" and option_id == "reject_field":
        _replace_mapping(
            body,
            "region_code",
            transform="reject",
            params={"message": "Reviewer rejected a constant region code."},
            source_fields=[],
            rationale="Reviewer chose to quarantine every row for region_code.",
        )
    elif question_id == "q-signup-format" and option_id == "confirm_mdy":
        mapping = _find_mapping(body, "signed_up_on")
        if mapping is None or mapping["transform"] != "parse_date":
            _replace_mapping(
                body,
                "signed_up_on",
                transform="parse_date",
                params={"format": "%m/%d/%Y"},
                source_fields=["SIGNUP_DT"],
                rationale="Reviewer confirmed MM/DD/YYYY.",
            )
        else:
            mapping["params"] = {"format": "%m/%d/%Y"}
            mapping["rationale"] = "Reviewer confirmed MM/DD/YYYY."
    elif question_id == "q-signup-format" and option_id == "reject_unconfirmed_dates":
        _replace_mapping(
            body,
            "signed_up_on",
            transform="reject",
            params={"message": "Reviewer did not confirm the source date format."},
            source_fields=[],
            rationale="Reviewer declined the date conversion.",
        )


def _replace_mapping(body: dict, target: str, transform: str, params: dict, source_fields: list[str], rationale: str) -> None:
    mappings = [item for item in body["mappings"] if item["target_field"] != target]
    mappings.append(
        {
            "target_field": target,
            "source_fields": source_fields,
            "transform": transform,
            "params": params,
            "confidence": 1.0,
            "rationale": rationale,
            "risk": None,
        }
    )
    order = [field["name"] for field in TARGET_SCHEMA["fields"]]
    body["mappings"] = sorted(mappings, key=lambda item: order.index(item["target_field"]))


def _find_mapping(body: dict, target: str) -> dict | None:
    return next((item for item in body["mappings"] if item["target_field"] == target), None)


def sync_question_answers(questions: list[dict], mappings: list[dict]) -> list[dict]:
    by_target = {mapping["target_field"]: mapping for mapping in mappings}
    synced = []
    for question in questions:
        updated = dict(question)
        mapping = by_target.get(question["target_field"])
        answer = updated.get("answer")
        if question["id"] == "q-region":
            if answer == "use_constant" and not _is_constant(mapping):
                updated["answer"] = None
            elif answer == "reject_field" and not _is_reject(mapping):
                updated["answer"] = None
        elif question["id"] == "q-signup-format":
            if answer == "confirm_mdy" and not _is_mdy(mapping):
                updated["answer"] = None
            elif answer == "reject_unconfirmed_dates" and not _is_reject(mapping):
                updated["answer"] = None
        synced.append(updated)
    return synced


def _is_constant(mapping: dict | None) -> bool:
    return bool(mapping and mapping["transform"] == "constant" and str(mapping.get("params", {}).get("value", "")).strip())


def _is_reject(mapping: dict | None) -> bool:
    return bool(mapping and mapping["transform"] == "reject")


def _is_mdy(mapping: dict | None) -> bool:
    return bool(mapping and mapping["transform"] == "parse_date" and mapping.get("params", {}).get("format") == "%m/%d/%Y")


def blocking_questions(body: dict) -> list[dict]:
    return [
        question
        for question in body.get("questions") or []
        if isinstance(question, dict) and question.get("blocking") and not question.get("answer")
    ]


def missing_required(mappings: list[dict]) -> list[str]:
    mapped = {mapping["target_field"] for mapping in mappings}
    return [field["name"] for field in TARGET_SCHEMA["fields"] if field["required"] and field["name"] not in mapped]


def _sum_payloads(payloads: list[dict]) -> dict[str, str]:
    credit = Decimal("0")
    points = 0
    for payload in payloads:
        credit += Decimal(payload["credit_limit"])
        points += int(payload["loyalty_points"] or 0)
    return {
        "accepted_rows": str(len(payloads)),
        "credit_limit_sum": format(credit, "f"),
        "loyalty_points_sum": str(points),
    }
