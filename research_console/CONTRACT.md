# Research Console Interface Contract

This document is the single source of truth for the `research_console/` interface. Backend and frontend implementations may evolve independently, but stable REST shapes, SSE event fields, run modes, owner IDs, and `step_id` values must remain compatible.

New optional fields may be added. Existing frozen fields and identifiers must not be removed or renamed without a contract version change.

All newly generated human-readable text is English. Stable wire identifiers remain unchanged. Chinese proper nouns, filing titles, quotations, and search queries may appear only as source data.

---

## 0. Architecture

```text
Browser (static/index.html + app.js + style.css)
   │ REST + SSE
FastAPI (research_console/app.py, default 127.0.0.1:8600)
   │ Python agent coordinator (default company mode)
   │   audit + project scripts + registered Claude Code workers
   │ or legacy deterministic Python DAG (manual/claude_cli/skip)
   │
Collector / processor / analyst / valuation / market-context workspaces
+ industry collector + orchestrator research_state
```

The active frontend is a zero-build native HTML/CSS/JavaScript page with an inline SVG agent room. `game.js` and `sprites.js` are legacy dormant Canvas modules and are not loaded by the active page.

## 1. Run Modes

| `mode` | Behavior |
|---|---|
| `company` | Company research workflow via Python agent coordinator |
| `industry` | Industry research workflow and simplified graphical `/rei` entry point |
| `demo` | Offline scripted English event sequence; no network or credentials required |
| `replay` | Synthesizes a historical event sequence from existing artifact modification times |

## 2. LLM Execution Modes

| `llm_mode` | Behavior |
|---|---|
| `python_agent_coordinator` | **Default for company runs.** Python owns the plan and dispatches registered agents with explicit input/output contracts; Claude Code is a bounded worker per step |
| `manual` | Legacy DAG emits `step_waiting_llm` with English instructions and polls expected artifacts |
| `claude_cli` | Legacy DAG launches one `claude -p` process for each LLM step |
| `skip` | Legacy DAG skips LLM steps and downgrades delivery |

`python_agent_coordinator` supports company runs only. Industry runs use the legacy modes. The old Claude Code main-session mode (`coordinator_cli` / full `/rec`) has been removed.

### 2.1 Python Agent Coordinator Execution Order

1. Publish `run_started` with `execution_mode=python_agent_coordinator`.
2. Run `audit_company_research_state` and publish `plan_ready` (authoritative plan, not display-only).
3. For each pending deterministic step, run the original project script and publish `step_started` / `step_completed` (or `step_skipped` / `step_failed`) with command and artifact paths.
4. For each pending LLM step, Python selects a registered `agent_id`, binds input/output paths, runs a bounded Claude Code worker, and publishes validation checks, tool names, and artifacts.
5. Refresh `research_state`, freeze the decision snapshot, and publish `run_completed`.


## 3. Frozen Company Steps

| `step_id` | owner | kind | Completion rule |
|---|---|---|---|
| `audit` | `orchestrator` | script | Runs the company research-state audit and returns JSON |
| `collector_fetch` | `information-collector` | script | Downloads the official filing PDF for the derived disclosure window |
| `processor_parse` | `information-processor` | script | Completes when `content.json` exists |
| `processor_digest` | `information-processor` | script | Runs prepare → auto-digest → merge and checks `digest_audit.json` |
| `processor_rag` | `information-processor` | script | Completes when `rag_chunks.jsonl` and `rag_index_meta.json` exist |
| `processor_compare` | `information-processor` | script | Produces `summary_comparison.json` only for annual filings with summary PDFs; interim filings are `not_applicable` |
| `financial_evidence_draft` | `financial-analyst` | script | Produces the evidence draft and checks evidence coverage and blocking requests |
| `formal_financial_analysis` | `financial-analyst` | llm | Produces `formal_financial_analysis.json` and `.md` in English |
| `market_context_update` | `market-context-collector` | script | Produces the market-context package; runs in parallel after audit |
| `valuation_update` | `valuation-analyst` | llm | Produces valuation report, evidence table, and audit in the current or legacy layout |
| `final_audit` | `orchestrator` | script | Refreshes final layer status |
| `deliver` | `orchestrator` | synthetic | Builds `run_completed.payload.summary` |

Main dependency chain:

```text
audit → collector_fetch → processor_parse → processor_digest → processor_rag
→ processor_compare → financial_evidence_draft → formal_financial_analysis
→ valuation_update → final_audit → deliver
```

`market_context_update` begins after audit and joins before valuation. Reusable layers are represented as skipped; only missing, partial, stale, or incompatible layers execute unless `force_refresh=true`.

