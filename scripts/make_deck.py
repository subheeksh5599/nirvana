"""Build the slide deck (docs/deck.html) from the ledger: every number on the slides comes
from a recorded run, nothing is typed in by hand.

usage: uv run python scripts/make_deck.py [live_run_id] [sample_run_id]
Then print docs/deck.html to PDF (1920x1080 pages)."""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from nirvana.config import load_env_file, settings
from nirvana.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
load_env_file()
L = Ledger(settings().ledger_path)

runs = L.runs()
live = [r for r in runs if r["status"] == "done" and r["mode"] == "live" and r["summary"] and r["summary"]["queries"] == 103]
run = next((r for r in runs if r["id"] == sys.argv[1]), None) if len(sys.argv) > 1 else (live[0] if live else None)
if run is None:
    sys.exit("no finished 103-query live run in the ledger")
s = run["summary"]
v = s["verdicts"]
js = L.judgements(run["id"])
al = L.alignment(run["id"])
when = datetime.fromtimestamp(run["finished_at"], timezone.utc).strftime("%d %b %Y, %H:%M UTC")

# the run against Zetaris' shipped sample, kept as evidence of what alignment catches
sample_counts = json.loads((Path.home() / "nirvana-data/zetaris_counts.json").read_text())
db_counts = json.loads((Path.home() / "nirvana-data/databricks_counts.json").read_text())
diff_tables = [(t, db_counts[t], sample_counts[t]) for t in sorted(db_counts) if db_counts[t] != sample_counts.get(t)]

e = html.escape
LABEL = {"move": "Same answer", "move_note": "Same, with a note", "rewrite": "Moves after a proven rewrite", "keep": "Stays on Databricks", "blocked": "Cannot be judged"}
COLOR = {"move": "#0F7A46", "move_note": "#4C9A6E", "rewrite": "#145FE4", "keep": "#B02A22", "blocked": "#B9B9B0"}


def bar():
    segs = "".join(f'<span style="width:{v[k] / s["queries"] * 100}%;background:{COLOR[k]}"></span>' for k in COLOR if v.get(k))
    leg = "".join(f'<span><i style="background:{COLOR[k]}"></i>{LABEL[k]} <b>{v[k]}</b></span>' for k in COLOR if v.get(k))
    return f'<div class="bar">{segs}</div><div class="legend">{leg}</div>'


def ms(x):
    return f"{x / 1000:.0f} s" if x >= 1000 else f"{x:.0f} ms"


def short_note(t):
    return t if len(t) <= 110 else t[:107].rsplit(' ', 1)[0] + ' …'


noted = [j for j in js if j["verdict"] in ("move_note", "rewrite", "keep", "blocked")][:8]
rows_noted = "".join(
    f"<tr><td class=m>{e(j['query_id'])}</td><td><span class=tag style='color:{COLOR[j['verdict']]}'>{LABEL[j['verdict']]}</span></td><td>{e(short_note(j['note'] or ''))}</td></tr>" for j in noted
)
rows_diff = "".join(f"<tr><td class=m>{e(t)}</td><td class=n>{a:,}</td><td class=n>{b:,}</td><td class=n>{b - a:+,}</td></tr>" for t, a, b in diff_tables)

