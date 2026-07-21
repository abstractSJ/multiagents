# Research Console

A graphical console for the A-share multi-agent research project. Company research defaults to the **Python Agent Coordinator**: Python owns the plan and dispatches registered agents with explicit input/output contracts; Claude Code runs only as a bounded worker per step. The old full `/rec` main-session mode has been removed. SSE streams step events, validation results, and artifacts in real time.

The API and event contract is documented in [CONTRACT.md](./CONTRACT.md). Event schemas, REST shapes, stable IDs, and `step_id` values are defined there.

## Start the Console

```bash
# Runtime dependencies: Python 3.12, FastAPI, Uvicorn, and Pydantic
python research_console/app.py
# Open http://127.0.0.1:8600
```

- `GET /` returns `static/index.html`. If the static directory is missing, the backend serves an English placeholder page and remains usable through the API.
- Run data is persisted under `research_console/console_workspace/runs/<run_id>/`.
- `meta.json` stores execution mode and `claude_session_id`.
- `events.jsonl` stores authoritative console events; cumulative partial previews are not persisted.
- Python agent workers store diagnostics under `python_agent_coordinator/` inside the run directory.
- Company runs freeze an immutable `decision_snapshot.json` before the terminal event. Reviews are appended to `reviews.jsonl`.
- Metadata and formal research state use atomic replacement. On restart, an existing `run_completed` event repairs stale metadata; only runs with no terminal event are marked failed.

## API Overview

| Method and path | Description |
|---|---|
| `GET /api/health` | Project root, Bocha-key presence, Claude CLI version, active run count, and missing scripts |
| `GET /api/catalog` | Company/fiscal-year artifact catalog and research-state list |
| `POST /api/audit` | Read-only research-state audit; does not overwrite formal `research_state.json` |
| `POST /api/runs` | Creates `{mode, llm_mode?, params}`; validates required parameters and rejects conflicting active company runs with HTTP 409 |
| `GET /api/runs` | Lists all runs |
| `GET /api/runs/{id}` | Returns run metadata, history summary, and all authoritative events |
| `GET /api/runs/{id}/decision` | Reads a frozen decision; legacy company runs are derived in memory until the first review is created |
| `GET /api/runs/{id}/reviews` | Reads appended reviews and returns warnings for malformed JSONL lines |
| `POST /api/runs/{id}/reviews` | Creates a current review with optional manual stock/benchmark prices, falsification status, and notes |
| `GET /api/runs/{id}/events?after=N` | SSE stream; replays events after `N`, then continues live with 15-second heartbeats |
| `POST /api/runs/{id}/cancel` | Cancels a run and cleans up subprocesses before publishing one terminal event |
| `POST /api/runs/{id}/steps/{step_id}/complete` | Manually completes a waiting legacy LLM step; returns missing artifacts when incomplete |
| `POST /api/runs/{id}/steps/{step_id}/skip` | Skips only legacy steps with an active skip-consumption window |
| `GET /api/artifact?path=...` | Safely reads allowlisted JSON, JSONL, Markdown, text, or PDF metadata |

Artifact access is restricted to project workspaces: collector, processor, analyst, valuation, market context, industry collection, orchestrator, and console workspaces. Paths outside the allowlist return HTTP 403.

## Package Layout

```text
research_console/
├── config.py          # Paths, workspaces, allowlists, port, timeouts, and feature flags
├── state_reader.py    # Audit execution, research-state parsing, catalog, artifact reading, valuation extraction
├── history.py         # Decision snapshots, legacy derivation, reviews, local prices, descriptive metrics
├── steps.py           # Stable step definitions, plans, command builders, and English LLM prompts
├── engine.py          # Run/EventBus execution, progress, backflow, coordinator/legacy modes, demo, replay
├── worker_runtime.py  # Bounded Claude Code worker process boundary
├── agent_runtime.py   # Python-owned agent registry + I/O contracts
├── company_research_coordinator.py  # Company research main-session scheduler
├── static/            # Zero-build English HTML/CSS/JavaScript frontend
└── app.py             # FastAPI routes, SSE, static mounting, and Uvicorn entry point
```

## Worker and Agent Runtime

`worker_runtime.py` runs one constrained Claude Code process and validates tools, paths, and outputs. `agent_runtime.py` registers production agents (`formal_financial_analyst`, `company_valuation_analyst`) with explicit I/O slots. The company console uses `company_research_coordinator.py` on top of both.


## Company Research Coordinator

`company_research_coordinator.py` is the company main-session scheduler:

1. Run `audit_company_research_state`
2. Build the plan with `steps.build_company_plan` (reuse vs pending)
3. Execute deterministic layers via existing project scripts (collector, processor, financial draft, market context)
4. Execute LLM layers by **Python-selected** agents:
   - `formal_financial_analyst` → formal financial package
   - `company_valuation_analyst` → valuation four-file package
