"""Provided inputs for the migration workbench.

Schemas, the sample, and the transform catalog are files under data/.
The sample is capped by MAX_SAMPLE_SIZE. The app does not connect to a
production database or accept arbitrary transformation code.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _load(name: str):
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


MANIFEST = _load("manifest.json")
MAX_SAMPLE_SIZE = int(MANIFEST["max_sample_size"])
SOURCE_SCHEMA = _load("source_schema.json")
TARGET_SCHEMA = _load("target_schema.json")
TRANSFORMS = _load("transforms.json")
SAMPLE_RECORDS = _load("sample.json")


class SampleLimitExceeded(ValueError):
    def __init__(self, count: int):
        super().__init__(f"Sample has {count} rows. The maximum is {MAX_SAMPLE_SIZE}.")
        self.count = count


def bounded_sample(records: list[dict] | None = None) -> list[dict]:
    rows = SAMPLE_RECORDS if records is None else records
    if len(rows) > MAX_SAMPLE_SIZE:
        raise SampleLimitExceeded(len(rows))
    return [dict(row) for row in rows]


def transform_names() -> set[str]:
    return {item["name"] for item in TRANSFORMS}


def source_field_names() -> list[str]:
    return [field["name"] for field in SOURCE_SCHEMA["fields"]]


def target_field_map() -> dict[str, dict]:
    return {field["name"]: field for field in TARGET_SCHEMA["fields"]}