## 4. Frozen Industry Steps

| `step_id` | owner | kind | Completion rule |
|---|---|---|---|
| `industry_collect` | `industry-info-collector` | script | Runs `run_industry_collection.py` in company-validation or industry-theme mode |
| `industry_validate` | `industry-info-collector` | script | Validates the industry package |
| `industry_research` | `industry-researcher` | llm | Produces `industry_research_view.json` in English |
| `industry_deliver` | `orchestrator` | synthetic | Summarizes package quality and validation into the conclusion card |

## 5. SSE Event Schema

Each SSE `data:` frame is a JSON object:

```json
{
  "seq": 42,
  "ts": "2026-07-13T12:00:00+08:00",
  "run_id": "r_xxx",
  "type": "...",
  "step_id": "...",
  "owner": "...",
  "payload": {}
}
```

- `seq` is a positive integer that increases monotonically within a run.
- Reconnection uses `?after=<seq>`.
- `step_id` and `owner` appear only when applicable.
- Heartbeats are SSE comment lines `: ping` and do not consume a sequence number.

### 5.1 Frozen Event Types

| `type` | Frozen payload fields |
|---|---|
| `run_started` | `{mode, params, llm_mode, execution_mode?}` |
| `plan_ready` | `{steps:[{step_id,owner,kind,title,status,skip_reason?,layer?}], research_state_path?, layer_statuses?, reusable?, next_actions?, display_only?, trace_mode?, milestone_states?}` |
| `coordinator_session_started` | historical/demo only; not emitted by python_agent_coordinator |
| `coordinator_message` | `{text, partial?, warning?, stream?, agent_name?, tool_use_id?, compat_for?}` |
| `work_item_upsert` | `{work_item_id, title?, description?, active_form?, status, blocked_by?, owner?}` |
| `agent_started` | `{agent_name, description?, invocation_id, tool_use_id?, runtime_task_id?, parent_invocation_id?, work_item_id?}` |
| `agent_completed` | `{agent_name, invocation_id, tool_use_id?, runtime_task_id?, work_item_id?, is_error?, status?, summary?}` |
| `target_resolved` | `{stock_code, company_name, report_year, report_type}` |
| `tool_activity` | `{phase:"started"|"completed"|"observed", tool_name, tool_use_id?, invocation_id?, runtime_task_id?, work_item_id?, agent_name?, inferred?, status?, is_error?, summary?}` |
| `handoff` | `{kind:"delegation"|"delivery"|"final_delivery", from_owner?, to_owner?, from_station?, to_station?, invocation_id?, work_item_id?, description?, label?, summary?, is_error?}` |
| `step_started` | `{cmd?}` |
| `step_log` | `{line}` |
| `step_progress` | `{done, total, unit, detail?}` |
| `artifact_created` | `{path, name, kind:"json"|"md"|"jsonl"|"pdf"|"other"}` |
| `step_waiting_llm` | `{instructions, prompt, expected_artifacts:[...], claude_cmd?}` |
| `step_completed` | `{summary?, artifacts?:[...], degraded?}` |
| `step_failed` | `{error, exit_code?}` |
| `step_skipped` | `{reason}` |
| `backflow` | `{from_step, to_owner, reason}` |
| `state_refreshed` | `{layer_statuses, reusable, next_actions?, milestone_states?}` |
| `run_completed` | `{status:"completed"|"partial"|"failed"|"cancelled", summary?}` |
| `run_error` | `{error}` |

`run_error` is diagnostic and does not terminate the stream. Every run terminates with exactly one `run_completed` event.

A `coordinator_message` with `partial=true` is a replaceable live preview. It is visible over SSE but not persisted to authoritative `events.jsonl`; the final complete assistant message is persisted.

`target_resolved` is emitted only after the information collector returns a validated machine-readable identity. The backend must apply it to the persisted run parameters before subsequent observer polls and the final audit, so all later state scans and the decision snapshot use the canonical stock-code workspace.

When `trace_mode="runtime"`, the frontend displays both:

- `milestone_states`: whether formal artifacts are ready.
- `work_item_upsert`, `agent_*`, `tool_activity`, and `handoff`: the real execution order.

The Task Route is owned exclusively by `plan_ready.payload.steps`. Its milestone membership and order remain fixed for the lifetime of the run. Later `milestone_states` snapshots and `step_*` events may update status and descriptive details only for `step_id` values already declared by `plan_ready`; they must not add, remove, replace, or reorder route milestones.