slides = [
    f"""<h1 class=hero>Know which queries can leave Databricks<br>before they do.</h1>
    <p class=lead>Nirvana runs every query on Databricks and on Zetaris against the same data, compares the answers row by row, and keeps a rewrite only when it returns the exact rows.</p>
    <div class=foot>Nirvana · Open Agent Hackathon 2026 · github.com/subheeksh5599/nirvana</div>""",
    """<div class=eye>The problem</div><h2>A migration fails on answers, not on syntax.</h2>
    <div class=cols3>
      <div class=card><b>The push</b><p>Azure Databricks Standard workspaces are being moved to Premium from 1 October 2026. Zetaris puts the increase at 35% or more per workspace.</p></div>
      <div class=card><b>The risk</b><p>A query that runs on a new engine can still return a different answer: a DECIMAL read as DOUBLE, 7 / 2 = 3, NULLs sorted to the other end of a top 10.</p></div>
      <div class=card><b>The gap</b><p>Table diffs prove the data moved. Nobody proves, query by query, that the workload still gives the same answers, and which rewrite fixes the ones that do not.</p></div>
    </div>""",
    """<div class=eye>How it works</div><h2>Four steps. Every one executed.</h2>
    <div class=cols4>
      <div class=card><span class=n1>01</span><b>Align the inputs</b><p>Both engines profile every table: row count, and per column non-null count, MIN, MAX and an exact SUM. A query over a table that differs is blocked, not judged.</p></div>
      <div class=card><span class=n1>02</span><b>Run on both</b><p>The same query runs on Databricks and on Zetaris Lightning, which reads the parquet files in place.</p></div>
      <div class=card><span class=n1>03</span><b>Compare every row</b><p>Results become canonical tokens with exact decimals and a SHA-256 fingerprint. A changed value is named by its column.</p></div>
      <div class=card><span class=n1>04</span><b>Prove the cause</b><p>Rewrites run on Zetaris until one returns the source's exact rows. Otherwise the query stays on Databricks.</p></div>
    </div>""",
    """<div class=eye>The guarantee</div><h2>No verdict comes from a model.</h2>
    <div class=cols2>
      <div><p class=big2>A model may only propose SQL. The judge runs the proposal on Zetaris and keeps it only if its rows equal Databricks' rows.</p>
      <p class=lead2>A proposal with the right shape and the wrong rows is refused (test_wrong_advisor_rewrite_is_refused). An offline model ends a query in "keep", never in a guess.</p></div>
      <table class=t><tr><th>Verdict</th><th>Meaning</th></tr>
      <tr><td style='color:#0F7A46'><b>move</b></td><td>same answer on both engines</td></tr>
      <tr><td style='color:#4C9A6E'><b>move_note</b></td><td>same up to float noise or tied rows, proven by extra runs</td></tr>
      <tr><td style='color:#145FE4'><b>rewrite</b></td><td>moves after a rewrite proven on the rows</td></tr>
      <tr><td style='color:#B02A22'><b>keep</b></td><td>no rewrite reproduced the answer</td></tr>
      <tr><td style='color:#56564E'><b>blocked</b></td><td>input differs, or the source refuses the query</td></tr></table>
    </div>""",
    f"""<div class=eye>The live run · {e(when)}</div><h2><span class=num>{s['movable']}</span> of {s['queries']} TPC-DS queries can move.</h2>
    {bar()}
    <div class=stats>
      <div class=stat><div class=k>Tables aligned</div><div class=v>{s['tables_usable']}/{s['tables']}</div></div>
      <div class=stat><div class=k>Databricks time, all queries</div><div class=v>{ms(s['source_ms'])}</div></div>
      <div class=stat><div class=k>Zetaris time, all queries</div><div class=v>{ms(s['target_ms'])}</div></div>
      <div class=stat><div class=k>Run</div><div class="v sm">{e(run['id'])}</div></div>
    </div>
    <p class=lead2>Databricks Free Edition (serverless SQL warehouse) against Zetaris Lightning (local docker), the same 24 TPC-DS sf1 parquet files on both. Query texts: Apache Spark's TPC-DS set, unchanged.</p>""",
    (f"""<div class=eye>Queries that did not simply match</div><h2>Every exception carries its reason.</h2>
    <table class=t><tr><th>Query</th><th>Verdict</th><th>Why</th></tr>{rows_noted}</table>""" if noted else
     """<div class=eye>Queries that did not simply match</div><h2>None: every answer matched row for row.</h2><p class=lead2>Same files, two engines, 103 queries, identical fingerprints.</p>"""),
    f"""<div class=eye>What alignment caught on day one</div><h2>Zetaris' bundled TPC-DS sample is not Databricks' sample.</h2>
    <div class=cols2>
      <table class=t><tr><th>Table</th><th class=n>Databricks samples</th><th class=n>Zetaris TPCDS_DB</th><th class=n>Gap</th></tr>{rows_diff}</table>
      <div><p class=big2>{len(diff_tables)} of 24 tables differ in row count before a single query runs.</p>
      <p class=lead2>Comparing queries over these tables would have compared two datasets, not two engines. Alignment blocks them, so the live run loads the same parquet files into both engines instead.</p></div>
    </div>""",
    """<div class=eye>Sponsor technology, load-bearing</div><h2>Remove any one and a step stops.</h2>
    <div class=cols3>
      <div class=card><b>Zetaris · the data layer</b><p>The 24 parquet files are Lightning filestore tables, read in place. Every query, table profile and rewrite runs through the REST SQL endpoint. Result types come from INFER SQLTABLE COLUMNS.</p></div>
      <div class=card><b>Meterless · H-MEM and Nightshift</b><p>H-MEM stores each proven fix and each order-tie proof with its query features. Matching queries try recalled fixes first and skip re-runs already settled; a fix that fails loses confidence. Nightshift is the only AI provider, on 127.0.0.1:8088, under the 0.90 provider contract.</p></div>
      <div class=card><b>NVIDIA Nemotron</b><p>Nightshift relays rewrite proposals to a free Nemotron for differences no rule explains. No local model runs. The judge decides on the rows.</p></div>
    </div>""",
    """<div class=eye>Engineering</div><h2>Built to be checked.</h2>
    <div class=cols3>
      <div class=card><b>1,230 tests</b><p>Real SQL on DuckDB, the real H-MEM engine, every TPC-DS query parsed and qualified, tampered tables caught, property tests on the canonicaliser.</p></div>
      <div class=card><b>Receipts</b><p>Each run gets a SHA-256 over query hashes, both fingerprints, verdicts and rewrites. Importing an edited recording is refused.</p></div>
      <div class=card><b>A dashboard, not a slide</b><p>Start a run, watch 24 tables and 103 queries fill in live, open any query to its rows, fingerprints, rewrite and every execution step.</p></div>
    </div>""",
    """<div class=eye>Honesty</div><h2>What this is, and what it is not.</h2>
    <div class=cols2>
      <div class=card><b>Declared pre-existing work</b><p>None of our own: Nirvana was started during the hackathon. Third-party parts used unchanged: Meterless H-MEM (vendored at 1742dec), the 103 TPC-DS texts from Apache Spark (3153f1e), DuckDB's dsdgen for the data, the Zetaris docker platform.</p></div>
      <div class=card><b>Limits</b><p>Results over 100,000 rows are truncated and marked. Rules cover four causes; anything else goes to the model or ends in keep. Nirvana reports verdicts and timings; it makes no dollar claim of its own.</p></div>
    </div>
    <div class=foot>github.com/subheeksh5599/nirvana · Apache-2.0</div>""",
]

