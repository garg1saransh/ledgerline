"""Tool-using mapping agent.

The planner may only call the inspection and validation tools in this module.
It proposes a plan. It does not execute a load, and it cannot invent transforms.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.catalog import (
    MAX_SAMPLE_SIZE,
    SOURCE_SCHEMA,
    TARGET_SCHEMA,
    TRANSFORMS,
    bounded_sample,
    source_field_names,
    target_field_map,
    transform_names,
)
from app.engine import preview_mapping

ALLOWED_TOOLS = {
    "inspect_source_schema",
    "inspect_target_schema",
    "inspect_sample_records",
    "list_supported_transforms",
    "profile_field",
    "check_type_compatibility",
    "validate_proposed_mapping",
}

SYNONYMS = {
    "customer_id": {"custid", "customerid", "customerkey"},
    "email": {"email", "emailaddr", "emailaddress"},
    "phone_e164": {"phone", "phonenumber", "mobile", "phonee164"},
    "signed_up_on": {"signupdt", "signupdate", "signedupon"},
    "status": {"status", "statuscd", "statuscode"},
    "credit_limit": {"creditlim", "creditlimit"},
    "loyalty_points": {"loyaltypts", "loyaltypoints"},
    "full_name": {"fullname", "customername"},
    "region_code": {"region", "regioncode", "regioncd"},
}


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def propose_plan() -> dict:
    tools = Toolbelt()
    source = tools.inspect_source_schema()
    target = tools.inspect_target_schema()
    sample = tools.inspect_sample_records()
    transforms = tools.list_supported_transforms()
    profiles = {field: tools.profile_field(field) for field in source_field_names()}

    claimed_sources: set[str] = set()
    mappings: list[dict] = []
    full_name = _propose_full_name(source)
    if full_name:
        mappings.append(full_name)
        claimed_sources.update(full_name["source_fields"])

    for spec in target["fields"]:
        if spec["name"] in {"full_name", "region_code"}:
            continue
        choice = _best_source(spec["name"], source["fields"], claimed_sources)
        if choice is None:
            continue
        source_field, confidence = choice
        claimed_sources.add(source_field["name"])
        transform, params = _choose_transform(spec, profiles[source_field["name"]])
        compatibility = tools.check_type_compatibility(source_field["type"], spec["type"], transform)
        mapping = {
            "target_field": spec["name"],
            "source_fields": [source_field["name"]],
            "transform": transform,
            "params": params,
            "confidence": confidence,
            "rationale": _rationale(source_field, spec, confidence, compatibility),
            "risk": _mapping_risk(spec, transform),
        }
        mappings.append(mapping)

    mappings.append(_propose_region())
    _validate_each(tools, mappings)

    unmapped_source = [field["name"] for field in source["fields"] if field["name"] not in claimed_sources]
    missing_target = ["region_code"]
    incompatibilities = _incompatibilities(mappings, source, target, tools)
    questions = _questions(profiles)
    risks = _risks(sample, mappings, unmapped_source)
    open_questions = sum(1 for question in questions if not question["answer"])
    summary = (
        f"Proposed {len(mappings)} mappings from {source['name']} to {target['name']}. "
        f"{len(missing_target)} required target field is missing on the source. "
        f"{len(unmapped_source)} source field has no target. "
        f"{open_questions} clarification questions need an answer before approval."
    )
    assert_plan_uses_catalog(
        {
            "mappings": mappings,
            "tool_trace": tools.trace,
        }
    )
    return {
        "dataset": source["name"],
        "target": target["name"],
        "sample_limit": MAX_SAMPLE_SIZE,
        "sample_count": sample["count"],
        "mappings": _sorted(mappings),
        "risks": risks,
        "questions": questions,
        "incompatibilities": incompatibilities,
        "missing_target_fields": missing_target,
        "unmapped_source_fields": unmapped_source,
        "tool_trace": tools.trace,
        "agent_summary": summary,
        "transforms_available": [item["name"] for item in transforms],
    }


class Toolbelt:
    def __init__(self):
        self.trace: list[dict] = []

    def inspect_source_schema(self) -> dict:
        return self._call("inspect_source_schema", {}, lambda: SOURCE_SCHEMA)

    def inspect_target_schema(self) -> dict:
        return self._call("inspect_target_schema", {}, lambda: TARGET_SCHEMA)

    def inspect_sample_records(self) -> dict:
        def run():
            records = bounded_sample()
            keys = ["" if _blank(row.get("CUST_ID")) else str(row.get("CUST_ID")).strip() for row in records]
            duplicates = sorted({key for key in keys if key and keys.count(key) > 1})
            return {
                "count": len(records),
                "limit": MAX_SAMPLE_SIZE,
                "duplicate_keys": duplicates,
                "blank_keys": sum(1 for key in keys if key == ""),
                "records": records,
            }

        return self._call("inspect_sample_records", {"limit": MAX_SAMPLE_SIZE}, run)

    def list_supported_transforms(self) -> list[dict]:
        return self._call("list_supported_transforms", {}, lambda: list(TRANSFORMS))

    def profile_field(self, field_name: str) -> dict:
        def run():
            records = bounded_sample()
            values = [row.get(field_name) for row in records]
            present = [str(value).strip() for value in values if not _blank(value)]
            distinct = sorted(set(present))
            return {
                "field": field_name,
                "rows": len(records),
                "blanks": sum(1 for value in values if _blank(value)),
                "distinct_count": len(distinct),
                "examples": distinct[:8],
                "date_format": _detect_date_format(present),
                "patterns": _patterns(present),
            }

        return self._call("profile_field", {"field": field_name}, run)

    def check_type_compatibility(self, source_type: str, target_type: str, transform: str) -> dict:
        def run():
            if transform not in transform_names():
                return {
                    "compatible": False,
                    "reason": "Transform is not in the catalog.",
                    "suggested_transform": None,
                }
            suggested = _suggested_transform(source_type, target_type)
            compatible = transform == suggested or transform in {
                "constant",
                "reject",
                "concat",
                "coalesce",
                "email_normalize",
                "phone_normalize",
                "trim",
                "direct",
                "lower",
                "upper",
            }
            if source_type != target_type and transform in {"direct", "trim", "lower", "upper"}:
                compatible = False
            reason = (
                f"{source_type} can load into {target_type} with {transform}."
                if compatible
                else f"{source_type} cannot load into {target_type} with {transform}. Use {suggested}."
            )
            return {"compatible": compatible, "reason": reason, "suggested_transform": suggested}

        return self._call(
            "check_type_compatibility",
            {"source_type": source_type, "target_type": target_type, "transform": transform},
            run,
        )

    def validate_proposed_mapping(self, mapping: dict) -> dict:
        return self._call(
            "validate_proposed_mapping",
            {"target_field": mapping["target_field"], "transform": mapping["transform"]},
            lambda: preview_mapping(mapping),
        )

    def dispatch(self, name: str, arguments: dict):
        arguments = arguments or {}
        if name == "inspect_source_schema":
            return self.inspect_source_schema()
        if name == "inspect_target_schema":
            return self.inspect_target_schema()
        if name == "inspect_sample_records":
            return self.inspect_sample_records()
        if name == "list_supported_transforms":
            return self.list_supported_transforms()
        if name == "profile_field":
            return self.profile_field(str(arguments.get("field") or ""))
        if name == "check_type_compatibility":
            return self.check_type_compatibility(
                str(arguments.get("source_type") or ""),
                str(arguments.get("target_type") or ""),
                str(arguments.get("transform") or ""),
            )
        if name == "validate_proposed_mapping":
            mapping = arguments.get("mapping") if isinstance(arguments.get("mapping"), dict) else arguments
            return self.validate_proposed_mapping(
                {
                    "target_field": mapping.get("target_field"),
                    "source_fields": list(mapping.get("source_fields") or []),
                    "transform": mapping.get("transform"),
                    "params": dict(mapping.get("params") or {}),
                }
            )
        raise RuntimeError(f"Agent attempted to call '{name}', which is not an inspection tool.")

    def _call(self, name: str, arguments: dict, fn):
        if name not in ALLOWED_TOOLS:
            raise RuntimeError(f"Agent attempted to call '{name}', which is not an inspection tool.")
        result = fn()
        self.trace.append({"tool": name, "arguments": arguments, "summary": _summarize(name, result)})
        return result


def _propose_full_name(source: dict) -> dict | None:
    first = _name_kind(source["fields"], "first")
    last = _name_kind(source["fields"], "last")
    if not first or not last:
        return None
    return {
        "target_field": "full_name",
        "source_fields": [first, last],
        "transform": "concat",
        "params": {"separator": " "},
        "confidence": 0.94,
        "rationale": f"Target full_name has no single source column. {first} and {last} concatenate with a space.",
        "risk": "Blank given and family names quarantine the row. Middle names are not in the export.",
    }


def _propose_region() -> dict:
    return {
        "target_field": "region_code",
        "source_fields": [],
        "transform": "constant",
        "params": {"value": "UNASSIGNED"},
        "confidence": 0.55,
        "rationale": "No source column scores as region_code. A constant is only a proposal until the reviewer answers.",
        "risk": "A constant hides the fact that the legacy export has no region.",
    }


def _best_source(target_name: str, source_fields: list[dict], claimed: set[str]):
    ranked = []
    for field in source_fields:
        if field["name"] in claimed:
            continue
        ranked.append((field, _score(field["name"], target_name)))
    ranked.sort(key=lambda item: (-item[1], item[0]["name"]))
    if not ranked or ranked[0][1] < 0.72:
        return None
    return ranked[0]


def _score(source_name: str, target_name: str) -> float:
    source_norm = norm(source_name)
    if source_norm in SYNONYMS.get(target_name, set()):
        return 0.97
    return round(SequenceMatcher(None, source_norm, norm(target_name)).ratio(), 2)


def _choose_transform(spec: dict, profile: dict) -> tuple[str, dict]:
    name = spec["name"]
    if name == "email":
        return "email_normalize", {}
    if name == "phone_e164":
        return "phone_normalize", {}
    if name == "customer_id":
        return "trim", {}
    if spec["type"] == "date":
        return "parse_date", {"format": profile.get("date_format") or "%m/%d/%Y"}
    if spec["type"] == "decimal":
        return "parse_decimal", {}
    if spec["type"] == "integer":
        return "parse_integer", {}
    if spec["type"] == "enum":
        return "map_enum", {"map": _enum_from_description(spec)}
    return "trim", {}


def _enum_from_description(spec: dict) -> dict:
    allowed = set(spec.get("enum") or [])
    found = {}
    description = ""
    for field in SOURCE_SCHEMA["fields"]:
        if field["name"] == "STATUS_CD":
            description = field.get("description") or ""
    for code, word in re.findall(r"\b([A-Za-z])\s+(active|inactive|suspended)\b", description, re.I):
        lowered = word.lower()
        if lowered in allowed:
            found[code.upper()] = lowered
    if found:
        return found
    return {"A": "active", "I": "inactive", "S": "suspended"}


def _validate_each(tools: Toolbelt, mappings: list[dict]) -> None:
    for mapping in mappings:
        preview = tools.validate_proposed_mapping(mapping)
        if preview["failures"]:
            mapping["risk"] = (
                f"{preview['failures']} of {preview['sampled']} sample values fail {mapping['transform']} "
                "and will quarantine."
            )


def _incompatibilities(mappings: list[dict], source: dict, target: dict, tools: Toolbelt) -> list[dict]:
    by_source = {field["name"]: field for field in source["fields"]}
    by_target = {field["name"]: field for field in target["fields"]}
    found = []
    for mapping in mappings:
        if mapping["transform"] in {"constant", "reject", "concat"} or not mapping["source_fields"]:
            continue
        source_name = mapping["source_fields"][0]
        source_field = by_source[source_name]
        target_field = by_target[mapping["target_field"]]
        if source_field["type"] == target_field["type"]:
            continue
        direct = tools.check_type_compatibility(source_field["type"], target_field["type"], "direct")
        found.append(
            {
                "source_field": source_name,
                "target_field": target_field["name"],
                "source_type": source_field["type"],
                "target_type": target_field["type"],
                "issue": direct["reason"],
                "suggested_transform": mapping["transform"],
            }
        )
    return found


def _questions(profiles: dict) -> list[dict]:
    date_format = profiles.get("SIGNUP_DT", {}).get("date_format") or "%m/%d/%Y"
    return [
        {
            "id": "q-region",
            "blocking": True,
            "target_field": "region_code",
            "prompt": (
                "region_code is required on customer_master and has no column on the legacy export. "
                "Stamp UNASSIGNED, or quarantine every row for this field."
            ),
            "options": [
                {"id": "use_constant", "label": "Stamp UNASSIGNED on every accepted row"},
                {"id": "reject_field", "label": "Quarantine every row for region_code"},
            ],
            "answer": None,
        },
        {
            "id": "q-signup-format",
            "blocking": True,
            "target_field": "signed_up_on",
            "prompt": (
                f"Profile of SIGNUP_DT suggests {date_format}. Confirm that format. "
                "Values that do not match are quarantined with the raw text kept as evidence."
            ),
            "options": [
                {"id": "confirm_mdy", "label": "Confirm MM/DD/YYYY"},
                {"id": "reject_unconfirmed_dates", "label": "Do not convert dates"},
            ],
            "answer": None,
        },
    ]


def _risks(sample: dict, mappings: list[dict], unmapped_source: list[str]) -> list[dict]:
    risks = [
        {
            "severity": "high",
            "code": "missing_region",
            "message": "Required target field region_code is missing from the source. Approval waits on that question.",
        }
    ]
    if sample["duplicate_keys"]:
        risks.append(
            {
                "severity": "medium",
                "code": "duplicate_source_keys",
                "message": "Duplicate source keys stay out of the pipeline: " + ", ".join(sample["duplicate_keys"]) + ".",
            }
        )
    if sample["blank_keys"]:
        risks.append(
            {
                "severity": "medium",
                "code": "blank_source_keys",
                "message": f"{sample['blank_keys']} sample row has no CUST_ID and will quarantine.",
            }
        )
    for name in unmapped_source:
        risks.append(
            {
                "severity": "low",
                "code": "dropped_source_field",
                "message": f"{name} has no target column and will be dropped. The raw row is still kept on quarantine records.",
            }
        )
    for mapping in mappings:
        if mapping.get("risk") and "sample values fail" in mapping["risk"]:
            risks.append(
                {
                    "severity": "medium",
                    "code": "sample_failures",
                    "message": f"{mapping['target_field']}: {mapping['risk']}",
                }
            )
    return risks


def _rationale(source_field: dict, spec: dict, confidence: float, compatibility: dict) -> str:
    return f"{source_field['name']} matches {spec['name']} at {confidence:.2f}. {compatibility['reason']}"


def _mapping_risk(spec: dict, transform: str) -> str | None:
    if transform == "phone_normalize":
        return "Unparseable phone text quarantines the row. Blank phones stay null because the target field is optional."
    if transform == "map_enum":
        return "Codes outside the catalog map quarantine the row."
    if spec["type"] == "date":
        return "Dates that do not match the confirmed format quarantine the row."
    return None


def _name_kind(fields: list[dict], kind: str) -> str | None:
    for field in fields:
        if norm(field["name"]).startswith(kind):
            return field["name"]
    return None


def _detect_date_format(values: list[str]) -> str | None:
    us = sum(1 for value in values if re.fullmatch(r"\d{2}/\d{2}/\d{4}", value))
    iso = sum(1 for value in values if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
    if us == 0 and iso == 0:
        return None
    if us >= iso:
        return "%m/%d/%Y"
    return "%Y-%m-%d"


def _patterns(values: list[str]) -> list[str]:
    patterns = []
    if any(re.fullmatch(r"\d{2}/\d{2}/\d{4}", value) for value in values):
        patterns.append("us_date")
    if any(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) for value in values):
        patterns.append("iso_date")
    if any("@" in value for value in values):
        patterns.append("email")
    if any(re.fullmatch(r"-?\d+", value.replace(",", "")) for value in values):
        patterns.append("integer_like")
    return patterns


def _suggested_transform(source_type: str, target_type: str) -> str:
    if source_type == target_type:
        return "trim" if target_type == "string" else "direct"
    bridges = {
        ("string", "date"): "parse_date",
        ("string", "decimal"): "parse_decimal",
        ("string", "integer"): "parse_integer",
        ("string", "enum"): "map_enum",
    }
    return bridges.get((source_type, target_type), "direct")


def _sorted(mappings: list[dict]) -> list[dict]:
    order = [field["name"] for field in TARGET_SCHEMA["fields"]]
    return sorted(mappings, key=lambda mapping: order.index(mapping["target_field"]))


def _summarize(name: str, result) -> str:
    if name == "inspect_source_schema":
        return f"{result['name']}: {len(result['fields'])} fields"
    if name == "inspect_target_schema":
        return f"{result['name']}: {len(result['fields'])} fields"
    if name == "inspect_sample_records":
        return (
            f"{result['count']} rows, limit {result['limit']}, "
            f"duplicate keys {result['duplicate_keys'] or 'none'}, blank keys {result['blank_keys']}"
        )
    if name == "list_supported_transforms":
        return f"{len(result)} catalog transforms"
    if name == "profile_field":
        return (
            f"{result['field']}: {result['blanks']} blank, {result['distinct_count']} distinct, "
            f"date format {result['date_format'] or 'n/a'}"
        )
    if name == "check_type_compatibility":
        return result["reason"]
    if name == "validate_proposed_mapping":
        return (
            f"{result['target_field']} via {result['transform']}: "
            f"{result['failures']} failures in {result['sampled']}"
        )
    return name


def _blank(value) -> bool:
    return value is None or str(value).strip() == ""


def assert_plan_uses_catalog(plan: dict) -> None:
    allowed = transform_names()
    known_targets = set(target_field_map())
    for mapping in plan["mappings"]:
        if mapping["transform"] not in allowed:
            raise RuntimeError(f"Agent proposed unknown transform {mapping['transform']}")
        if mapping["target_field"] not in known_targets:
            raise RuntimeError("Agent mapped an unknown target field")
    for event in plan["tool_trace"]:
        if event["tool"] not in ALLOWED_TOOLS:
            raise RuntimeError(f"Agent called {event['tool']}")
