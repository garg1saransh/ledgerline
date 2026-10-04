"""HTTP API for the migration workbench."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.db import Database
from app.service import ServiceError, Workbench
from app.workspace import Workspace, WorkspaceError

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path("/tmp/workbench.db") if os.environ.get("VERCEL") else ROOT / "workbench.db"
STATIC_DIR = ROOT / "static"


class AnswerIn(BaseModel):
    question_id: str
    option_id: str


class ApproveIn(BaseModel):
    note: str | None = None


class CorrectionIn(BaseModel):
    record: dict = Field(default_factory=dict)


class RevisionIn(BaseModel):
    mappings: list[dict] = Field(default_factory=list)


def create_app(db_path: str | None = None) -> FastAPI:
    database = Database(db_path or str(DEFAULT_DB))
    workspace = Workspace.from_env(Path(database.path), database) if db_path is None else None
    workbench = Workbench(database)
    app = FastAPI(title="Ledgerline Migration Workbench", version="1.0.0")
    app.state.workbench = workbench

    @app.middleware("http")
    async def shared_workspace(request, call_next):
        if workspace is None or not request.url.path.startswith("/api/") or request.url.path in {"/api/health", "/api/inputs"}:
            return await call_next(request)
        try:
            async with workspace.gate:
                if request.method == "GET":
                    await run_in_threadpool(workspace.pull)
                    return await call_next(request)
                await run_in_threadpool(workspace.acquire_writer)
                try:
                    await run_in_threadpool(workspace.pull)
                    response = await call_next(request)
                    await run_in_threadpool(workspace.push_if_changed)
                    return response
                finally:
                    await run_in_threadpool(workspace.release_writer)
        except WorkspaceError as exc:
            return JSONResponse(status_code=503, content={"detail": exc.detail})

    @app.exception_handler(ServiceError)
    async def service_error(_request, exc: ServiceError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/inputs")
    def inputs():
        return workbench.inputs()

    @app.post("/api/agent/propose")
    def propose(mode: str = "rules"):
        return workbench.propose(mode)

    @app.get("/api/plans")
    def list_plans():
        return workbench.list_plans()

    @app.get("/api/plans/{plan_id}")
    def get_plan(plan_id: str):
        return workbench.get_plan(plan_id)

    @app.post("/api/plans/{plan_id}/revisions")
    def revise(plan_id: str, payload: RevisionIn):
        return workbench.save_revision(plan_id, payload.mappings)

    @app.post("/api/plans/{plan_id}/answers")
    def answer(plan_id: str, payload: AnswerIn):
        return workbench.answer(plan_id, payload.question_id, payload.option_id)

    @app.post("/api/plans/{plan_id}/approve")
    def approve(plan_id: str, payload: ApproveIn):
        return workbench.approve(plan_id, payload.note)

    @app.post("/api/plans/{plan_id}/dry-run")
    def dry_run(plan_id: str):
        return workbench.dry_run(plan_id)

    @app.post("/api/plans/{plan_id}/execute")
    def execute(plan_id: str):
        return workbench.execute(plan_id)

    @app.get("/api/runs")
    def list_runs():
        return workbench.list_runs()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return workbench.get_run(run_id)

    @app.post("/api/runs/{run_id}/rollback")
    def rollback(run_id: str):
        return workbench.rollback(run_id)

    @app.get("/api/quarantine")
    def quarantine(run_id: str | None = None):
        return workbench.quarantine_inbox(run_id)

    @app.post("/api/quarantine/{case_id}/release")
    def release_case(case_id: str, payload: CorrectionIn):
        return workbench.release_case(case_id, payload.record)

    @app.get("/api/target")
    def target():
        return workbench.target_rows()

    @app.get("/api/reconcile")
    def reconcile():
        return workbench.reconcile()

    @app.get("/api/history")
    def history():
        return workbench.history()

    @app.post("/api/reset")
    def reset():
        return workbench.reset()

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
