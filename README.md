<div align="center">

# NIRVANA

### Before a query leaves Databricks, it runs on both engines, and only a rewrite proven on the rows is kept.

[![tests](https://img.shields.io/badge/tests-1207%20passing-2ea44f)](#tests)
[![workload](https://img.shields.io/badge/workload-103%20TPC--DS%20queries-1f6feb)](#the-workload)
[![license](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![stack](https://img.shields.io/badge/Zetaris%20%C2%B7%20Meterless%20%C2%B7%20Nemotron-engines-6f42c1)](#where-each-sponsor-technology-is-load-bearing)

[![run it](https://img.shields.io/badge/run%20it-one%20command-black)](#-see-it-in-one-command)
[![verify](https://img.shields.io/badge/verify-every%20claim-black)](#verify-every-claim-in-one-command)
[![honesty](https://img.shields.io/badge/honesty-table-black)](#whats-real-vs-pending--the-honesty-table)

</div>

Azure Databricks has been moving every remaining Standard-tier workspace to Premium since 1 October 2026. Zetaris tells those customers to *"spin up a free cluster and run a real workload before you accept that increase"*. Nirvana is that sentence turned into an agent. It takes a workload, runs every query on Databricks and on Zetaris against the same data, compares the two answers row by row, and when they differ it finds the cause by running rewrites on Zetaris until one returns Databricks' exact rows. The output is a ledger with one line per query (**move**, **rewrite**, **keep** or **blocked**), and every line carries the fingerprints of both answers.

```
same input tables  ->  same query  ->  same rows        = move
same input tables  ->  same query  ->  different rows   -> a rewrite that returns the source's rows = rewrite
                                                         -> nothing proven                          = keep
different input tables                                                                             = blocked
```

## Live status

| Surface | Status | The evidence |
|---|---|---|
| Engine clients (Databricks SQL statements API, Zetaris REST, DuckDB) | built, tested | `tests/test_engines.py` |
| Source alignment, comparator, judge, rewrites | built, tested on real engines | `tests/test_align.py`, `tests/test_compare.py`, `tests/test_judge.py` |
| Meterless provider contract 0.90.0 and model advisor | built, tested against the contract | `tests/test_meterless.py` |
| Meterless H-MEM memory (vendored reference engine, unchanged) | built, tested with the real engine | `tests/test_memory.py`, `vendor/meterless/UPSTREAM.md` |
| HTTP API with live stream and recorded mode | built, tested | `tests/test_api.py` |
| Web app: landing page and dashboard (overview, live run, queries, tables, receipt, workload, memory, history) | built, served by the API, tested | `web/dist/`, `tests/test_web.py` |
| Model route: Meterless runtime, free Nemotron on OpenRouter, or NVIDIA's hosted Nemotron | built, tested; OpenRouter route answered live on 27 Oct 2026 | `tests/test_meterless.py` |
| Full rehearsal run, 103 queries on two local engines | run on 27 Oct 2026 | [See it in one command](#-see-it-in-one-command) |
| Full live run, Databricks Free Edition against Zetaris Lightning | **pending**: needs the two engines running | [honesty table](#whats-real-vs-pending--the-honesty-table) |

## The 20-second pitch

Every migration tool answers "did the data copy over?". The question a team actually signs off on is different: **does every report still give the same number on the new engine?** Two engines can hold identical tables and still answer differently. Integer division, NULL placement, a column a CSV loader turned from DECIMAL into DOUBLE, a LIMIT that cuts through tied rows: each one returns plausible numbers that are wrong. Nirvana finds those cases before cutover, names the cause, and keeps a rewrite only if executing it reproduces the source's rows. The language model can propose SQL. It never decides a verdict.

## Table of contents

- [Live status](#live-status)
- [The 20-second pitch](#the-20-second-pitch)
- [See it in one command](#-see-it-in-one-command)
- [Verify every claim in one command](#verify-every-claim-in-one-command)
- [What Nirvana is NOT](#what-nirvana-is-not)
- [The problem I set out to solve](#the-problem-i-set-out-to-solve)
- [How migration teams do this today](#how-migration-teams-do-this-today)
- [What I built](#what-i-built)
- [Architecture](#architecture)
- [The judging loop, step by step](#the-judging-loop-step-by-step)
- [Where the guarantee is enforced](#where-the-guarantee-is-enforced)
- [What source alignment measures](#what-source-alignment-measures)
- [Where the model sits](#where-the-model-sits)
- [Where each sponsor technology is load-bearing](#where-each-sponsor-technology-is-load-bearing)
- [The workload](#the-workload)
- [Engineering decisions & the traps that taught me something](#engineering-decisions--the-traps-that-taught-me-something)
- [What's real vs pending — the honesty table](#whats-real-vs-pending--the-honesty-table)
- [Attack → test](#attack--test)
- [Limitations](#limitations)
- [Security](#security)
- [Tech stack](#tech-stack)
- [Project layout](#project-layout)
- [Full command reference](#full-command-reference)
- [How I'd deploy it](#how-id-deploy-it)
- [Tests](#tests)
- [License](#license)

## ▶ See it in one command

No credentials needed. The rehearsal uses DuckDB's own TPC-DS generator to build the 24 tables at scale 0.01 (28,810 `store_sales` rows) and makes a second copy in which every DECIMAL column is stored as DOUBLE. That is what loading the same files as CSV with type inference does. It is labelled `rehearsal` everywhere it appears.

```bash
uv sync && npm ci
uv run nirvana run --rehearsal --no-model
```

Output of that command on 27 Oct 2026 (stderr ledger trimmed to four lines, stdout verbatim):

```
run run-20261027-164838-8dda10: 103 queries, rehearsal-source -> rehearsal-target
   q1  move                           same answer on both engines
  q30  blocked    source_error        the source cannot run this query: Binder Error: Referenced column "c_last_review_date" not
  q42  move_note  float_noise         floating-point differences only, largest relative gap 2.18e-16
  q90  blocked    source_error        the source cannot run this query: Parser Error: syntax error at or near "at"
{
  "run": "run-20261027-164838-8dda10",
  "summary": {
    "queries": 103,
    "verdicts": {
      "move": 57,
      "move_note": 44,
      "blocked": 2
    },
    "causes": {
      "float_noise": 44,
      "source_error": 2
    },
    "movable": 101,
    "movable_share": 0.9805825242718447,
    "tables": 24,
    "tables_usable": 24,
    "tables_retyped": 13,
    "source_ms": 299.7,
    "target_ms": 284.7
  },
  "status": "done"
}
```

What it found: 13 of the 24 tables changed type (every DECIMAL became DOUBLE), yet all 24 align at the precision the source declares. 44 queries give answers that differ only in the last bits of a floating-point sum. Those are reported as `move_note`, never as `move`. Two queries cannot run on the rehearsal source at all, and they are `blocked`, not counted as moves. `q30` reads `c_last_review_date`, a column the TPC-DS generator names `c_last_review_date_sk`. `q90` uses `at` as a table alias, which DuckDB reserves.

The live run, Databricks Free Edition against Zetaris Lightning, uses the same command without `--rehearsal`.

To watch it instead, run `uv run nirvana serve` and open http://127.0.0.1:8000. The landing page shows the latest run. The dashboard at `/app` starts runs, fills in the 24 tables and the 103 queries as they are judged, and opens any query to its rows, fingerprints, rewrite and every execution step.

## Verify every claim in one command

```bash
uv run pytest -q
```

```
1207 passed in 9.38s
```

The suite runs real SQL. Each test of the judge sets up DuckDB connections over the same generated TPC-DS rows and changes one documented thing on the target: `SET SESSION integer_division = true`, `SET SESSION default_null_order = 'nulls_last'`, the DECIMAL columns stored as REAL, or the rows of `item` stored in shuffled order. Then it asserts the cause the judge proves. None of these tests imitates an engine. HTTP doubles stand in only where a vendor's wire format is under test (Databricks statements API, Zetaris REST, Meterless provider), and they speak the shapes those vendors document.

## What Nirvana is NOT

- **Not a data copier.** It assumes the data is already readable by both engines. Zetaris reads it in place, so nothing has to be copied. Before judging any query it checks that the inputs match.
- **Not a SQL transpiler.** sqlglot renders the text for each engine. Nirvana's job is to prove whether the answer survives.
- **Not "the AI migrates your SQL".** The model is one optional proposer at the end of the line. Every verdict is a row comparison done by code.
- **Not new as a category.** Cross-database data diffing exists (see [How migration teams do this today](#how-migration-teams-do-this-today)). What is new here is the direction (off Databricks, onto an engine that federates the data in place), the cause proven by executing a rewrite, and the per-query ledger with receipts.

## The problem I set out to solve

A data team on Azure Databricks Standard was moved to Premium on a date it did not choose. Zetaris' own site calls it *"a 35%+ cost increase on every workspace, no opt-out"*. Moving the workload to another engine is the obvious answer, and the reason teams do not take it is that nobody can say which reports will still be right afterwards. A green test suite does not settle it. The two engines run the same text, the dashboards fill in, and the numbers are slightly off: a ratio rounded to an integer, a top-10 list with a different tenth row, a revenue figure that differs in the fourth decimal because a loader stored money as DOUBLE.

Nirvana answers the sign-off question, one query at a time, with evidence.

## How migration teams do this today

Before building I read how established migration and validation tools work, and kept their best ideas:

| Practice in the field | What Nirvana takes from it |
|---|---|
| Freeze the inputs and prove they match on both systems before diffing any output | [Source alignment](#what-source-alignment-measures) runs first; a query over an unaligned table is `blocked`, never judged |
| Translate, diff, repeat until parity, then hand back the code together with the diffs as sign-off evidence | the rewrite loop; the ledger keeps each attempt with its fingerprint |
| Multi-level checks: row count, column aggregates (count, sum, min, max), row hashes with NULL sentinels | the alignment profile; canonical row tokens with an explicit NULL |
| Numeric tolerance reported as its own mismatch class | `float_noise` is its own verdict with the largest gap stated, never folded into `move` |

The tools I read go one way: into a vendor's warehouse, validated table by table. Nirvana goes the other way, onto an engine that reads the same tables in place, and its unit of work is the query a report runs, not the table.

## What I built

- **Engine clients** for the Databricks SQL Statement Execution API (inline JSON results, polling, result chunks, cancel on timeout), the Zetaris Lightning REST SQL endpoint (`POST /sql-editor/sqls/run`) and DuckDB.
- **Source alignment**: one profile statement per table on each engine, compared at the precision the source declares.
- **A comparator** that treats results as multisets of canonical rows and pairs changed rows by their most-equal partner. A changed value then shows up as "column `half_month` differs", not as one row missing and one row extra.
- **A judge** that runs each query on both engines, then proves a cause by executing rewrites on the target: cast retyped columns back, make division floating point, write NULL placement explicitly, render the source dialect for the target. For ties at a LIMIT it runs two probes (no LIMIT, and a total order) before saying the source itself is non-deterministic.
- **Memory** in the Meterless H-MEM reference engine: proven fixes are stored with the facts of the difference, recalled first next time, and lose trust when they fail.
- **A model advisor** over the Meterless provider contract, asked only when every rule has failed. Its SQL is executed and compared like any other rewrite.
- **A ledger** in SQLite with receipts (SHA-256 over query hashes, both fingerprints, verdicts and rewrite hashes) and recordings that can be served without the engines.
- **An HTTP API** with a live event stream, used by the web app.

## Architecture

```
                   ┌───────────────────────── nirvana (Python) ─────────────────────────┐
 Databricks SQL ◄──┤ engines/databricks.py                                               │
 warehouse         │        │                                                            │
                   │        ▼                                                            │
                   │  align.py ──► judge.py ──► compare.py / canon.py                    │
                   │        ▲          │  ▲                                              │
 Zetaris        ◄──┤ engines/zetaris.py │  └── rewrite.py (rules)                        │
 Lightning         │                    ├────► meterless.py ──► Meterless runtime ───────┼──► Nemotron
                   │                    └────► memory.py ──► bridges/hmem.ts ──► H-MEM   │
                   │  runner.py ──► ledger.py (SQLite, receipts, recordings)             │
                   │  api.py (FastAPI, SSE) ◄── web app                                  │
                   └─────────────────────────────────────────────────────────────────────┘
```

| Component | File | Role |
|---|---|---|
| Model | `src/nirvana/model.py` | `ResultSet`, `Column`, type families, `EngineError` |
| Canonical values | `src/nirvana/canon.py` | tokens per type family; row fingerprints |
| Comparator | `src/nirvana/compare.py` | multiset diff, pairing, float-noise label |
| Workload | `src/nirvana/workload/__init__.py` | the 103 queries, table qualification per engine |
| Alignment | `src/nirvana/align.py` | per-table profile on both engines |
| Rewrites | `src/nirvana/rewrite.py` | rule-based candidate rewrites |
| Judge | `src/nirvana/judge.py` | verdict and proven cause per query |
| Model | `src/nirvana/meterless.py` | Meterless provider contract, advisor |
| Memory | `src/nirvana/memory.py`, `bridges/hmem.ts` | H-MEM over a JSON-lines bridge |
| Ledger | `src/nirvana/ledger.py` | runs, receipts, recordings |
| Runner | `src/nirvana/runner.py` | ping, align, judge, summarise |
| API | `src/nirvana/api.py` | HTTP surface below |

| Method | Path | What it returns |
|---|---|---|
| GET | `/api/health` | version, mode (`live` or `recorded`) |
| GET | `/api/status?check=true` | configured engines, `SELECT 1` on each, model runtime state, memory stats |
| GET | `/api/workload` | 103 queries with tables, ORDER BY, LIMIT, hash |
| GET | `/api/workload/{id}` | the text and its qualified form for each engine |
| POST | `/api/runs` | starts a run (`engines`: `live` or `rehearsal`) |
| GET | `/api/runs`, `/api/runs/{id}` | runs and their summaries |
| GET | `/api/runs/{id}/alignment` | per-table alignment |
| GET | `/api/runs/{id}/judgements[/{query}]` | verdicts; the full path of one judgement |
| GET | `/api/runs/{id}/events`, `/stream` | progress as JSON or server-sent events |
| GET | `/api/runs/{id}/receipt`, `/recording` | receipt hash; downloadable recording |
| POST | `/api/runs/{id}/cancel` | stops between queries |
| GET | `/api/memory?q=` | what H-MEM holds |

## The judging loop, step by step

1. **Blocked?** If any table the query reads failed alignment, the verdict is `blocked` and nothing runs.
2. **Source.** Run on the source. If the source refuses, the verdict is `blocked` (`source_error`): a query that does not run on the source has no answer to preserve.
3. **Target and compare.** Run on the target and compare as multisets, in order when the outermost statement has an ORDER BY.
   - identical → `move`
   - only floating-point gaps within 1e-9 relative → `move_note` (`float_noise`), with the largest gap
   - same rows, different order → try the explicit NULL placement; else `move_note` (`order_ties`)
4. **Recall.** Ask H-MEM which fixes proved similar differences before, and try those first.
5. **Rewrite.** Try each rule, then the drift fix combined with each other rule. Every attempt runs on the target and is compared with the source's original rows. The first identical one wins → `rewrite`, with the cause it proves.
6. **Tie probe.** For ORDER BY … LIMIT queries with equal row counts, run both engines without the LIMIT (same multiset?) and with every output column added to the ORDER BY (same rows, same order?). Both must hold → `move_note` (`tie_at_limit`).
7. **Model.** Ask the Meterless runtime for a rewrite; execute and compare it.
8. **Keep.** Nothing proven → `keep`, with the engine's error or `unexplained`.

## Where the guarantee is enforced

| Guarantee | Module | The test that covers it |
|---|---|---|
| A `move` means the multisets of canonical rows are equal | `compare.py` | `test_any_single_change_is_caught` |
| Float noise is labelled, never hidden as identical | `compare.py`, `judge.py` | `test_float_noise_is_labelled_not_hidden`, `test_single_precision_sums_are_float_noise_not_hidden` |
| Text is compared byte for byte | `canon.py` | `test_trailing_space_is_a_difference`, `test_case_is_a_difference` |
| No query over unaligned input is judged | `judge.py` | `test_blocked_when_input_not_aligned` |
| A rewrite is kept only if its rows equal the source's original rows | `judge.py` | `test_integer_division_is_proven_and_rewritten`, `test_wrong_advisor_rewrite_is_refused` |
| A tie at a LIMIT needs both probes | `judge.py` | `test_tie_at_limit_is_proven_not_assumed` |
| Recalled fixes are re-run, never trusted | `judge.py`, `memory.py` | `test_recalled_fix_is_tried_first`, `test_judge_with_real_memory` |
| Qualifying tables does not change any answer | `workload/__init__.py` | `test_qualified_query_gives_same_rows_as_original_on_real_data` (101 queries) |
| A recording cannot be edited without detection | `ledger.py` | `test_import_rejects_tampered_recording` |

## What source alignment measures

For every table both engines run one statement: `COUNT(*)`, and for each column the non-null count, MIN, MAX, and an exact SUM (numbers cast to `DECIMAL(38, s)` where `s` is the scale the **source** declares) or the summed text length. A DECIMAL(7,2) column on one side and a DOUBLE or REAL column holding the same cents on the other therefore still align. The type change is recorded (`aligned_types`) and handed to the judge, which uses it to name the cause when a query's answer changes.

The tests tamper with a copy in four ways (a deleted row, a value changed by 0.01, a trailing space, a dropped column) and one dropped table. Each one is caught: `tests/test_align.py`.

## Where the model sits

At the very end, and only as a proposer. When every rule has failed, `ModelAdvisor` sends the target SQL and the facts of the difference to the Meterless runtime (row counts, mismatched columns, type changes; never data values) and asks for one statement. The judge runs it on the target and compares it with the source's rows, like any other rewrite. A proposal that is wrong gets refused on the rows (`test_wrong_advisor_rewrite_is_refused`), and an offline runtime does not stop the run (`test_offline_advisor_does_not_break_the_judge`).

## Where each sponsor technology is load-bearing

| Sponsor | What it does in Nirvana | Remove it and… |
|---|---|---|
| **Zetaris** | The target engine. It reads the workload's tables in place through Lightning, and every query, alignment profile and rewrite runs on it through the REST SQL endpoint (`engines/zetaris.py`). | there is no second engine to compare against, so there is no product |
| **Meterless** | H-MEM (the reference engine, vendored unchanged) holds every proven fix with its trust ledger. The provider contract 0.90.0 (URL normalisation, `meterless-app` and `meterless-scope` headers, status by `/v1/models`, auto-detect on 8088/8090/8787/11434) carries the model calls. | fixes are rediscovered on every query, and the model has no route |
| **NVIDIA Nemotron** | The model behind the Meterless runtime that proposes rewrites for differences no rule explains. It runs locally, so no query text leaves the machine. | unexplained differences end in `keep` with no second opinion |
| **Databricks** (the subject, not a sponsor) | The source of truth: its answer is the one every rewrite must reproduce. Free Edition ships `samples.tpcds_sf1`. | |

## The workload

The 103 TPC-DS query texts as Apache Spark renders them (99 templates; 14, 23, 24 and 39 have two parts), copied unchanged from the Apache Spark repository at a pinned commit (`src/nirvana/workload/tpcds/NOTICE.md`). 91 of them sort their output. Databricks Free Edition provides the data as `samples.tpcds_sf1`, and the Zetaris getting-started material supplies a `TPCDS_DB` sample. Nirvana qualifies table names per engine with sqlglot and leaves CTE names and already-qualified names alone. A test runs 101 of the queries before and after qualification on generated data and requires identical rows.

## Engineering decisions & the traps that taught me something

- **Python's default decimal context rounds at 28 digits.** `Decimal(10**30).normalize()` loses digits, so two equal 30-digit integers could tokenize differently. A property test found it. The canonicaliser now normalises in a 1,000-digit context.
- **sqlglot drops a NULLS clause that matches Spark's default.** Writing `nulls_first=True` on an ascending key produces no text at all, so the "explicit NULL order" rewrite was silently a no-op for an engine with another default. It now writes the clause as raw text.
- **`a / b / c` was half rewritten.** The outer division was wrapped first and the inner node was copied, so the inner cast was lost. Rewrites now walk innermost first.
- **`'N/A'` counted as a division.** TPC-DS q41 compares `i_size = 'N/A'`. Division is now detected on the parse tree, not the text.
- **A tie at a LIMIT is easy to over-claim.** One probe (drop the LIMIT, same multiset?) also passes when the engines sort NULLs differently. The judge therefore tries the NULL-order rewrite first, and the tie verdict needs a second probe with a total order.
- **DuckDB's generator spells `s_tax_percentage`.** The TPC-DS spec spells it `s_tax_precentage`. Alignment reports a column missing on one side rather than guessing, and a test pins the spelling.
- **A float32 copy is not automatically "different".** SUMs over REAL columns stayed within 1e-9 of the decimal sums at this scale, while single values did not (1234.56 as REAL is 1234.56005859375). Both behaviours are pinned in tests.

## What's real vs pending — the honesty table

| Claim | State |
|---|---|
| 1,207 tests pass, executing real SQL on DuckDB and the real H-MEM engine | real (`uv run pytest -q`) |
| Full 103-query rehearsal run | real, two local DuckDB engines, labelled rehearsal |
| Databricks client | built and tested against the documented statements API; **not yet run against a live warehouse** |
| Zetaris client | built against the request shape in Zetaris' getting-started repo; the response parser accepts several column-metadata shapes because the exact one is **not yet confirmed on a live instance** |
| Full live run (Databricks Free Edition → Zetaris Lightning) | **pending**: needs both engines running |
| Nemotron through the Meterless contract | contract tested; **no live model call recorded yet** |
| Cost figures | none: Nirvana reports verdicts and timings only; it makes no dollar claim of its own |

## Attack → test

| The abuse | The test that answers it |
|---|---|
| A model proposes SQL that returns the right shape and the wrong rows | `test_wrong_advisor_rewrite_is_refused` |
| The model runtime is down mid-run | `test_offline_advisor_does_not_break_the_judge` |
| One value changes by 1000 in a hundred rows | `test_any_single_change_is_caught` |
| The target silently drops a duplicate row | `test_duplicates_count` |
| The target table lost a row before the run | `test_missing_row_is_caught`, `test_blocked_when_input_not_aligned` |
| A trailing space appears in a text column | `test_trailing_space_is_caught` |
| Someone edits a verdict in a recording | `test_import_rejects_tampered_recording` |
| A memory recalls a fix that does not apply | `test_unknown_recalled_names_are_ignored` |

## Limitations

- Results above 100,000 rows are truncated and marked (`NIRVANA_MAX_ROWS`). TPC-DS answers are far smaller.
- Rows that differ are paired greedily up to 400 per side. Above that, sorted order is used.
- Only one run executes at a time per server.
- The tie probe re-runs the query without its LIMIT. On large tables that costs a full scan.
- Rules cover four causes (type drift, integer division, NULL ordering, dialect). Anything else is left to the model or ends in `keep`.

## Security

- Secrets are read from the environment or `.env.local` (gitignored) and only handed to the HTTP clients. `describe()` and `/api/status` report whether each engine is configured, never the values (`test_databricks_describe_has_no_token`, `test_settings_configured_flags_never_carry_values`).
- The model prompt carries the query text and the shape of the difference, never data values (`test_advisor_prompt_and_proposal`).
- Recorded mode contacts no engine and refuses new runs.

## Tech stack

Python 3.12, FastAPI, httpx, sqlglot 30, DuckDB 2.0, SQLite, pytest and Hypothesis (1,207 tests). Node with tsx runs the vendored Meterless H-MEM reference engine. Zetaris Lightning (REST), the Databricks SQL Statement Execution API, and the Meterless provider contract 0.90.0 with NVIDIA Nemotron.

## Project layout

```
src/nirvana/
  model.py canon.py compare.py      values, tokens, comparison
  align.py judge.py rewrite.py      alignment, verdicts, rewrites
  engines/                          databricks.py zetaris.py duck.py base.py
  workload/                         the 103 TPC-DS texts and qualification
  meterless.py memory.py            provider contract, advisor, H-MEM memory
  ledger.py runner.py api.py cli.py
web/dist/                           the web app (plain HTML, CSS and JS, fonts served locally)
bridges/hmem.ts                     JSON-lines bridge to H-MEM
vendor/meterless/hmem/              H-MEM reference engine, unchanged (UPSTREAM.md)
tests/                              1,207 tests
recordings/                         finished live runs served in recorded mode
```

## Full command reference

```bash
uv run nirvana status                       # SELECT 1 on both engines
uv run nirvana run                          # all 103 queries, live engines, model + memory
uv run nirvana run q3 q42 --no-model        # some queries, rules only
uv run nirvana run --rehearsal              # local DuckDB pair, labelled rehearsal
uv run nirvana export <run-id>              # write recordings/<run-id>.json
uv run nirvana serve --port 8000            # HTTP API (live)
NIRVANA_MODE=recorded uv run nirvana serve  # serve recordings only
```

Configuration lives in `.env.local` (template: `.env.example`).

## How I'd deploy it

The live mode needs both engines reachable, so it runs where Zetaris runs. Recorded mode needs nothing: the `Dockerfile` serves `recordings/` with `NIRVANA_MODE=recorded`, and `render.yaml` deploys it on a free web service.

## Tests

```
uv run pytest -q
1207 passed in 9.38s
```

| File | What it covers |
|---|---|
| `tests/test_canon.py` | type families, tokens, fingerprints, property tests |
| `tests/test_compare.py` | multiset diff, pairing, float noise, property tests |
| `tests/test_workload.py` | all 103 queries: parse, qualify per engine, idempotence, same rows before and after on generated data |
| `tests/test_engines.py` | Databricks and Zetaris wire formats, DuckDB, the rehearsal pair |
| `tests/test_align.py` | alignment of all 24 tables, five kinds of tampering |
| `tests/test_judge.py` | each cause proven on a real engine changed in one documented way |
| `tests/test_rewrite.py` | every rule; the division rewrite on every dividing TPC-DS query |
| `tests/test_meterless.py` | the provider contract, auto-detect, the advisor |
| `tests/test_memory.py` | the real H-MEM engine through the bridge, persistence, judge integration |
| `tests/test_ledger.py` | receipts, recordings, tamper detection |
| `tests/test_api.py` | every route, a rehearsal run end to end, recorded mode |
| `tests/test_config_cli.py` | settings, the command line |
| `tests/test_web.py` | the web app: every route served, assets local, every endpoint it calls exists, every verdict styled |

## License

Apache-2.0 (`LICENSE`). The TPC-DS query texts come from Apache Spark (Apache-2.0) and the H-MEM reference engine from Meterless (Apache-2.0). Each keeps its notice: `src/nirvana/workload/tpcds/NOTICE.md`, `vendor/meterless/UPSTREAM.md`.
