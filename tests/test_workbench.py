import json
from decimal import Decimal

from fastapi.testclient import TestClient

from app.catalog import MAX_SAMPLE_SIZE, SAMPLE_RECORDS, SampleLimitExceeded, bounded_sample
from app.engine import evaluate_plan, raw_source_totals, totals_for
from app.main import create_app
from app.transforms import catalog_is_implemented


def client_for(tmp_path):
    app = create_app(str(tmp_path / "workbench.db"))
    return TestClient(app)


def answer_and_approve(client: TestClient, plan: dict) -> dict:
    plan = client.post(
        f"/api/plans/{plan['id']}/answers",
        json={"question_id": "q-region", "option_id": "use_constant"},
    ).json()
    plan = client.post(
        f"/api/plans/{plan['id']}/answers",
        json={"question_id": "q-signup-format", "option_id": "confirm_mdy"},
    ).json()
    return client.post(f"/api/plans/{plan['id']}/approve", json={"note": "Reviewed mappings."}).json()


def test_catalog_transforms_are_implemented():
    assert catalog_is_implemented()


def test_sample_is_bounded():
    assert len(SAMPLE_RECORDS) <= MAX_SAMPLE_SIZE
    try:
        bounded_sample([{"CUST_ID": str(i)} for i in range(MAX_SAMPLE_SIZE + 1)])
    except SampleLimitExceeded as exc:
        assert exc.count == MAX_SAMPLE_SIZE + 1
    else:
        raise AssertionError("sample limit was not enforced")


def test_agent_proposes_catalog_mappings_and_questions(tmp_path):
    client = client_for(tmp_path)
    plan = client.post("/api/agent/propose").json()
    body = plan["body"]
    by_target = {item["target_field"]: item for item in body["mappings"]}
    assert by_target["full_name"]["transform"] == "concat"
    assert by_target["full_name"]["source_fields"] == ["FIRST_NME", "LAST_NME"]
    assert by_target["signed_up_on"]["transform"] == "parse_date"
    assert by_target["signed_up_on"]["params"]["format"] == "%m/%d/%Y"
    assert by_target["status"]["transform"] == "map_enum"
    assert by_target["status"]["params"]["map"]["A"] == "active"
    assert by_target["region_code"]["transform"] == "constant"
    assert "NOTES" in body["unmapped_source_fields"]
    assert "region_code" in body["missing_target_fields"]
    assert {item["target_field"] for item in body["incompatibilities"]} >= {
        "signed_up_on",
        "status",
        "credit_limit",
        "loyalty_points",
    }
    assert {question["id"] for question in body["questions"]} == {"q-region", "q-signup-format"}
    assert all(question["answer"] is None for question in body["questions"])
    tools = {event["tool"] for event in body["tool_trace"]}
    assert tools <= {
        "inspect_source_schema",
        "inspect_target_schema",
        "inspect_sample_records",
        "list_supported_transforms",
        "profile_field",
        "check_type_compatibility",
        "validate_proposed_mapping",
    }
    assert "validate_proposed_mapping" in tools
    assert plan["open_questions"] == 2
    rejected = client.post(
        f"/api/plans/{plan['id']}/revisions",
        json={"mappings": [{"target_field": "email", "transform": "exec", "source_fields": ["EMAIL_ADDR"]}]},
    )
    assert rejected.status_code == 400


