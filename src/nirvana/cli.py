"""Command line: ``nirvana serve``, ``nirvana run``, ``nirvana status``, ``nirvana export``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import workload as wl
from .config import load_env_file, settings
from .engines import build
from .ledger import Ledger
from .runner import Run


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_status(args) -> int:
    s = settings()
    out = {"configured": s.configured(), "source": s.source, "target": s.target}
    for role, kind in (("source", s.source), ("target", s.target)):
        try:
            st = build(kind, s, role=role).ping()
            out[role] = {"engine": st.engine, "ok": st.ok, "detail": st.detail}
        except ValueError as e:
            out[role] = {"engine": kind, "ok": False, "detail": str(e)}
    _print(out)
    return 0 if out["source"]["ok"] and out["target"]["ok"] else 1


def cmd_run(args) -> int:
    s = settings()
    queries = wl.select(args.queries or None)
    kind_src, kind_tgt = (("rehearsal", "rehearsal") if args.rehearsal else (s.source, s.target))
    src, tgt = build(kind_src, s, role="source"), build(kind_tgt, s, role="target")
    advisor = memory = None
    if args.model:
        from .meterless import ModelAdvisor, MeterlessProvider, auto_detect, pick_model

        p = MeterlessProvider(s.meterless_url, s.meterless_token, model=s.meterless_model) if s.meterless_url else auto_detect(s.meterless_token, model=s.meterless_model)[0]
        if p is not None:
            st = p.status()
            if st.state == "online":
                p.model = pick_model(st.models, s.meterless_model)
                advisor = ModelAdvisor(p)
        print(f"model: {'on, ' + p.url + ' ' + p.model if advisor and p else 'not reachable, rules only'}", file=sys.stderr)
    if args.memory:
        from .memory import HMemMemory

        memory = HMemMemory(s.memory_dir)
    ledger = Ledger(s.ledger_path)
    run = Run(ledger, src, tgt, queries, mode="rehearsal" if args.rehearsal else "live", advisor=advisor, memory=memory,
              max_rows=s.max_rows, recordings_dir=None if args.rehearsal else s.recordings_dir)
    print(f"run {run.id}: {len(queries)} queries, {src.name} -> {tgt.name}", file=sys.stderr)
    summary = run.execute()
    for j in ledger.judgements(run.id):
        print(f"{j['query_id']:>5}  {j['verdict']:<9}  {(j.get('cause') or ''):<18}  {j['note'][:90]}", file=sys.stderr)
    _print({"run": run.id, "summary": summary, "status": ledger.run(run.id)["status"]})
    return 0 if summary is not None else 1


def cmd_export(args) -> int:
    s = settings()
    ledger = Ledger(s.ledger_path)
    rec = ledger.export(args.run_id)
    out = Path(args.out or s.recordings_dir / f"{args.run_id}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rec, indent=1, sort_keys=True, default=str), encoding="utf-8")
    print(out)
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    uvicorn.run(create_app(mode=args.mode), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    p = argparse.ArgumentParser(prog="nirvana", description="Prove which queries can leave Databricks before they move.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="start the HTTP API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--mode", choices=["live", "recorded"], default=None)
    sp.set_defaults(func=cmd_serve)
    sr = sub.add_parser("run", help="judge queries now and print the ledger")
    sr.add_argument("queries", nargs="*", help="query ids (default: all 103)")
    sr.add_argument("--rehearsal", action="store_true", help="use the local DuckDB rehearsal pair instead of the real engines")
    sr.add_argument("--no-model", dest="model", action="store_false")
    sr.add_argument("--no-memory", dest="memory", action="store_false")
    sr.set_defaults(func=cmd_run)
    ss = sub.add_parser("status", help="check both engines with SELECT 1")
    ss.set_defaults(func=cmd_status)
    se = sub.add_parser("export", help="write a run as a recording")
    se.add_argument("run_id")
    se.add_argument("--out")
    se.set_defaults(func=cmd_export)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