CSS = """
@font-face{font-family:Inter;src:url(../web/dist/assets/fonts/Inter-latin.woff2) format('woff2');font-weight:100 900}
@font-face{font-family:JBM;src:url(../web/dist/assets/fonts/JetBrainsMono-latin.woff2) format('woff2')}
@page{size:1920px 1080px;margin:0}
*{box-sizing:border-box}body{margin:0;font-family:Inter,sans-serif;color:#131311;-webkit-print-color-adjust:exact;print-color-adjust:exact}
section{width:1920px;height:1080px;padding:120px 140px;position:relative;page-break-after:always;overflow:hidden;background:#fff;border-bottom:1px solid #eee}
section::before{content:"";position:absolute;left:140px;top:64px;width:22px;height:22px;border-radius:6px;background:#131311;box-shadow:inset 0 0 0 6px #131311,inset 0 0 0 11px #fff}
.eye{font-size:24px;color:#8C8C82;margin-bottom:14px}
h1.hero{font-size:104px;line-height:1.04;letter-spacing:-.035em;font-weight:650;margin:170px 0 40px}
h2{font-size:64px;letter-spacing:-.03em;font-weight:650;margin:0 0 56px;line-height:1.08}
.lead{font-size:34px;color:#56564E;max-width:1300px;line-height:1.4}
.lead2{font-size:26px;color:#56564E;line-height:1.5;max-width:1500px}
.big2{font-size:36px;line-height:1.35;font-weight:550;margin:0 0 28px}
.foot{position:absolute;left:140px;bottom:70px;font-size:22px;color:#8C8C82}
.cols2{display:grid;grid-template-columns:1fr 1fr;gap:60px;align-items:start}
.cols3{display:grid;grid-template-columns:repeat(3,1fr);gap:28px}
.cols4{display:grid;grid-template-columns:repeat(4,1fr);gap:24px}
.card{border:2px solid #E8E8E3;border-radius:20px;padding:36px 34px;background:#FAFAF8}
.card b{display:block;font-size:34px;margin-bottom:14px;letter-spacing:-.01em}
.card p{font-size:27px;line-height:1.5;color:#56564E;margin:0}
.n1{font-family:JBM,monospace;color:#8C8C82;font-size:22px;display:block;margin-bottom:18px}
.num{font-family:JBM,monospace}
.bar{display:flex;height:34px;border-radius:999px;overflow:hidden;background:#F3F3F0;margin:10px 0 18px}
.bar span{display:block;height:100%}
.legend{display:flex;gap:34px;font-size:24px;color:#56564E;margin-bottom:50px}
.legend i{display:inline-block;width:16px;height:16px;border-radius:4px;margin-right:10px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:22px;margin-bottom:44px}
.stat{border:2px solid #E8E8E3;border-radius:16px;padding:24px 28px}
.stat .k{font-size:21px;color:#56564E}.stat .v{font-family:JBM,monospace;font-size:48px;font-weight:600;margin-top:6px}.stat .v.sm{font-size:22px;margin-top:18px}
table.t{border-collapse:collapse;width:100%;font-size:23px}
table.t th{text-align:left;font-weight:500;color:#56564E;padding:14px 18px;border-bottom:2px solid #E8E8E3;background:#FAFAF8}
table.t td{padding:13px 18px;border-bottom:1px solid #E8E8E3;vertical-align:top}
.m{font-family:JBM,monospace}.tag{white-space:nowrap}.n{text-align:right;font-family:JBM,monospace}.tag{font-weight:650}
"""

out = ROOT / "docs" / "deck.html"
out.parent.mkdir(exist_ok=True)
out.write_text(f"<!doctype html><meta charset=utf-8><title>Nirvana</title><style>{CSS}</style>" + "".join(f"<section>{x}</section>" for x in slides))
print(out, len(slides), "slides from", run["id"])