def test_execution_is_gated_versioned_idempotent_and_reversible(tmp_path):
    client = client_for(tmp_path)
    proposed = client.post("/api/agent/propose").json()
    blocked = client.post(f"/api/plans/{proposed['id']}/execute")
    assert blocked.status_code == 409

    plan = answer_and_approve(client, proposed)
    assert plan["status"] == "approved"
    assert plan["version"] == 3
    versions = client.get("/api/plans").json()
    assert [item["version"] for item in versions] == [1, 2, 3]
    assert [item["status"] for item in versions] == ["superseded", "superseded", "approved"]

    first = client.post(f"/api/plans/{plan['id']}/dry-run").json()
    second = client.post(f"/api/plans/{plan['id']}/dry-run").json()
    assert first["fingerprint"] == second["fingerprint"]
    assert first["source_count"] == len(SAMPLE_RECORDS) == 23
    assert first["transformed_count"] == 21
    assert first["accepted_count"] == 12
    assert first["rejected_count"] == 11
    assert first["source_count"] == first["accepted_count"] + first["rejected_count"]
    rules = {(error["source_key"], error["field"], error["rule"]) for error in first["errors"]}
    assert ("C-1010", "email", "email_normalize") in rules
    assert ("C-1011", "signed_up_on", "parse_date") in rules
    assert ("C-1014", "status", "map_enum") in rules
    assert ("C-1001", "CUST_ID", "duplicate_source_key") in rules
    assert ("", "CUST_ID", "missing_source_key") in rules
    assert any(error["record"] for error in first["errors"])

    loaded = client.post(f"/api/plans/{plan['id']}/execute").json()
    assert loaded["accepted_count"] == 12
    assert loaded["duplicate_skipped_count"] == 0
    assert loaded["is_retry"] is False
    target = client.get("/api/target").json()
    assert target["count"] == 12
    amina = next(row for row in target["rows"] if row["customer_id"] == "C-1001")
    assert amina["full_name"] == "Amina Rahman"
    assert amina["email"] == "amina.rahman@example.com"
    assert amina["status"] == "active"
    assert amina["signed_up_on"] == "2020-01-15"
    assert amina["phone_e164"] == "+14155550134"
    assert amina["region_code"] == "UNASSIGNED"

    retried = client.post(f"/api/plans/{plan['id']}/execute").json()
    assert retried["is_retry"] is True
    assert retried["accepted_count"] == 0
    assert retried["duplicate_skipped_count"] == 12
    assert client.get("/api/target").json()["count"] == 12

    reconcile = client.get("/api/reconcile").json()
    assert reconcile["in_balance"] is True
    assert reconcile["deltas"]["credit_limit_sum"] == "0.00"
    assert reconcile["target"]["accepted_rows"] == "12"

    unknown = client.post(
        f"/api/plans/{plan['id']}/revisions",
        json={"mappings": [{"target_field": "email", "transform": "exec", "source_fields": ["EMAIL_ADDR"]}]},
    )
    assert unknown.status_code == 409

    rolled = client.post(f"/api/runs/{loaded['id']}/rollback")
    assert rolled.status_code == 200
    assert client.get("/api/target").json()["count"] == 0
    assert client.get("/api/reconcile").json()["in_balance"] is False
    events = [item["event_type"] for item in client.get("/api/history").json()]
    assert events.index("plan_approved") < events.index("dry_run_completed")
    assert "migration_executed" in events
    assert "migration_retried" in events
    assert "migration_rolled_back" in events


def test_direct_evaluation_is_deterministic():
    plan = {
        "mappings": [
            {"target_field": "customer_id", "source_fields": ["CUST_ID"], "transform": "trim", "params": {}},
            {
                "target_field": "full_name",
                "source_fields": ["FIRST_NME", "LAST_NME"],
                "transform": "concat",
                "params": {"separator": " "},
            },
            {"target_field": "email", "source_fields": ["EMAIL_ADDR"], "transform": "email_normalize", "params": {}},
            {"target_field": "phone_e164", "source_fields": ["PHONE"], "transform": "phone_normalize", "params": {}},
            {
                "target_field": "signed_up_on",
                "source_fields": ["SIGNUP_DT"],
                "transform": "parse_date",
                "params": {"format": "%m/%d/%Y"},
            },
            {
                "target_field": "status",
                "source_fields": ["STATUS_CD"],
                "transform": "map_enum",
                "params": {"map": {"A": "active", "I": "inactive", "S": "suspended"}},
            },
            {"target_field": "credit_limit", "source_fields": ["CREDIT_LIM"], "transform": "parse_decimal", "params": {}},
            {"target_field": "loyalty_points", "source_fields": ["LOYALTY_PTS"], "transform": "parse_integer", "params": {}},
            {
                "target_field": "region_code",
                "source_fields": [],
                "transform": "constant",
                "params": {"value": "UNASSIGNED"},
            },
        ]
    }
    first = evaluate_plan(plan["mappings"])
    second = evaluate_plan(plan["mappings"])
    assert first.fingerprint == second.fingerprint
    assert first.identities_hold()
    jose = next(item for item in first.outcomes if item.source_key == "C-1020")
    assert jose.output["full_name"] == "José Navarro"
    assert jose.output["credit_limit"] == "980.10"


