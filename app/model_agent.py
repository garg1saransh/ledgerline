"""Language-model mapping agent.

The model may call only the inspection and validation tools. It proposes a plan.
It cannot execute a load or invent a transform outside the catalog.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.agent import ALLOWED_TOOLS, Toolbelt, assert_plan_uses_catalog
from app.catalog import SOURCE_SCHEMA, TARGET_SCHEMA

REQUIRED_TOOLS = {
    "inspect_source_schema",
    "inspect_target_schema",
    "inspect_sample_records",
    "list_supported_transforms",
    "validate_proposed_mapping",
}

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "inspect_source_schema",
            "description": "Return the only source schema.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_target_schema",
            "description": "Return the only target schema.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_sample_records",
            "description": "Return the bounded sample, duplicate keys, and blank keys.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_supported_transforms",
            "description": "Return the closed transformation catalog.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "profile_field",
            "description": "Profile one source field: blanks, distinct values, and detected date format.",
            "parameters": {
                "type": "object",
                "properties": {"field": {"type": "string"}},
                "required": ["field"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_type_compatibility",
            "description": "Check whether a catalog transform can load a source type into a target type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_type": {"type": "string"},
                    "target_type": {"type": "string"},
                    "transform": {"type": "string"},
                },
                "required": ["source_type", "target_type", "transform"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_proposed_mapping",
            "description": "Run one proposed mapping across the sample and return failure counts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_field": {"type": "string"},
                    "source_fields": {"type": "array", "items": {"type": "string"}},
                    "transform": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["target_field", "source_fields", "transform"],
                "additionalProperties": False,
            },
        },
    },
]


class ModelNotConfigured(RuntimeError):
    pass


class ModelAgentError(RuntimeError):
    pass


_GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def _credentials() -> tuple[str, str, str, str]:
    gemini = os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
    if gemini:
        return (
            gemini,
            os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite").strip() or "gemini-3.1-flash-lite",
            os.environ.get("GEMINI_BASE_URL", _GEMINI_BASE).rstrip("/"),
            "google-ai-studio",
        )
    return (
        os.environ.get("OPENAI_API_KEY", "").strip(),
        os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        "openai-compatible",
    )


def model_status() -> dict:
    key, model, base_url, provider = _credentials()
    return {
        "configured": bool(key),
        "provider": provider,
        "model": model,
        "base_url": base_url,
    }


def propose_with_model(complete=None) -> dict:
    status = model_status()
    if complete is None and not status["configured"]:
        raise ModelNotConfigured(
            "The language-model agent needs GEMINI_API_KEY or OPENAI_API_KEY. "
            "The rules-based mapping agent is available without a key."
        )
    completer = complete or _openai_complete
    tools = Toolbelt()
    messages = [
        {"role": "system", "content": _system_prompt()},
        {
            "role": "user",
            "content": (
                f"Propose a migration plan from {SOURCE_SCHEMA['name']} to {TARGET_SCHEMA['name']}. "
                "Call the inspection tools first. Then return only the plan JSON."
            ),
        },
    ]
    for _ in range(10):
        message = completer(messages, TOOL_SPECS)
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            messages.append(message)
            for call in tool_calls:
                name = call["name"]
                if name not in ALLOWED_TOOLS:
                    result = {"error": f"{name} is not an inspection tool."}
                else:
                    try:
                        result = tools.dispatch(name, call.get("arguments") or {})
                    except Exception as exc:  # tool arguments can be malformed
                        result = {"error": str(exc)}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": name,
                        "content": json.dumps(_compact(name, result), ensure_ascii=False),
                    }
                )
            continue
        missing = REQUIRED_TOOLS - {event["tool"] for event in tools.trace}
        if missing:
            messages.append(
                {
                    "role": "user",
                    "content": "Call these tools before the final plan: " + ", ".join(sorted(missing)) + ".",
                }
            )
            continue
        plan = _parse_plan(message.get("content") or "")
        _coerce_confidence(plan)
        from app.service import ServiceError, normalize_mappings

        try:
            plan["mappings"] = normalize_mappings(plan["mappings"])
        except ServiceError as exc:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That plan was rejected: "
                        + exc.detail
                        + " Use only catalog transforms and include required params, "
                        "such as a strptime format for parse_date. Return the full plan JSON."
                    ),
                }
            )
            continue
        plan["tool_trace"] = tools.trace
        plan["agent_mode"] = "model"
        plan["model"] = status["model"]
        assert_plan_uses_catalog(plan)
        return plan
    raise ModelAgentError("The language-model agent did not return a plan after using the inspection tools.")


def _system_prompt() -> str:
    return (
        "You are the mapping agent for one bounded dataset migration. "
        "Use only the provided inspection and validation tools. "
        "Suggest only transforms from list_supported_transforms. "
        "Do not write transformation code, do not execute a load, and do not invent fields. "
        "When the tools have been used, reply with one JSON object and no markdown. "
        "The object must include mappings, risks, questions, incompatibilities, "
        "missing_target_fields, unmapped_source_fields, and agent_summary. "
        "Each mapping needs target_field, source_fields, transform, params, confidence, rationale, and risk. "
        "Ask blocking clarification questions when a required target field has no source column "
        "or a date format needs confirmation. Leave question answers null."
    )


def _parse_plan(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelAgentError("The language-model agent returned a plan that is not JSON.") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("mappings"), list):
        raise ModelAgentError("The language-model agent returned a plan without mappings.")
    plan.setdefault("risks", [])
    plan.setdefault("questions", [])
    plan.setdefault("incompatibilities", [])
    plan.setdefault("missing_target_fields", [])
    plan.setdefault("unmapped_source_fields", [])
    plan.setdefault("agent_summary", "Language-model mapping proposal.")
    plan["questions"] = _question_objects(plan.get("questions"))
    plan["risks"] = _risk_objects(plan.get("risks"))
    plan["incompatibilities"] = [item for item in plan.get("incompatibilities") or [] if isinstance(item, dict)]
    plan["missing_target_fields"] = [str(item) for item in plan.get("missing_target_fields") or [] if item]
    plan["unmapped_source_fields"] = [str(item) for item in plan.get("unmapped_source_fields") or [] if item]
    plan["dataset"] = SOURCE_SCHEMA["name"]
    plan["target"] = TARGET_SCHEMA["name"]
    return plan


def _question_objects(questions) -> list[dict]:
    cleaned = []
    for index, question in enumerate(questions or []):
        if isinstance(question, str) and question.strip():
            cleaned.append(
                {
                    "id": f"q-model-{index + 1}",
                    "prompt": question.strip(),
                    "blocking": False,
                    "options": [],
                    "answer": None,
                }
            )
        elif isinstance(question, dict) and question.get("prompt"):
            question.setdefault("id", f"q-model-{index + 1}")
            question.setdefault("blocking", False)
            question.setdefault("options", [])
            question.setdefault("answer", None)
            cleaned.append(question)
    return cleaned


def _risk_objects(risks) -> list[dict]:
    cleaned = []
    for risk in risks or []:
        if isinstance(risk, str) and risk.strip():
            cleaned.append({"severity": "medium", "code": "model_risk", "message": risk.strip()})
        elif isinstance(risk, dict) and risk.get("message"):
            risk.setdefault("severity", "medium")
            cleaned.append(risk)
    return cleaned


def _coerce_confidence(plan: dict) -> None:
    words = {"high": 0.9, "medium": 0.6, "low": 0.3}
    for mapping in plan.get("mappings") or []:
        value = mapping.get("confidence")
        if value is None or value == "":
            mapping.pop("confidence", None)
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = words.get(str(value).strip().lower())
        if number is None or not 0 <= number <= 1:
            mapping.pop("confidence", None)
        else:
            mapping["confidence"] = number


def _compact(name: str, result):
    if name == "inspect_sample_records" and isinstance(result, dict):
        preview = dict(result)
        records = preview.get("records") or []
        preview["records"] = records[:5]
        preview["records_truncated"] = len(records) > 5
        return preview
    return result


def _openai_complete(messages: list[dict], tools: list[dict]) -> dict:
    key, model, base_url, _provider = _credentials()
    payload_messages = [_to_api_message(message) for message in messages]
    response = None
    for attempt in range(3):
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": payload_messages,
                "tools": tools,
                "temperature": 0,
            },
            timeout=90,
        )
        if response.status_code not in {429, 503} or attempt == 2:
            break
        time.sleep(2 * (attempt + 1))
    if response.status_code >= 400:
        detail = _error_detail(response)
        message = f"The model request failed ({response.status_code})."
        if detail:
            message = f"{message} {detail}"
        raise ModelAgentError(message)
    message = response.json()["choices"][0]["message"]
    tool_calls = []
    for call in message.get("tool_calls") or []:
        arguments = call.get("function", {}).get("arguments") or "{}"
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        normalized = {
            "id": call.get("id") or call["function"]["name"],
            "name": call["function"]["name"],
            "arguments": arguments,
        }
        if call.get("extra_content"):
            normalized["extra_content"] = call["extra_content"]
        tool_calls.append(normalized)
    return {"role": "assistant", "content": message.get("content") or "", "tool_calls": tool_calls}


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error") or {}
    if isinstance(error, dict):
        return str(error.get("message") or "").strip()
    return str(error).strip()


def _tool_call_payload(call: dict) -> dict:
    payload = {
        "id": call["id"],
        "type": "function",
        "function": {"name": call["name"], "arguments": json.dumps(call.get("arguments") or {})},
    }
    if call.get("extra_content"):
        payload["extra_content"] = call["extra_content"]
    return payload


def _to_api_message(message: dict) -> dict:
    if message.get("tool_calls"):
        return {
            "role": "assistant",
            "content": message.get("content") or "",
            "tool_calls": [_tool_call_payload(call) for call in message["tool_calls"]],
        }
    if message.get("role") == "tool":
        return {"role": "tool", "tool_call_id": message["tool_call_id"], "content": message["content"]}
    return {"role": message["role"], "content": message.get("content") or ""}
