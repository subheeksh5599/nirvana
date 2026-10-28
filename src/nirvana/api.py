"""HTTP API for the frontend.

Two modes:

* ``live`` (default) - runs are started against the configured engines (or the labelled
  local rehearsal) and stream their progress;
* ``recorded`` (``NIRVANA_MODE=recorded``) - no engines are contacted; finished real runs
  are served from ``recordings/``. Every recorded run says so in its payload.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query as Q
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from . import workload as wl
from .config import Settings, load_env_file, settings as read_settings
from .engines import Engine, build
from .federate import DiffStore, row_diff
from .guard import GuardStore, build_guard
from .investigate import Agent, InvestigationStore
from .audit import Auditor, AuditStore
from .ledger import Ledger
from .runner import Run, load_recordings

TERMINAL = ("done", "failed", "cancelled")


class RunRequest(BaseModel):
    engines: str = Field("live", description="live (configured source and target) or rehearsal (local, labelled)")
    queries: list[str] | None = Field(None, description="query ids; all 103 when omitted")
    use_model: bool = Field(True, description="ask the Meterless runtime for a rewrite when the rules find none")
    use_memory: bool = Field(True, description="recall and store proven fixes in H-MEM")
    target_location: str | None = Field(None, description="the Zetaris database to judge against; the configured one when omitted")
    explained_by: list[str] | None = Field(None, description="ids of finished agent investigations whose proven findings may clear blocked tables")


class DiffRequest(BaseModel):
    table: str = Field(..., description="a TPC-DS table name")
    reference: str = Field(..., description="the Zetaris database holding the copy the source was loaded from")
    candidate: str = Field(..., description="the Zetaris database holding the copy to check")
    sample: int = Field(5, ge=0, le=50)


class GuardRequest(BaseModel):
    table: str = Field(..., description="a TPC-DS table name")
    candidate: str = Field(..., description="the Zetaris database holding the migrated copy")
    columns: list[str] | None = Field(None, description="only these columns; every column when omitted")
    sample: int = Field(5, ge=0, le=50)


class InvestigateRequest(BaseModel):
    run_id: str = Field(..., description="the run whose blocked table or kept query to investigate")
    table: str | None = Field(None, description="a table the run's alignment blocked")
    query_id: str | None = Field(None, description="a query the run kept on the source")


class AuditRequest(BaseModel):
    target_location: str = Field(..., description="the Zetaris database to audit")
    queries: list[str] | None = Field(None, description="query ids; all 103 when omitted")


class State:
    def __init__(self, s: Settings, mode: str) -> None:
        self.settings = s
        self.mode = mode
        self.ledger = Ledger(s.ledger_path if mode == "live" else ":memory:")
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()
        self._memory: Any = None
        self._memory_error: str | None = None
        self.loaded = load_recordings(self.ledger, s.recordings_dir)

    def memory(self):
        if self._memory is None and self._memory_error is None:
            try:
                from .memory import HMemMemory

                self._memory = HMemMemory(self.settings.memory_dir)
            except Exception as e:
                self._memory_error = str(e)
        return self._memory

    def provider(self):
        from .meterless import model_provider

        return model_provider(self.settings)


def create_app(s: Settings | None = None, mode: str | None = None) -> FastAPI:
    if s is None:
        load_env_file()
        s = read_settings()
    mode = mode or os.environ.get("NIRVANA_MODE", "live")
    if mode not in ("live", "recorded"):
        raise ValueError("NIRVANA_MODE must be live or recorded")
    state = State(s, mode)
    app = FastAPI(title="Nirvana", version=__version__, description="Prove which queries can leave Databricks before they move.")
    app.state.nirvana = state
    origins = [o.strip() for o in os.environ.get("NIRVANA_CORS", "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    def get_run(run_id: str) -> dict[str, Any]:
        r = state.ledger.run(run_id)
        if r is None:
            raise HTTPException(404, f"no run {run_id}")
        return r

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "mode": state.mode, "recorded_runs": len(state.loaded)}

    @app.get("/api/status")
    def status(check: bool = Q(False, description="also run SELECT 1 on each engine and probe the model runtime")):
        out: dict[str, Any] = {"mode": state.mode, "configured": s.configured(), "source": s.source, "target": s.target}
        out["locations"] = {"source": s.source_location, "target": s.target_location}
        if check and state.mode == "live":
            engines = {}
            for role, kind in (("source", s.source), ("target", s.target)):
                try:
                    st = build(kind, s, role=role).ping()
                    engines[role] = {"engine": st.engine, "ok": st.ok, "detail": st.detail}
                except ValueError as e:
                    engines[role] = {"engine": kind, "ok": False, "detail": str(e)}
            out["engines"] = engines
            _, pst = state.provider()
            out["model"] = None if pst is None else {"state": pst.state, "url": pst.url, "models": list(pst.models)[:20], "detail": pst.detail}
            mem = state.memory()
            out["memory"] = mem.stats() if mem else {"error": state._memory_error}
        return out

    @app.get("/api/workload")
    def workload():
        return [
            {
                "id": q.id,
                "tables": wl.tables_read(q.sql),
                "ordered": wl.has_top_level_order(q.sql),
                "limit": wl.top_level_limit(q.sql),
                "sha256": q.sha256,
                "lines": q.sql.count("\n") + 1,
            }
            for q in wl.tpcds()
        ]

    @app.get("/api/workload/{query_id}")
    def workload_query(query_id: str):
        try:
            q = wl.get(query_id)
        except KeyError:
            raise HTTPException(404, f"no query {query_id}")
        return {
            "id": q.id,
            "sql": q.sql,
            "tables": wl.tables_read(q.sql),
            "source_sql": wl.qualify(q.sql, s.source_location, "databricks"),
            "target_sql": wl.qualify(q.sql, s.target_location, "spark"),
        }

    @app.post("/api/runs", status_code=202)
    def start_run(req: RunRequest):
        if state.mode != "live":
            raise HTTPException(409, "this server only serves recorded runs; start it in live mode with the engines running")
        try:
            queries = wl.select(req.queries)
        except KeyError as e:
            raise HTTPException(400, str(e))
        if req.engines == "rehearsal":
            src, tgt, mode = build("rehearsal", s, role="source"), build("rehearsal", s, role="target"), "rehearsal"
        elif req.engines == "live":
            ts = s
            if req.target_location:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", req.target_location):
                    raise HTTPException(400, "target_location must be a plain database name")
                ts = dataclasses.replace(s, target_location=req.target_location)
            try:
                src, tgt = build(s.source, s, role="source"), build(s.target, ts, role="target")
            except ValueError as e:
                raise HTTPException(400, f"engines not configured: {e}")
            mode = "live"
        else:
            raise HTTPException(400, "engines must be live or rehearsal")
        with state.lock:
            busy = [r for r in state.runs.values() if r.thread and r.thread.is_alive()]
            if busy:
                raise HTTPException(409, f"run {busy[0].id} is still going")
            advisor = None
            if req.use_model:
                p, _ = state.provider()
                if p is not None:
                    from .meterless import ModelAdvisor

                    advisor = ModelAdvisor(p)
            memory = state.memory() if req.use_memory else None
            explained = {}
            for inv_id in req.explained_by or []:
                inv = investigations.get(inv_id)
                if inv is None or inv.get("status") != "done" or inv.get("kind") != "table" or not inv.get("cleared"):
                    raise HTTPException(400, f"investigation {inv_id} did not clear its table")
                fs = [f for f in inv["findings"] if f["kind"] in ("same_values", "repair")]
                explained[inv["subject"]] = {"investigation": inv_id, "columns": [f["column"] for f in fs],
                                             "repairs": {f["column"]: f["expression"] for f in fs if f["kind"] == "repair" and f.get("expression")}}
            run = Run(state.ledger, src, tgt, queries, mode=mode, advisor=advisor, memory=memory, max_rows=s.max_rows,
                      recordings_dir=s.recordings_dir if mode == "live" else None, explained=explained)
            state.runs[run.id] = run
            run.start()
        return {"id": run.id, "mode": mode, "queries": len(queries), "model": advisor is not None, "memory": memory is not None}

    @app.get("/api/runs")
    def list_runs():
        return state.ledger.runs()

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        return get_run(run_id)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        get_run(run_id)
        r = state.runs.get(run_id)
        if r is None or not (r.thread and r.thread.is_alive()):
            raise HTTPException(409, "run is not in progress")
        r.cancel()
        return {"id": run_id, "cancelling": True}

    @app.get("/api/runs/{run_id}/alignment")
    def alignment(run_id: str):
        get_run(run_id)
        return state.ledger.alignment(run_id)

    @app.get("/api/runs/{run_id}/judgements")
    def judgements(run_id: str, verdict: str | None = None):
        get_run(run_id)
        out = []
        for j in state.ledger.judgements(run_id):
            if verdict and j["verdict"] != verdict:
                continue
            cmp = j.get("comparison") or {}
            out.append({
                "query_id": j["query_id"], "verdict": j["verdict"], "cause": j.get("cause"), "note": j["note"],
                "tables": j.get("tables", []), "source_ms": j.get("source_ms"), "target_ms": j.get("target_ms"),
                "source_rows": cmp.get("source_rows"), "target_rows": cmp.get("target_rows"), "status": cmp.get("status"),
                "rewritten": bool(j.get("rewritten_sql")),
            })
        return out

    @app.get("/api/runs/{run_id}/judgements/{query_id}")
    def judgement(run_id: str, query_id: str):
        get_run(run_id)
        j = state.ledger.judgement(run_id, query_id)
        if j is None:
            raise HTTPException(404, f"no judgement for {query_id} in {run_id}")
        return j

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0):
        get_run(run_id)
        return state.ledger.events(run_id, after)

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str, after: int = 0):
        get_run(run_id)

        async def gen():
            last = after
            while True:
                evs = state.ledger.events(run_id, last)
                for e in evs:
                    last = e["seq"]
                    yield f"id: {e['seq']}\nevent: {e['kind']}\ndata: {json.dumps(e, default=str)}\n\n"
                run = state.ledger.run(run_id)
                if run and run["status"] in TERMINAL and not evs:
                    yield f"event: end\ndata: {json.dumps({'status': run['status']})}\n\n"
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/runs/{run_id}/receipt")
    def receipt(run_id: str):
        get_run(run_id)
        return state.ledger.receipt(run_id)

    @app.get("/api/runs/{run_id}/recording")
    def recording(run_id: str):
        get_run(run_id)
        return JSONResponse(state.ledger.export(run_id), headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    investigations = InvestigationStore(s.recordings_dir / "investigations.jsonl")

    @app.get("/api/investigations")
    def list_investigations():
        return investigations.all()

    @app.get("/api/investigations/{inv_id}")
    def get_investigation(inv_id: str):
        inv = investigations.get(inv_id)
        if inv is None:
            raise HTTPException(404, f"no investigation {inv_id}")
        return inv

    @app.post("/api/investigations", status_code=202)
    def start_investigation(req: InvestigateRequest):
        if state.mode != "live":
            raise HTTPException(409, "this server only serves recorded results; start it in live mode with both engines running")
        run = state.ledger.run(req.run_id)
        if run is None:
            raise HTTPException(404, f"no run {req.run_id}")
        if bool(req.table) == bool(req.query_id):
            raise HTTPException(400, "name exactly one of table or query_id")
        if any(i.get("status") == "running" for i in investigations.all()):
            raise HTTPException(409, "an investigation is still running")
        al: dict[str, Any] | None = None
        j: dict[str, Any] | None = None
        if req.table:
            al = next((a for a in state.ledger.alignment(req.run_id) if a["table"] == req.table), None)
            if al is None or al["status"] != "not_aligned":
                raise HTTPException(400, f"{req.table} was not blocked in {req.run_id}")
        else:
            j = state.ledger.judgement(req.run_id, req.query_id)
            if j is None:
                raise HTTPException(404, f"no judgement {req.query_id} in {req.run_id}")
            drift = {c["column"].lower(): c["source"] for a in state.ledger.alignment(req.run_id) if a["table"] in (j.get("tables") or []) for c in a["type_changes"]}
            j = {**j, "drift": drift}
        provider, _ = state.provider()
        if provider is None:
            raise HTTPException(400, "no model route: start Meterless Nightshift with a cloud provider")
        candidate = (run.get("target") or {}).get("location") or s.target_location
        try:
            src, z = build(s.source, s, role="source"), build("zetaris", s, role="target")
        except ValueError as e:
            raise HTTPException(400, f"engines not configured: {e}")
        first: dict[str, Any] = {}

        def emit(inv):
            inv.run_id = req.run_id
            first.setdefault("id", inv.id)
            investigations.put(inv)

        agent = Agent(provider, src, z, source_location=s.source_location, memory=state.memory(), on_step=emit)
        _, pst = state.provider()
        fallbacks = [m for m in (pst.models if pst else ()) if "nemotron" in m.lower() and m.endswith(":free") and m != provider.model and "safety" not in m and "embed" not in m]
        agent.fallback_model = fallbacks[0] if fallbacks else None

        def work():
            if req.table:
                agent.investigate_table(req.table, candidate, al or {})
            else:
                agent.investigate_query(req.query_id, candidate, j or {})

        threading.Thread(target=work, daemon=True).start()
        for _ in range(600):
            if "id" in first:
                break
            time.sleep(0.05)
        return {"id": first.get("id"), "run_id": req.run_id, "table": req.table, "query_id": req.query_id, "candidate": candidate}

    audits = AuditStore(s.recordings_dir / "audits.jsonl")

    def _audit_run(queries: list[str] | None, explained_by: list[str], target: str) -> str:
        r = start_run(RunRequest(engines="live", queries=queries, target_location=target, use_model=False, explained_by=explained_by or None))
        run = state.runs[r["id"]]
        if run.thread:
            run.thread.join()
        return r["id"]

    def _audit_investigate(run_id: str, table: str | None, query_id: str | None) -> dict[str, Any]:
        r = start_investigation(InvestigateRequest(run_id=run_id, table=table, query_id=query_id))
        while True:
            inv = investigations.get(r["id"])
            if inv and inv["status"] != "running":
                return inv
            time.sleep(1)

    @app.get("/api/audits")
    def list_audits():
        return audits.all()

    @app.get("/api/audits/{audit_id}")
    def get_audit(audit_id: str):
        a = audits.get(audit_id)
        if a is None:
            raise HTTPException(404, f"no audit {audit_id}")
        return a

    @app.post("/api/audits", status_code=202)
    def start_audit(req: AuditRequest):
        if state.mode != "live":
            raise HTTPException(409, "this server only serves recorded results")
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", req.target_location):
            raise HTTPException(400, "target_location must be a plain database name")
        if any(a.get("status") == "running" for a in audits.all()):
            raise HTTPException(409, "an audit is still running")
        with state.lock:
            busy = [r for r in state.runs.values() if r.thread and r.thread.is_alive()]
        if busy:
            raise HTTPException(409, f"wait for run {busy[0].id} to finish first")
        if req.queries:
            try:
                wl.select(req.queries)
            except KeyError as e:
                raise HTTPException(400, str(e))
        target = req.target_location
        auditor = Auditor(
            run_queries=lambda q, ex: _audit_run(q, ex, target),
            run_record=lambda rid: state.ledger.run(rid),
            alignment=lambda rid: state.ledger.alignment(rid),
            judgements=lambda rid: state.ledger.judgements(rid),
            investigate=_audit_investigate,
            guard=lambda t, c: run_guard(GuardRequest(table=t, candidate=target, columns=[c])),
            store=audits,
        )
        a = auditor.start(target, req.queries)
        return {"id": a.id, "target_location": target}

    diffs = DiffStore(s.recordings_dir / "row-diffs.jsonl")

    @app.get("/api/diffs")
    def list_diffs():
        return diffs.latest()

    @app.post("/api/diffs")
    def run_diff(req: DiffRequest):
        if state.mode != "live":
            raise HTTPException(409, "this server only serves recorded results; start it in live mode with Zetaris running")
        if req.table not in wl.TPCDS_TABLES:
            raise HTTPException(400, f"unknown table {req.table}")
        try:
            z = build("zetaris", s, role="target")
        except ValueError as e:
            raise HTTPException(400, f"Zetaris not configured: {e}")
        d = row_diff(z, req.table, req.reference, req.candidate, sample=req.sample, databricks_location=s.source_location)
        if d.error and d.error.startswith(("not a plain identifier", "not a plain location")):
            raise HTTPException(400, d.error)
        diffs.save(d)
        return d.as_dict()

    guards = GuardStore(s.recordings_dir / "guards.jsonl")

    @app.get("/api/guards")
    def list_guards():
        return guards.latest()

    @app.post("/api/guards")
    def run_guard(req: GuardRequest):
        if state.mode != "live":
            raise HTTPException(409, "this server only serves recorded results; start it in live mode with both engines running")
        if req.table not in wl.TPCDS_TABLES:
            raise HTTPException(400, f"unknown table {req.table}")
        try:
            src, z = build(s.source, s, role="source"), build("zetaris", s, role="target")
        except ValueError as e:
            raise HTTPException(400, f"engines not configured: {e}")
        g = build_guard(src, f"{s.source_location}.{req.table}", z, req.table, req.candidate, sample=req.sample, columns=req.columns)
        if g.error and g.error.startswith("not a plain identifier"):
            raise HTTPException(400, g.error)
        guards.save(g)
        return g.as_dict()

    @app.get("/api/memory")
    def memory(q: str = "status:different", top: int = 10):
        mem = state.memory()
        if mem is None:
            raise HTTPException(503, f"memory unavailable: {state._memory_error}")
        return {"stats": mem.stats(), "hits": mem.search(q, top)}

    web = Path(os.environ.get("NIRVANA_WEB", str(Path(__file__).resolve().parents[2] / "web" / "dist")))
    if web.is_dir() and (web / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=web / "assets", check_dir=False), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path == "api" or path.startswith("api/"):
                raise HTTPException(404, f"no endpoint /{path}")
            f = web / path
            if path and f.is_file() and web in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(web / "index.html")

    return app