def test_duplicate_target_key_keeps_the_source_record():
    mappings = _approved_style_mappings()
    for mapping in mappings:
        if mapping["target_field"] == "customer_id":
            mapping["source_fields"] = []
            mapping["transform"] = "constant"
            mapping["params"] = {"value": "SAME"}
    evaluation = evaluate_plan(mappings, SAMPLE_RECORDS[:2])
    collided = [item for item in evaluation.outcomes if any(error.rule == "duplicate_target_key" for error in item.errors)]
    assert collided
    assert collided[0].record.get("CUST_ID")
    assert collided[0].errors[-1].record.get("CUST_ID") == collided[0].record["CUST_ID"]


def test_raw_source_totals_include_values_the_plan_rejects():
    raw = raw_source_totals()
    assert raw["rows"] == "23"
    assert int(raw["credit_unparsed"]) >= 1
    assert any(record.get("CREDIT_LIM") == "no-limit" for record in SAMPLE_RECORDS)
    accepted = totals_for(evaluate_plan(_approved_style_mappings()).outcomes)
    assert Decimal(raw["credit_limit_sum"]) > Decimal(accepted["credit_limit_sum"])


def test_inputs_come_from_the_data_files(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = client_for(tmp_path)
    inputs = client.get("/api/inputs").json()
    assert inputs["sample_count"] == 23
    assert inputs["input_files"]["sample"] == "data/sample.json"
    assert inputs["input_files"]["source_schema"] == "data/source_schema.json"
    assert inputs["agent"]["configured"] is False
    reconcile = client.get("/api/reconcile").json()
    assert reconcile["raw_source"]["rows"] == "23"
    assert int(reconcile["raw_source"]["credit_unparsed"]) >= 1


def test_model_agent_is_optional_and_must_call_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    client = client_for(tmp_path)
    refused = client.post("/api/agent/propose?mode=model")
    assert refused.status_code == 409

    from app.agent import propose_plan
    from app.model_agent import REQUIRED_TOOLS, propose_with_model
    from app.service import normalize_mappings

    rules = propose_plan()
    calls = {"n": 0}

    def complete(messages, tools):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "1", "name": "inspect_source_schema", "arguments": {}},
                    {"id": "2", "name": "inspect_target_schema", "arguments": {}},
                    {"id": "3", "name": "inspect_sample_records", "arguments": {}},
                    {"id": "4", "name": "list_supported_transforms", "arguments": {}},
                    {
                        "id": "5",
                        "name": "validate_proposed_mapping",
                        "arguments": {
                            "target_field": "customer_id",
                            "source_fields": ["CUST_ID"],
                            "transform": "trim",
                            "params": {},
                        },
                    },
                ],
            }
        return {"role": "assistant", "content": json.dumps(rules), "tool_calls": []}

    plan = propose_with_model(complete=complete)
    assert plan["agent_mode"] == "model"
    assert REQUIRED_TOOLS <= {event["tool"] for event in plan["tool_trace"]}
    normalize_mappings(plan["mappings"])