5. Refresh `research_state` and write a coordinator report

Claude Code only executes the worker task Python compiled for the selected agent_id.

```bash
python tools/run_company_research_coordinator.py ^
  --stock-code 601138 ^
  --as-of-date 2026-05-01 ^
  --claude-bin "C:/Users/1/.local/bin/claude.exe" ^
  --workspace "d:/desk/multiagents/tmp_company_coord_601138" ^
  --keep --timeout 1800
```

Flags: `--scripts-only`, `--llm-only`, `--force-refresh`, `--no-market-context`.

## Execution Modes

### Default company mode: `python_agent_coordinator`

- Python runs `audit_company_research_state`, builds `steps.build_company_plan`, then executes pending deterministic scripts and registered LLM agents (`formal_financial_analyst`, `company_valuation_analyst`).
- Each step publishes console events with explicit `input_paths` / `output_paths`, tool names, and validation checks — not a black-box main session.
- Claude Code is only the worker runtime for the current agent_id; it cannot reorder the plan or pick another agent.
- CLI equivalent: `tools/run_company_research_coordinator.py`.


### Legacy fallback

The deterministic DAG remains available:

- `manual`: emits `step_waiting_llm` and polls expected artifacts.
- `claude_cli`: launches a separate `claude -p` call for each LLM step.
- `skip`: skips LLM steps and downgrades delivery.

Industry runs use the legacy modes. Demo and replay do not execute the real research workflow.

## Legacy Company Workflow

- The initial audit produces schema 2.0 `research_state`. Company runs default to `recent_history`, selecting the latest two eligible annual reports plus cutoff-eligible current/prior-year interim filings. Explicit filing filters preserve `single_filing` behavior.
- Collection expands the filing plan into stock-specific annual/Q1/semiannual/Q3 disclosure windows. Parse, digest, and RAG remain fixed UI steps but batch only the missing filing entries.
- Summary comparison is required only for annual filings with a local summary PDF; interim filings are marked `not_applicable`.
- Formal financial analysis consumes `filing_set.json`, and valuation reuse requires the same `financial_input_fingerprint`. Reusable layers are skipped; only missing, partial, stale, or incompatible work executes.
- Every deterministic step refreshes research state and emits `state_refreshed`.
- Market-context collection runs in parallel with the main line. Without a Bocha key, the step uses `--dry-run` and returns a limited delivery without exposing credentials.
- Backflow notices cover incomplete digests, evidence-verification rates below 60%, blocking evidence requests, and valuation `upstream_request.json` files.
- Valuation monitors both current and legacy workspace layouts and completes when either contains the four-file package.
- Delivery extracts a tolerant conclusion summary from valuation, formal financial analysis, and market context. English and Chinese legacy field aliases remain readable, while new narrative output is English.

## Decision History and Current Review

- New company runs freeze a deep copy of the terminal summary into `decision_snapshot.json` before `run_completed`.
- Legacy `GET decision` calls derive the snapshot without writing it. The first `POST reviews` materializes the snapshot atomically.
- Local Tencent and Eastmoney prices are resolved to the latest valid trading date no later than the requested date. Auditable manual prices can fill missing observations.
- Review metrics include elapsed days, share-price change, four valuation buckets, distance to bear/base/bull points, optional benchmark performance, and excess return.
- Reviews preserve `unknown`, `held`, or `breached` falsification status. The UI explicitly states that share-price change is not TSR and that the comparison does not establish causality.

## Demo and Replay

- `demo` plays a scripted English event sequence without network access, subprocesses, or real workspace mutation. Its conclusion is explicitly labeled as demo data.
- `replay` scans existing workspace artifacts, orders events by file modification time, and emits a completed historical timeline.

## Failure and Degradation

- Fatal collection, parsing, digest, RAG, or evidence-draft failures produce a failed run.
- Summary-comparison failure is a limited completion because summary PDFs are optional.
- Market-context failure does not block valuation. Missing or skipped formal financial analysis causes valuation to skip.
- Any failed, skipped-at-runtime, or limited step downgrades the run to partial while preserving usable conclusions and explicit gaps.
- Cancellation waits for child tasks and subprocess cleanup before publishing the single terminal event.
- A workspace lease serializes runs for the same normalized company target and prevents concurrent writes to formal workspaces.

## Current Boundary

This phase provides one complete `/rec` coordinator session and stream-json visualization. It does not yet implement a separate dynamic `research_requests` schema, request cards, deduplication, or blocking/non-blocking request state machine.

## Tests

```bash
python -m pytest tests/test_research_console.py -q
python -m pytest -q
```

The suite covers plan construction, company research coordinator routing, worker/agent contracts, valuation extraction, decision snapshots, APIs, and frontend contracts. Unit tests do not require network access or a real Claude invocation.
