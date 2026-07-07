# Company Research Reuse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a company research state audit layer so repeated `/rec` stock research reuses existing artifacts and only runs missing, stale, or incompatible steps.

**Architecture:** Add a read-only Python audit script that scans collector, processor, financial analyst, and valuation workspaces and emits a structured `research_state`. Update orchestration docs and agent prompts so `/rec` and `/re` must call this audit first and dispatch only the `next_actions` it returns.

**Tech Stack:** Python 3 stdlib (`argparse`, `dataclasses`, `json`, `pathlib`, `unittest`), Markdown skill/agent contracts.

---

### Task 1: Add company research state auditor

**Files:**
- Create: `research_orchestrator_scripts/audit_company_research_state.py`
- Test: `tests/test_audit_company_research_state.py`

- [ ] **Step 1: Write unit tests for reuse, partial processor, stale valuation, and incompatible formal analysis**

Use temporary workspaces to create minimal manifest and artifact files, then assert the auditor emits expected statuses, skipped actions, and next actions.

- [ ] **Step 2: Implement the auditor data model and helper functions**

Create `ResearchAuditRequest`, JSON helpers, target matching, focus/depth compatibility, and path serialization helpers.

- [ ] **Step 3: Implement artifact layer audits**

Implement collector, processor, financial evidence draft, formal financial analysis, and valuation scans. Each layer returns status, artifacts, gaps, and compatibility metadata.

- [ ] **Step 4: Implement next action planning and CLI output**

Combine layer states into `reusable`, `skipped_actions`, `next_actions`, and optional `research_state.json` output under `research_orchestrator_scripts/orchestrator_workspace/company_state/<stock_code>/<report_year>/`.

- [ ] **Step 5: Run unit tests**

Run: `python -m unittest discover -s tests -p 'test_audit_company_research_state.py' -v`
Expected: all tests pass.

### Task 2: Update orchestration contracts

**Files:**
- Modify: `AGENTS.md`
- Modify: `.agents/skills/rec/SKILL.md`
- Modify: `.agents/skills/re/SKILL.md`
- Modify: `.claude/skills/rec/SKILL.md`
- Modify: `.claude/skills/re/SKILL.md`

- [ ] **Step 1: Update project rules**

Add a company research state audit hard gate before any company chain rerun. Document that `force_refresh=true` is required to intentionally rerun reusable layers.

- [ ] **Step 2: Update `/rec`**

Add the audit command as step 0, define how to interpret `ready`, `partial`, `missing`, `stale`, and `incompatible`, and require final output to list reused, skipped, and newly generated artifacts.

- [ ] **Step 3: Update `/re`**

When routing company research to `/rec`, require passing the audit state and preserving `/rec` reuse semantics.

### Task 3: Update role contracts

**Files:**
- Modify: `.claude/agents/information-processor.md`
- Modify: `.claude/agents/financial-analyst.md`
- Modify: `.claude/agents/valuation-analyst.md`

- [ ] **Step 1: Update information processor rules**

Require using `research_state` and only filling missing processor sub-artifacts.

- [ ] **Step 2: Update financial analyst rules**

Allow writing standard `formal_financial_analysis.json/md`, reuse compatible formal analysis, and perform focus/depth supplements without rerunning upstream evidence steps.

- [ ] **Step 3: Update valuation analyst rules**

Reuse same-date valuation reports, treat older valuations as stale reference, and update valuation only without requesting upstream reruns unless financial inputs changed.

### Task 4: Verify on real workspace

**Files:**
- No source edits expected.

- [ ] **Step 1: Run auditor against an existing company**

Run: `python research_orchestrator_scripts/audit_company_research_state.py --stock-code 600519 --report-year 2025 --report-type annual --depth standard --as-of-date 2026-07-08 --write-state`
Expected: JSON output identifies existing collector/processor/financial artifacts and gives only necessary missing/stale actions.

- [ ] **Step 2: Check git diff**

Run: `git diff --stat`
Expected: only planned script, tests, docs, skills, and agent contracts changed.

## Self-review

- Spec coverage: tasks cover auditor creation, `/rec` and `/re` hard gate, role contract updates, state output, and verification.
- Placeholder scan: no implementation placeholder remains; code details are implemented directly in source files during execution.
- Type consistency: the auditor emits `layers`, `reusable`, `skipped_actions`, and `next_actions`; all docs refer to those same names.
