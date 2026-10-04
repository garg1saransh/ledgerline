"""Closed catalog of migration transforms. No arbitrary code is accepted."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.catalog import TRANSFORMS, transform_names

EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class TransformError(Exception):
    def __init__(self, message: str, *, rule: str, value=None, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.rule = rule
        self.value = value
        self.field = field


def is_blank(value) -> bool:
    return value is None or str(value).strip() == ""


def snapshot(value) -> str | None:
    if value is None:
        return None
    return str(value)


def _require_known(name: str) -> None:
    if name not in transform_names():
        raise TransformError(f"Transform '{name}' is not in the supported catalog.", rule="unknown_transform", value=name)


def apply_transform(name: str, record: dict, source_fields: list[str], params: dict, field_spec: dict):
    _require_known(name)
    params = params or {}
    if name == "constant":
        value = params.get("value")
        if is_blank(value):
            raise TransformError("Constant value is empty.", rule=name, value=value, field=field_spec["name"])
        return _validate(value, field_spec, name)
    if name == "reject":
        message = params.get("message") or "Field rejected by the migration plan."
        raise TransformError(str(message), rule=name, value=None, field=field_spec["name"])
    if name == "concat":
        return _apply_concat(record, source_fields, params, field_spec)
    if name == "coalesce":
        return _apply_coalesce(record, source_fields, field_spec)

    if len(source_fields) != 1:
        raise TransformError(
            "This transform expects exactly one source field.",
            rule=name,
            value=",".join(source_fields),
            field=field_spec["name"],
        )
    raw = record.get(source_fields[0])
    if is_blank(raw):
        if field_spec["required"]:
            raise TransformError(
                "Required source value is blank.",
                rule=name,
                value=snapshot(raw),
                field=field_spec["name"],
            )
        return None
    produced = TRANSFORM_FNS[name](raw, params, field_spec["name"])
    return _validate(produced, field_spec, name)


def _apply_concat(record: dict, source_fields: list[str], params: dict, field_spec: dict):
    if len(source_fields) < 2:
        raise TransformError("Concatenate needs at least two source fields.", rule="concat", field=field_spec["name"])
    separator = params.get("separator", " ")
    if not isinstance(separator, str):
        raise TransformError("Separator must be text.", rule="concat", field=field_spec["name"])
    parts = [str(record.get(field) or "").strip() for field in source_fields]
    joined = separator.join(part for part in parts if part).strip()
    if separator == " ":
        joined = " ".join(joined.split())
    if joined == "":
        if field_spec["required"]:
            raise TransformError("Concatenated value is blank.", rule="concat", value="", field=field_spec["name"])
        return None
    return _validate(joined, field_spec, "concat")


def _apply_coalesce(record: dict, source_fields: list[str], field_spec: dict):
    if not source_fields:
        raise TransformError("Coalesce needs a source field.", rule="coalesce", field=field_spec["name"])
    for field in source_fields:
        raw = record.get(field)
        if not is_blank(raw):
            return _validate(str(raw).strip(), field_spec, "coalesce")
    if field_spec["required"]:
        raise TransformError("Every coalesce input is blank.", rule="coalesce", field=field_spec["name"])
    return None


def _direct(raw, _params, field: str):
    return str(raw).strip()


def _trim(raw, _params, field: str):
    return str(raw).strip()


def _lower(raw, _params, field: str):
    return str(raw).strip().lower()


def _upper(raw, _params, field: str):
    return str(raw).strip().upper()


def _parse_date(raw, params, field: str):
    fmt = params.get("format")
    text = str(raw).strip()
    if not isinstance(fmt, str) or not fmt:
        raise TransformError("Date format is required.", rule="parse_date", value=text, field=field)
    try:
        parsed = datetime.strptime(text, fmt)
    except ValueError as exc:
        raise TransformError(
            f"Value does not match {fmt}.",
            rule="parse_date",
            value=text,
            field=field,
        ) from exc
    return parsed.date().isoformat()


def _parse_decimal(raw, _params, field: str):
    text = str(raw).strip().replace("$", "").replace(",", "").replace(" ", "")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise TransformError("Value is not a decimal number.", rule="parse_decimal", value=str(raw), field=field) from exc
    if not number.is_finite():
        raise TransformError("Value is not a finite decimal.", rule="parse_decimal", value=str(raw), field=field)
    quantized = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(quantized, "f")


def _parse_integer(raw, _params, field: str):
    text = str(raw).strip().replace(",", "")
    if not re.fullmatch(r"-?\d+", text):
        raise TransformError("Value is not a whole number.", rule="parse_integer", value=str(raw), field=field)
    return int(text)


def _map_enum(raw, params, field: str):
    mapping = params.get("map")
    text = str(raw).strip()
    if not isinstance(mapping, dict) or not mapping:
        raise TransformError("Enum map is required.", rule="map_enum", value=text, field=field)
    if text not in mapping:
        known = ", ".join(sorted(str(key) for key in mapping))
        raise TransformError(
            f"Code '{text}' is not in the map ({known}).",
            rule="map_enum",
            value=text,
            field=field,
        )
    return mapping[text]


def _email(raw, _params, field: str):
    text = str(raw).strip().lower()
    if not EMAIL_RE.fullmatch(text):
        raise TransformError("Value is not an email address.", rule="email_normalize", value=text, field=field)
    return text


def _phone(raw, _params, field: str):
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise TransformError(
        "Value is not a 10-digit US phone number.",
        rule="phone_normalize",
        value=str(raw),
        field=field,
    )


def _validate(value, field_spec: dict, rule: str):
    if value is None:
        if field_spec["required"]:
            raise TransformError("Required value is missing.", rule=rule, value=None, field=field_spec["name"])
        return None
    kind = field_spec["type"]
    name = field_spec["name"]
    if kind == "string":
        if not isinstance(value, str) or (field_spec["required"] and value.strip() == ""):
            raise TransformError("Expected text.", rule=rule, value=snapshot(value), field=name)
        return value
    if kind == "date":
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise TransformError("Expected an ISO date.", rule=rule, value=snapshot(value), field=name)
        return value
    if kind == "decimal":
        if not isinstance(value, str) or not re.fullmatch(r"-?\d+\.\d{2}", value):
            raise TransformError("Expected a two-decimal number.", rule=rule, value=snapshot(value), field=name)
        return value
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise TransformError("Expected an integer.", rule=rule, value=snapshot(value), field=name)
        return value
    if kind == "enum":
        allowed = field_spec.get("enum") or []
        if value not in allowed:
            raise TransformError(
                f"Value '{value}' is outside the target enum.",
                rule=rule,
                value=snapshot(value),
                field=name,
            )
        return value
    raise TransformError(f"Unsupported target type '{kind}'.", rule=rule, value=snapshot(value), field=name)


TRANSFORM_FNS = {
    "direct": _direct,
    "trim": _trim,
    "lower": _lower,
    "upper": _upper,
    "parse_date": _parse_date,
    "parse_decimal": _parse_decimal,
    "parse_integer": _parse_integer,
    "map_enum": _map_enum,
    "email_normalize": _email,
    "phone_normalize": _phone,
}


def catalog_is_implemented() -> bool:
    implemented = set(TRANSFORM_FNS) | {"concat", "coalesce", "constant", "reject"}
    return implemented == {item["name"] for item in TRANSFORMS}