Runtime activity is owned by `work_item_upsert`, `agent_*`, `tool_activity`, and `handoff`. These dynamic records continue to drive `taskItems()`, the Agent Room, the status rail, and the event log, but they must not replace or reshape the fixed Task Route.

The frontend must not infer actual coordinator execution order from owner IDs or the frozen step list.

### 5.2 Company Research State

New company audits emit schema `2.0` while preserving the legacy top-level `target`, `layers`, `reusable`, and `next_actions` fields. The additional authoritative fields are:

```json
{
  "filing_policy": "recent_history|single_filing",
  "filing_plan": [{"report_type":"q1","report_year":"2026","disclosure_start":"...","disclosure_end":"...","required":true}],
  "filings": [{"filing_id":"...","role":"...","report_type":"q1","report_year":"2026","collector":{"status":"ready"},"processor":{"status":"ready"},"summary_comparison":"not_applicable"}],
  "financial_input_fingerprint": "sha256"
}
```

`recent_history` is the default when no filing type/year is pinned. Aggregate collector/processor readiness means every required filing entry is ready. Fixed route step IDs remain unchanged; deterministic steps batch the filing entries internally. Formal financial analysis and valuation are reusable only when their recorded financial-input fingerprint matches the current state.

### 5.3 Company Summary

`run_completed.payload.summary` may contain:

```json
{
  "company_name": "...",
  "stock_code": "...",
  "report_year": "...",
  "as_of_date": "YYYY-MM-DD",
  "valuation_view": "undervalued|fair|overvalued|watch_only|unknown",
  "valuation_view_raw": "...",
  "one_line_conclusion": "...",
  "current_price": 0,
  "market_cap": 0,
  "price_source": "...",
  "price_observation": {
    "status": "available|unavailable",
    "observation_date": "YYYY-MM-DD|null",
    "price": 0,
    "source": "..."
  },
  "price_basis": "...",
  "cutoff_status": "at_cutoff|before_cutoff|after_cutoff|unknown",
  "fair_value": {"bear": 0, "base": 0, "bull": 0, "unit": "CNY/share"},
  "upside_downside": {"bear": -0.1, "base": 0.05, "bull": 0.2},
  "key_assumptions": ["..."],
  "valuation_falsifiers": ["..."],
  "market_context": {"status": "...", "source_count": 0, "tier_counts": {"S":0,"A":0,"B":0,"C":0}, "max_confidence": "..."},
  "layer_statuses": {"collector": "ready"},
  "artifact_paths": {"valuation_report_md": "...", "formal_financial_analysis_md": "...", "market_context_package_md": "..."},
  "confidence": "high|medium|low",
  "gaps": ["..."]
}
```

## 6. REST API

| Method and path | Request | Response |
|---|---|---|
| `GET /api/health` | — | `{ok, project_root, bocha_key_present, claude_cli_version, active_runs, missing_scripts}` |
| `GET /api/catalog` | — | `{companies:[...], states:[...]}` |
| `POST /api/audit` | company audit fields | Full read-only research state plus `state_path` |
| `POST /api/runs` | `{mode, llm_mode?, params}` | `{run_id}`; invalid input returns 400; active target conflict returns 409 with `existing_run_id` |
| `GET /api/runs` | — | `{runs:[...]}` |
| `GET /api/runs/{id}` | — | Run metadata, history summary, and authoritative events |
| `GET /api/runs/{id}/decision` | — | Frozen or derived decision snapshot; GET never materializes a legacy snapshot |
| `GET /api/runs/{id}/reviews` | — | `{status:"available", run_id, reviews, warnings}` |
| `POST /api/runs/{id}/reviews` | review fields | Validates all fields before materializing a snapshot; returns 201 on success |
| `GET /api/runs/{id}/events?after=N` | — | SSE history replay followed by live events |
| `POST /api/runs/{id}/cancel` | — | `{ok}` |
| `POST /api/runs/{id}/steps/{step_id}/complete` | `{force?:bool}` | `{ok}` or 409 with `{missing:[...]}` |
| `POST /api/runs/{id}/steps/{step_id}/skip` | — | `{ok}` only for skippable active legacy steps |
| `GET /api/artifact?path=...` | — | Allowlisted artifact metadata and content |
| `GET /` | — | `static/index.html` |

Artifact paths must remain under an allowlisted project workspace. Paths outside those roots return 403.

## 7. Owner IDs and Active Visual Mapping

