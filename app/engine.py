"""Deterministic plan evaluation. The same plan and sample always yield the same outcomes."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from app.catalog import SOURCE_SCHEMA, TARGET_SCHEMA, bounded_sample, target_field_map
from app.transforms import TransformError, apply_transform, is_blank, snapshot

COUNT_DEFINITIONS = {
    "source": "Rows read from the customer extract.",
    "transformed": "Rows that entered the mapping pipeline.",
    "accepted": "Rows that passed validation. On execute, rows newly inserted into staging.",
    "rejected": "Rows quarantined with field-level evidence.",
    "duplicate_skipped": "Valid rows skipped on execute because that target key is already loaded.",
}


@dataclass
class FieldError:
    source_key: str
    field: str
    value: str | None
    rule: str
    message: str
    record: dict

    def evidence(self) -> dict:
        return {
            "source_key": self.source_key,
            "field": self.field,
            "value": self.value,
            "rule": self.rule,
            "message": self.message,
            "record": self.record,
        }


@dataclass
class RecordOutcome:
    index: int
    source_key: str
    status: str
    entered_pipeline: bool
    output: dict | None = None
    errors: list[FieldError] = field(default_factory=list)
    record: dict = field(default_factory=dict)


@dataclass
class Evaluation:
    outcomes: list[RecordOutcome]
    source_count: int
    transformed_count: int
    accepted_count: int
    rejected_count: int
    duplicate_skipped_count: int
    fingerprint: str
    inserted: list[RecordOutcome] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def identities_hold(self) -> bool:
        return self.source_count == (
            self.accepted_count + self.rejected_count + self.duplicate_skipped_count
        )


def source_key(record: dict) -> str:
    raw = record.get(SOURCE_SCHEMA["primary_key"][0])
    return "" if is_blank(raw) else str(raw).strip()


def evaluate_plan(mappings: list[dict], records: list[dict] | None = None) -> Evaluation:
    sample = bounded_sample(records)
    specs = target_field_map()
    ordered = _ordered_mappings(mappings, specs)
    outcomes: list[RecordOutcome] = []
    seen_source: set[str] = set()

    for index, record in enumerate(sample):
        key = source_key(record)
        if key == "":
            outcomes.append(
                RecordOutcome(
                    index=index,
                    source_key="",
                    status="rejected",
                    entered_pipeline=False,
                    errors=[
                        FieldError(
                            source_key="",
                            field=SOURCE_SCHEMA["primary_key"][0],
                            value=snapshot(record.get(SOURCE_SCHEMA["primary_key"][0])),
                            rule="missing_source_key",
                            message="Source row has no primary key.",
                            record=dict(record),
                        )
                    ],
                    record=dict(record),
                )
            )
            continue
        if key in seen_source:
            outcomes.append(
                RecordOutcome(
                    index=index,
                    source_key=key,
                    status="rejected",
                    entered_pipeline=False,
                    errors=[
                        FieldError(
                            source_key=key,
                            field=SOURCE_SCHEMA["primary_key"][0],
                            value=key,
                            rule="duplicate_source_key",
                            message="Source primary key already appeared in this sample.",
                            record=dict(record),
                        )
                    ],
                    record=dict(record),
                )
            )
            continue
        seen_source.add(key)
        outcomes.append(_transform_row(index, key, record, ordered, specs))

    _reject_duplicate_targets(outcomes)
    return _package(outcomes, existing_ids=None)


def apply_against_target(evaluation: Evaluation, existing_ids: set[str]) -> Evaluation:
    """Split valid rows into inserts and already-loaded keys. Does not mutate the dry-run evaluation."""
    inserted: list[RecordOutcome] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for outcome in evaluation.outcomes:
        if outcome.status != "accepted" or outcome.output is None:
            continue
        customer_id = outcome.output["customer_id"]
        if customer_id in existing_ids or customer_id in seen:
            skipped.append(
                {
                    "source_key": outcome.source_key,
                    "customer_id": customer_id,
                    "reason": "Target key is already loaded. Retry will not insert a duplicate.",
                }
            )
        else:
            inserted.append(outcome)
            seen.add(customer_id)
    return _package(evaluation.outcomes, existing_ids=existing_ids, inserted=inserted, skipped=skipped)


def _ordered_mappings(mappings: list[dict], specs: dict[str, dict]) -> list[dict]:
    by_target = {mapping["target_field"]: mapping for mapping in mappings}
    return [by_target[field["name"]] for field in TARGET_SCHEMA["fields"] if field["name"] in by_target and field["name"] in specs]


def _transform_row(index: int, key: str, record: dict, mappings: list[dict], specs: dict[str, dict]) -> RecordOutcome:
    errors: list[FieldError] = []
    output: dict = {}
    mapped_fields = {mapping["target_field"] for mapping in mappings}
    for mapping in mappings:
        spec = specs[mapping["target_field"]]
        try:
            value = apply_transform(
                mapping["transform"],
                record,
                list(mapping.get("source_fields") or []),
                dict(mapping.get("params") or {}),
                spec,
            )
        except TransformError as exc:
            errors.append(
                FieldError(
                    source_key=key,
                    field=exc.field or spec["name"],
                    value=snapshot(exc.value) if exc.value is not None else _raw_snapshot(record, mapping),
                    rule=exc.rule,
                    message=exc.message,
                    record=dict(record),
                )
            )
            continue
        output[spec["name"]] = value

    if not errors:
        for spec in TARGET_SCHEMA["fields"]:
            if spec["name"] in output:
                continue
            if spec["required"] and spec["name"] not in mapped_fields:
                errors.append(
                    FieldError(
                        source_key=key,
                        field=spec["name"],
                        value=None,
                        rule="missing_mapping",
                        message="Required target field has no mapping.",
                        record=dict(record),
                    )
                )
            elif not spec["required"]:
                output[spec["name"]] = None
        if not errors:
            ordered_output = {spec["name"]: output.get(spec["name"]) for spec in TARGET_SCHEMA["fields"]}
            return RecordOutcome(
                index=index,
                source_key=key,
                status="accepted",
                entered_pipeline=True,
                output=ordered_output,
                record=dict(record),
            )
    return RecordOutcome(
        index=index,
        source_key=key,
        status="rejected",
        entered_pipeline=True,
        errors=errors,
        record=dict(record),
    )


def _raw_snapshot(record: dict, mapping: dict) -> str | None:
    fields = mapping.get("source_fields") or []
    if not fields:
        return None
    if len(fields) == 1:
        return snapshot(record.get(fields[0]))
    return " ".join(f"{name}={snapshot(record.get(name))}" for name in fields)


def _reject_duplicate_targets(outcomes: list[RecordOutcome]) -> None:
    seen: set[str] = set()
    for outcome in outcomes:
        if outcome.status != "accepted" or not outcome.output:
            continue
        customer_id = str(outcome.output.get("customer_id") or "")
        if customer_id in seen:
            outcome.status = "rejected"
            outcome.output = None
            outcome.errors.append(
                FieldError(
                    source_key=outcome.source_key,
                    field="customer_id",
                    value=customer_id,
                    rule="duplicate_target_key",
                    message="Another source row in this sample already produced this target key.",
                    record=dict(outcome.record),
                )
            )
        else:
            seen.add(customer_id)


def _package(
    outcomes: list[RecordOutcome],
    existing_ids: set[str] | None,
    inserted: list[RecordOutcome] | None = None,
    skipped: list[dict] | None = None,
) -> Evaluation:
    valid = [outcome for outcome in outcomes if outcome.status == "accepted"]
    rejected = [outcome for outcome in outcomes if outcome.status == "rejected"]
    if existing_ids is None:
        inserted_rows = valid
        skipped_rows: list[dict] = []
        accepted_count = len(valid)
    else:
        inserted_rows = inserted or []
        skipped_rows = skipped or []
        accepted_count = len(inserted_rows)
    evaluation = Evaluation(
        outcomes=outcomes,
        source_count=len(outcomes),
        transformed_count=sum(1 for outcome in outcomes if outcome.entered_pipeline),
        accepted_count=accepted_count,
        rejected_count=len(rejected),
        duplicate_skipped_count=len(skipped_rows),
        fingerprint=fingerprint(outcomes),
        inserted=inserted_rows,
        skipped=skipped_rows,
    )
    if not evaluation.identities_hold():
        raise RuntimeError("Migration counts do not reconcile to the source row count.")
    return evaluation


def fingerprint(outcomes: list[RecordOutcome]) -> str:
    payload = []
    for outcome in outcomes:
        payload.append(
            {
                "index": outcome.index,
                "source_key": outcome.source_key,
                "status": outcome.status,
                "output": outcome.output,
                "errors": [
                    {
                        "field": error.field,
                        "rule": error.rule,
                        "message": error.message,
                        "value": error.value,
                    }
                    for error in outcome.errors
                ],
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def preview_mapping(mapping: dict, records: list[dict] | None = None) -> dict:
    """Run one mapping across the sample. Used by the agent validation tool."""
    sample = bounded_sample(records)
    spec = target_field_map()[mapping["target_field"]]
    failures = []
    attempted = 0
    for record in sample:
        if source_key(record) == "":
            continue
        attempted += 1
        try:
            apply_transform(
                mapping["transform"],
                record,
                list(mapping.get("source_fields") or []),
                dict(mapping.get("params") or {}),
                spec,
            )
        except TransformError as exc:
            failures.append(
                {
                    "source_key": source_key(record),
                    "message": exc.message,
                    "value": snapshot(exc.value) if exc.value is not None else snapshot(record.get((mapping.get("source_fields") or [""])[0])),
                }
            )
    return {
        "target_field": mapping["target_field"],
        "transform": mapping["transform"],
        "sampled": attempted,
        "failures": len(failures),
        "examples": failures[:3],
    }


def raw_source_totals(records: list[dict] | None = None) -> dict[str, str]:
    """Totals taken from the source file itself, before the migration plan filters rows."""
    from decimal import Decimal

    from app.transforms import TransformError, apply_transform

    sample = bounded_sample(records)
    credit = Decimal("0")
    points = 0
    unparsed_credit = 0
    unparsed_points = 0
    blank_credit = 0
    blank_points = 0
    decimal_spec = {"name": "credit_limit", "type": "decimal", "required": True}
    integer_spec = {"name": "loyalty_points", "type": "integer", "required": True}
    for record in sample:
        credit_raw = record.get("CREDIT_LIM")
        if is_blank(credit_raw):
            blank_credit += 1
        else:
            try:
                credit += Decimal(
                    apply_transform("parse_decimal", record, ["CREDIT_LIM"], {}, decimal_spec)
                )
            except TransformError:
                unparsed_credit += 1
        points_raw = record.get("LOYALTY_PTS")
        if is_blank(points_raw):
            blank_points += 1
        else:
            try:
                points += int(apply_transform("parse_integer", record, ["LOYALTY_PTS"], {}, integer_spec))
            except TransformError:
                unparsed_points += 1
    return {
        "rows": str(len(sample)),
        "credit_limit_sum": format(credit, "f"),
        "credit_unparsed": str(unparsed_credit),
        "credit_blank": str(blank_credit),
        "loyalty_points_sum": str(points),
        "loyalty_unparsed": str(unparsed_points),
        "loyalty_blank": str(blank_points),
    }


def totals_for(outcomes: list[RecordOutcome]) -> dict[str, str]:
    from decimal import Decimal

    credit = Decimal("0")
    points = 0

    for outcome in outcomes:
        if outcome.status != "accepted" or not outcome.output:
            continue
        credit += Decimal(outcome.output["credit_limit"])
        points += int(outcome.output["loyalty_points"] or 0)
    return {
        "accepted_rows": str(sum(1 for outcome in outcomes if outcome.status == "accepted")),
        "credit_limit_sum": format(credit, "f"),
        "loyalty_points_sum": str(points),
    }
