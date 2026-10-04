# Ledgerline

Ledgerline is a customer migration workbench. It plans the move from `legacy_customer_export` into `customer_master`, asks a person to approve that plan, then dry-runs, loads, reconciles, and can roll the load back.

Built by Saransh Garg.

## Run

Requirements: Python 3.11 or newer.

```powershell
cd workbench
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m pytest
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000

On macOS or Linux, activate the virtual environment with `source .venv/bin/activate` and copy the example env file with `cp .env.example .env`.

Leave `.env` empty to use the rules agent. To use the language-model agent, set `GEMINI_API_KEY` or `OPENAI_API_KEY` in `.env`. The app reads `GEMINI_API_KEY` first. `GEMINI_MODEL` defaults to `gemini-3.1-flash-lite`. `OPENAI_MODEL` defaults to `gpt-4o-mini`.

## How to use it

1. Open **Agent** and run the rules agent, or the language-model agent when a key is configured.
2. Open **Plan**, answer the region and signup questions, then approve the plan.
3. **Dry run** the plan. Staging does not change. The same plan produces the same counts.
4. Review **Quarantine**. Correct a source field and release the row through the approved plan, or leave it quarantined.
5. **Staging** loads the approved plan into `customer_master`. A later load skips customers already stored.
6. **Reconciliation** compares the approved plan’s accepted totals with the rows still in staging, and also shows raw source totals.
7. **History** records the approval, dry run, load, retry, release, and rollback.

## Architecture

The browser is a static workbench in `static/`. It talks only to the FastAPI app in `app/main.py`.

| Piece | Role |
| --- | --- |
| `data/` | Source schema, target schema, transform catalog, and the 23-row customer extract |
| `app/agent.py` | Rules agent. It proposes a plan through the inspection tools |
| `app/model_agent.py` | Optional language-model agent. Same tools, no load access |
| `app/transforms.py` | Closed transform catalog. No arbitrary code |
| `app/engine.py` | Deterministic evaluation, quarantine evidence, and totals |
| `app/service.py` | Plan versions, approval, dry run, execute, retry, release, rollback, reconciliation |
| `app/db.py` | SQLite persistence. The database file is created locally and is not part of the repository |
| `tests/test_workbench.py` | The migration flow and the agent contract |

Staging is the SQLite table `target_customers`. Plans, runs, quarantine cases, and history live in the same database.

## Agent

The default agent is deterministic. It may call only these tools:

- `inspect_source_schema`
- `inspect_target_schema`
- `inspect_sample_records`
- `list_supported_transforms`
- `profile_field`
- `check_type_compatibility`
- `validate_proposed_mapping`

It proposes mappings, names incompatible and missing fields, explains risks, and asks two blocking questions: what to do with the missing `region_code`, and whether `SIGNUP_DT` is `MM/DD/YYYY`. A load is rejected until those questions are answered and a person approves the plan.

`POST /api/agent/propose?mode=model` uses the same tools. The model returns a plan. It cannot execute a load or add a transform outside the catalog. Without an API key that route returns 409 and the rules agent remains available.

The rules proposal maps the Northline export with trim, concatenation of the two name fields, email and phone normalization, date parsing, status-code mapping, decimal and integer parsing, and a constant region when that option is chosen. Notes have no target column, so they are reported as dropped. Invalid emails, dates, status codes, amounts, blank keys, and duplicate source keys are quarantined with the field, value, rule, and message.

## Sample inputs

The workbench ships with one source and one target:

- Source: `legacy_customer_export`, primary key `CUST_ID`
- Target: `customer_master`, primary key `customer_id`
- Extract: 23 customer rows in `data/sample.json`, including names, emails, phones, signup dates, status codes, credit limits, and loyalty points
- Hard cap: 500 rows, set in `data/manifest.json`

After the region constant is accepted and `MM/DD/YYYY` is confirmed, a dry run of the rules plan reads 23 rows, transforms 21, accepts 12, and quarantines 11.

## Counts

- **Source** — rows read from the customer extract
- **Transformed** — rows that entered the mapping pipeline
- **Accepted** — rows that passed validation. On a load, rows newly inserted into staging
- **Rejected** — rows quarantined with field-level evidence
- **Duplicate skipped** — valid rows not inserted because that target key is already loaded

Reconciliation compares the accepted rows of the latest approved plan with the rows still in staging. A released quarantine row can make staging larger than that accepted count until that release is rolled back. Raw source totals are shown separately and still include values the plan rejected.

## Tests

From the `workbench` directory:

```powershell
python -m pytest
```

The tests cover the transform catalog, the 500-row cap, the rules proposal, the approval gate, deterministic dry-run counts, execute, duplicate skip on retry, reconciliation, rollback, history, file-based inputs, the optional model path without a key, and quarantine release.

## Scope

Included:

- One source and one target
- Versioned plans and a human approval gate
- Deterministic dry run and field-level quarantine
- Execute into a local staging table, idempotent retry, and rollback
- Reconciliation of accepted totals against staging, plus raw source totals
- History of approval, dry run, load, retry, release, and rollback

Left out:

- Production database access
- Arbitrary transformation code
- Distributed migration
- Live cloud connectors

## Review

The review copy is https://concern-roll-treatments-apnic.trycloudflare.com

No login. Open that address and use the workbench. The customer extract is the 23 rows already loaded in the app. The rules agent runs with no key. The language-model agent is available on that host.

Leave the computer that serves this address awake until review is finished. The address stops working if that machine sleeps or the tunnel stops.

## Deployment

This repository is the application. It runs as one FastAPI process and stores state in a local SQLite file, `workbench.db`, which is created on first start and is gitignored.

To host it, install the requirements, set `GEMINI_API_KEY` or `OPENAI_API_KEY` in the host environment if the language-model agent should be available, and run:

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Do not commit `.env`, the database, or the virtual environment. The variable names are listed in `.env.example`.