| owner | Display name | Active station |
|---|---|---|
| `orchestrator` | Coordinator | Command desk |
| `information-collector` | Information Collector | Collection station |
| `information-processor` | Information Processor | Processing station |
| `financial-analyst` | Financial Analyst | Financial analysis station |
| `valuation-analyst` | Valuation Analyst | Valuation station |
| `market-context-collector` | Market Context Collector | Market-context station |
| `industry-info-collector` | Industry Information Collector | Industry-data station |
| `industry-researcher` | Industry Researcher | Industry-research station |

Status is always communicated by icon and text as well as color. Stable status values include `idle`, `pending`, `running`, `waiting`, `ready`, `done`, `completed`, `skipped`, `partial`, `degraded`, `stale`, `missing`, `incompatible`, `blocked`, `failed`, and `cancelled`.

## 8. Decision Snapshot and Review Contract

### 8.1 Decision snapshot

Before the single terminal event, a completed company run freezes:

`console_workspace/runs/<run_id>/decision_snapshot.json`

```json
{
  "schema_version": "1.0",
  "artifact_type": "company_decision_snapshot",
  "run_id": "r_xxx",
  "frozen_at": "ISO-8601",
  "knowledge_cutoff": "YYYY-MM-DD",
  "target": {"company_name":"...","stock_code":"000001","report_year":"2025","report_type":"annual","as_of_date":"YYYY-MM-DD"},
  "decision": {},
  "source_artifacts": {
    "valuation_report_md": {"path":"...","status":"available|unavailable","size":0,"mtime_ns":0,"sha256":"..."}
  }
}
```

Freezing uses a complete same-directory temporary file, `fsync`, and no-replace atomic publication. Existing snapshots are immutable. Freezing failure downgrades a completed delivery to partial without publishing a second terminal event.

### 8.2 Review JSONL

Reviews append one JSON object per line to `reviews.jsonl`. Reviews are not research-run events and do not consume SSE sequence numbers.

```json
{
  "schema_version": "1.0",
  "artifact_type": "company_decision_review",
  "review_id": "rv_xxx",
  "run_id": "r_xxx",
  "created_at": "ISO-8601",
  "review_date": "YYYY-MM-DD",
  "knowledge_cutoff": "YYYY-MM-DD",
  "target": {"stock_code":"000001"},
  "benchmark_code": "000300|null",
  "falsification_status": "unknown|held|breached",
  "falsification_notes": "...",
  "note": "...",
  "prices": {
    "stock": {"status":"available|partial|unavailable","same_source":true,"baseline":{},"current":{},"basis_warnings":[]},
    "benchmark": {"status":"available|partial|unavailable","baseline":{},"current":{},"basis_warnings":[]}
  },
  "metrics": {
    "elapsed_days": 0,
    "spot_price_change": 0.0,
    "valuation_bucket": {
      "status":"available|unavailable",
      "bucket":"below_bear|bear_to_base|base_to_bull|above_bull|unavailable",
      "fair_value_points":{"bear":0,"base":0,"bull":0},
      "distances_to_points":{}
    },
    "benchmark_change": 0.0,
    "excess_return": 0.0
  },
  "limitations": ["Share price change is not TSR.", "The comparison does not establish causality."]
}
```

Local price resolution supports Tencent `qfqday` and Eastmoney `TRADE_DATE/CLOSE_PRICE`. Each endpoint selects the nearest valid trading date no later than the request date. Non-positive and non-finite prices are invalid. Same-provider baseline/current pairs are preferred. Auditable manual values may fill missing observations.

The valuation bucket is a four-range classification, not nearest-point matching. Missing, non-positive, or non-monotonic bear/base/bull values make the bucket unavailable.

## 9. Persistence

Each run directory may contain:

- `meta.json`: mode, parameters, status, timestamps, LLM mode, execution mode, session ID, and coordinator PID.
- `events.jsonl`: authoritative console events.
- `claude_events.jsonl`: raw coordinator stdout NDJSON.
- `decision_snapshot.json`: immutable company decision snapshot.
- `reviews.jsonl`: append-only current reviews.

Event sequence numbers continue from the maximum valid historical sequence. Gaps are allowed; duplicates are not. A recovered run with an existing terminal event uses that event to repair metadata. Only interrupted runs with no terminal event receive a recovered failed terminal state.

A workspace lease serializes active runs for the same normalized company target. Terminal publication waits for all tracked child tasks and subprocesses to stop.

## 10. Encoding and Platform Constraints

- Subprocess environments include `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8`.
- Stdout is decoded as UTF-8 with replacement for malformed bytes.
- Windows paths use `pathlib.Path` and normalized comparisons.
- The default port is 8600.
- The active frontend is zero-build native ES modules with no CDN dependencies.
- `prefers-reduced-motion` disables non-essential animation while preserving state changes.