def test_quarantine_release_loads_a_corrected_row_and_skips_a_loaded_key(tmp_path):
    client = client_for(tmp_path)
    plan = answer_and_approve(client, client.post("/api/agent/propose").json())
    loaded = client.post(f"/api/plans/{plan['id']}/execute").json()
    client.post(f"/api/plans/{plan['id']}/dry-run")
    inbox = client.get("/api/quarantine").json()
    assert any(case["run_id"] == loaded["id"] and case["source_key"] == "C-1010" for case in inbox["cases"])

    email_case = next(case for case in inbox["cases"] if case["source_key"] == "C-1010")
    assert any(error["field"] == "email" for error in email_case["errors"])
    record = dict(email_case["record"])
    still_bad = dict(record)
    still_bad["EMAIL_ADDR"] = "still-not-an-email"
    rejected = client.post(f"/api/quarantine/{email_case['id']}/release", json={"record": still_bad}).json()
    assert rejected["status"] == "open"
    assert rejected["release_errors"]
    assert client.get("/api/target").json()["count"] == 12
    record["EMAIL_ADDR"] = "fixed.customer@example.com"
    released = client.post(f"/api/quarantine/{email_case['id']}/release", json={"record": record})
    assert released.status_code == 200
    released_body = released.json()
    assert released_body["status"] == "released"
    assert released_body["run"]["is_retry"] is False
    assert released_body["run"]["accepted_count"] == 1
    assert client.get("/api/target").json()["count"] == 13
    fixed = next(row for row in client.get("/api/target").json()["rows"] if row["customer_id"] == "C-1010")
    assert fixed["email"] == "fixed.customer@example.com"
    again = client.post(f"/api/quarantine/{email_case['id']}/release", json={"record": record})
    assert again.status_code == 409

    duplicate = next(
        case
        for case in inbox["cases"]
        if case["source_key"] == "C-1001" and any(error["rule"] == "duplicate_source_key" for error in case["errors"])
    )
    skipped = client.post(
        f"/api/quarantine/{duplicate['id']}/release",
        json={"record": duplicate["record"]},
    ).json()
    assert skipped["status"] == "skipped_duplicate"
    assert client.get("/api/target").json()["count"] == 13

    rolled = client.post(f"/api/runs/{released_body['run']['id']}/rollback")
    assert rolled.status_code == 200
    assert client.get("/api/target").json()["count"] == 12
    reopened = next(case for case in client.get("/api/quarantine").json()["cases"] if case["id"] == email_case["id"])
    assert reopened["status"] == "open"
    assert reopened["record"]["EMAIL_ADDR"]
    events = [item["event_type"] for item in client.get("/api/history").json()]
    assert "quarantine_released" in events
    assert "quarantine_skipped_duplicate" in events


def _approved_style_mappings():
    return [
        {"target_field": "customer_id", "source_fields": ["CUST_ID"], "transform": "trim", "params": {}},
        {
            "target_field": "full_name",
            "source_fields": ["FIRST_NME", "LAST_NME"],
            "transform": "concat",
            "params": {"separator": " "},
        },
        {"target_field": "email", "source_fields": ["EMAIL_ADDR"], "transform": "email_normalize", "params": {}},
        {"target_field": "phone_e164", "source_fields": ["PHONE"], "transform": "phone_normalize", "params": {}},
        {
            "target_field": "signed_up_on",
            "source_fields": ["SIGNUP_DT"],
            "transform": "parse_date",
            "params": {"format": "%m/%d/%Y"},
        },
        {
            "target_field": "status",
            "source_fields": ["STATUS_CD"],
            "transform": "map_enum",
            "params": {"map": {"A": "active", "I": "inactive", "S": "suspended"}},
        },
        {"target_field": "credit_limit", "source_fields": ["CREDIT_LIM"], "transform": "parse_decimal", "params": {}},
        {"target_field": "loyalty_points", "source_fields": ["LOYALTY_PTS"], "transform": "parse_integer", "params": {}},
        {
            "target_field": "region_code",
            "source_fields": [],
            "transform": "constant",
            "params": {"value": "UNASSIGNED"},
        },
    ]
