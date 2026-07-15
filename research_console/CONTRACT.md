# research_console 接口契约（单一事实源）

本文件是 `research_console/`（FastAPI 后端 + Canvas 游戏化前端）的**唯一接口契约**。
后端与前端各自实现，但事件 schema、REST API、步骤定义必须与本文件逐字一致。
字段名一旦写入本文件即冻结；实现中新增字段允许，但删改本文件字段不允许。

---

## 0. 总体架构

```
浏览器 (static/: index.html + app.js + game.js + sprites.js + style.css)
   │  REST + SSE
FastAPI (research_console/app.py, 默认 127.0.0.1:8600)
   │  Claude Code stream-json / asyncio subprocess / 工作区 audit
完整 /rec 主协调会话（company 默认）或 legacy Python 静态 DAG
   │
五大工作区（collector / processor / analyst / valuation / market_context）+ orchestrator research_state
```

核心思想：公司研究默认由**一个持续存在的完整 `/rec` Claude Code 主会话**负责真实调度，
后端只把 stream-json 与周期性 workspace audit 映射为 SSE 事件。原静态 DAG 继续作为
manual / 分步 claude_cli / skip 的 legacy 路径，并保留 demo / replay。

## 1. 运行模式（run mode）

| mode | 说明 |
|---|---|
| `company` | 公司研究链路（/rec 的图形化替代） |
| `industry` | 行业研究链路（/rei 的图形化替代，简化版） |
| `demo` | 演示模式：后端播放一段脚本化事件序列（覆盖全部事件类型，含回流与庆祝），无需网络/API key |
| `replay` | 回放模式：从既有工作区产物合成事件序列（按文件 mtime 排序），前端按倍速播放 |

## 2. LLM 步骤执行模式（llm_mode，随 run 提交）

| llm_mode | 行为 |
|---|---|
| `coordinator_cli`（company 默认） | 每个 run 只 spawn 一次完整 `/rec`：`claude -p <prompt> --output-format stream-json --verbose --include-partial-messages --permission-mode auto`；不使用 `--bare`。主会话按 research_state 调度 custom agents，控制台读取实时事件并周期性 audit 工作区 |
| `manual`（legacy） | 静态 DAG 的 LLM 节骤发出 `step_waiting_llm`（含提示词 + 期望产物路径），后端轮询产物 |
| `claude_cli`（legacy） | 静态 DAG 为每个 LLM 步骤分别 spawn `claude -p ... --permission-mode acceptEdits`，完成仍以期望产物落盘为准 |
| `skip`（legacy） | 静态 DAG 的 LLM 步骤直接 skipped，流水线继续（交付降级） |

`coordinator_cli` 仅支持 `company`；行业链仍使用 legacy 三种模式。固定 company steps 在
coordinator 模式只用于初始化层状态与角色舞台，不控制真实 Agent 调度顺序。

### 2.1 coordinator_cli 公司执行顺序

1. 发布 `run_started`，执行一次初始 audit；
2. 发布仅供展示的 `plan_ready`；
3. 启动一个完整 `/rec` stream-json 进程，同时周期性执行**只读** audit 观察工作区；
4. stream 中的主协调器文本、Agent 启停与新产物实时映射为 SSE；
5. Claude 进程退出后停止观察器，执行 final audit；
6. 复用既有 deliver 结论卡提取，按 CLI result、permission denial 与终局层状态生成 `run_completed`。

阶段一只保存 `session_id`，不在服务重启后自动 resume，也没有独立的动态研究请求协议或请求状态机。

## 3. 公司链路步骤定义（step_id 冻结）

| step_id | owner | kind | 命令/判定 |
|---|---|---|---|
| `audit` | `orchestrator` | script | `python research_orchestrator_scripts/audit_company_research_state.py --stock-code <c>（或 --target）--report-year <y> --report-type <t> --depth <d> [--focus f] --as-of-date <date> --write-state [--force-refresh]`；stdout 即 research_state JSON |
| `collector_fetch` | `information-collector` | script | `python info_collector_scripts/run_cninfo_collection.py --start-date <S> --end-date <E> --report-types <t> --keyword <code> --download`；披露窗口由财年推导：annual FYy → (y+1)-01-01 .. min(today,(y+1)-08-31)；q1 y → y-04-01..y-08-31；semiannual y → y-07-01..y-12-31；q3 y → y-10-01..y-12-31 |
| `processor_parse` | `information-processor` | script | `python info_processor_scripts/run_pdf_processing.py --stock-code <c> --report-type <t> --report-year <y>`；完成信号 = report_dir/content.json 存在 |
| `processor_digest` | `information-processor` | script | 三连：`build_llm_digest.py prepare --content-json <cj>` → `auto-digest --pipeline-dir <dp>` → `merge --pipeline-dir <dp> [--allow-partial]`；进度 = digest_pipeline/agent_results/*.digest.json 数量 / chunk_manifest.json chunks 数量（`step_progress`）；merge 后读 digest_audit.json，complete=false 则发 `backflow`（to_owner=information-processor，仅提示不自动重跑），后续 draft 加 `--allow-incomplete-digest` |
| `processor_rag` | `information-processor` | script | `build_report_rag_index.py build --content-json <cj>`；完成信号 = rag_index/rag_chunks.jsonl + rag_index_meta.json |
| `processor_compare` | `information-processor` | script | `compare_digest_with_summary.py --content-json <cj>`；完成信号 = summary_comparison.json；若摘要 PDF 缺失允许失败降级为 warning（step_completed + payload.degraded=true） |
| `financial_evidence_draft` | `financial-analyst` | script | `python financial_analyst_scripts/run_financial_analysis.py --report-dir <rd> --analysis-depth <d> [--focus f] [--allow-incomplete-digest]`；完成后读 evidence_check.json：verified/checked < 0.6 或 analyst_audit.upstream_requests_blocking>0 → 发 `backflow`（to_owner=information-processor） |
| `formal_financial_analysis` | `financial-analyst` | llm | 期望产物：`<analyst_report_dir>/formal_financial_analysis.json` + `.md` |
| `market_context_update` | `market-context-collector` | script | `python market_context_collector_scripts/run_market_context_collection.py --target <t> --stock-code <c> --company-name <n> --as-of-date <date> --depth <d> [--focus f] --freshness <fr>`；无 API key（env 或 collector_workspace/local_config.json 均无）时自动加 `--dry-run` 并在 payload 标记 degraded；进度 = collector_workspace/cache/queries/<as_of_date>/ 新增文件数（近似）；**与主线并行**（audit 后即可启动） |
| `valuation_update` | `valuation-analyst` | llm | 期望产物：`valuation_analyst_scripts/valuation_workspace/reports/<code>/<as_of_date>/` 下 valuation_report.json/.md + valuation_evidence_table.json + valuation_audit.json（兼容旧布局 `valuation_workspace/<code>/<date>/`，两处都监视）；若出现 upstream_request.json → 发 `backflow`（owners 取自其 JSON）；**前置依赖：formal_financial_analysis 完成 且 market_context_update 结束（完成/跳过/失败均可）** |
| `final_audit` | `orchestrator` | script | 同 `audit`；完成后发 `state_refreshed` |
| `deliver` | `orchestrator` | synthetic | 从 valuation_report.json / formal_financial_analysis.json / market_context_package.json 提取结论卡数据，写入 `run_completed.payload.summary` |

依赖图：`audit → collector_fetch → processor_parse → processor_digest → processor_rag → processor_compare → financial_evidence_draft → formal_financial_analysis → valuation_update → final_audit → deliver`；
`market_context_update` 从 audit 后并行，汇入 `valuation_update`。
processor_rag 只依赖 content.json，可与 processor_digest 并行（实现允许并行或顺序执行，事件顺序不作保证）。

**复用规则（与 audit 的 research_state 一致）**：`plan_ready` 时，凡 research_state 判定
`reusable=true` 的层，其对应步骤直接标记 `skipped`（skip_reason=复用已有产物）；
只有 missing/partial/stale/incompatible 的层进入待执行。`force_refresh=true` 时全部执行。
层→步骤映射：collector→collector_fetch；processor→processor_parse/digest/rag/compare
（按 quality_flags.missing_required_artifacts 与 next_actions 细化到子步骤）；
financial_evidence_draft→financial_evidence_draft；formal_financial_analysis→formal_financial_analysis；
valuation→valuation_update；market_context→market_context_update。

## 4. 行业链路步骤定义

| step_id | owner | kind | 说明 |
|---|---|---|---|
| `industry_collect` | `industry-info-collector` | script | `python industry_info_collector_scripts/run_industry_collection.py`，公司验证模式（--stock-code/--company-name/--fiscal-year）或主题模式（--target/--industry-name/--deliverable-type theme_event_study + 事件参数），均带 --as-of-date |
| `industry_validate` | `industry-info-collector` | script | `python industry_info_collector_scripts/validate_industry_package.py --package <pkg.json> [--deliverable-type theme_event_study]` |
| `industry_research` | `industry-researcher` | llm | 期望产物：包目录下 `industry_research_view.json`（约定新文件）；manual 模式下也可由用户点"标记完成" |
| `industry_deliver` | `orchestrator` | synthetic | 汇总包质量 Gate + 验证结果为结论卡 |

## 5. 事件 schema（SSE）

SSE 端点每条 `data:` 为一个 JSON 对象：

```json
{"seq": 42, "ts": "2026-07-13T12:00:00+08:00", "run_id": "r_xxx", "type": "...", "step_id": "...", "owner": "...", "payload": {}}
```

- `seq`：run 内单调递增整数，从 1 开始。断线重连用 `?after=<seq>` 补发。
- `step_id`/`owner` 仅在与步骤相关的事件上出现。
- 心跳：每 15s 发 SSE 注释行 `: ping`（不占 seq）。

### 事件类型与 payload（冻结）

| type | payload 字段 |
|---|---|
| `run_started` | `{mode, params, llm_mode, execution_mode?}` |
| `plan_ready` | `{steps:[{step_id, owner, kind, title, status:"pending"\|"skipped", skip_reason?, layer?}], research_state_path?, layer_statuses?, reusable?, next_actions?, display_only?, trace_mode?:"runtime", milestone_states?:{...}}` |
| `coordinator_session_started` | `{session_id, execution_mode:"coordinator_cli"}` |
| `coordinator_message` | `{text, partial?, warning?, stream?, agent_name?, tool_use_id?, compat_for?}`；新版前端可忽略结构化事件的兼容副本 |
| `work_item_upsert` | `{work_item_id, title?, description?, active_form?, status, blocked_by?, owner?}`，来自真实 TaskCreate/TaskUpdate |
| `agent_started` | `{agent_name, description?, invocation_id, tool_use_id?, runtime_task_id?, parent_invocation_id?, work_item_id?}` |
| `agent_completed` | `{agent_name, invocation_id, tool_use_id?, runtime_task_id?, work_item_id?, is_error?, status?, summary?}`；`async_launched/running` 不属于完成 |
| `tool_activity` | `{phase:"started"\|"completed"\|"observed", tool_name, tool_use_id?, invocation_id?, runtime_task_id?, work_item_id?, agent_name?, inferred?, status?, is_error?, summary?}` |
| `handoff` | `{kind:"delegation"\|"delivery"\|"final_delivery", from_owner?, to_owner?, from_station?, to_station?, invocation_id?, work_item_id?, description?, label?, summary?, is_error?}` |
| `step_started` | `{cmd?}` |
| `step_log` | `{line}` |
| `step_progress` | `{done, total, unit, detail?}` |
| `artifact_created` | `{path, name, kind:"json"\|"md"\|"jsonl"\|"pdf"\|"other"}` |
| `step_waiting_llm` | `{instructions, prompt, expected_artifacts:[...], claude_cmd?}` |
| `step_completed` | `{summary?, artifacts?:[...], degraded?}` |
| `step_failed` | `{error, exit_code?}` |
| `step_skipped` | `{reason}` |
| `backflow` | `{from_step, to_owner, reason}` |
| `state_refreshed` | `{layer_statuses:{collector:"ready",...}, reusable:{...}, next_actions?:[...], milestone_states?:{step_id:{readiness_status,run_status,source,artifact_paths,summary}}}`；milestone 是工作区交付就绪状态，不代表真实 Agent 顺序 |
| `run_completed` | `{status:"completed"\|"partial"\|"failed"\|"cancelled", summary?}` |
| `run_error` | `{error}` |

当 `trace_mode="runtime"` 时，前端必须同时展示两套状态：`milestone_states` 回答正式产物是否就绪，`work_item_upsert/agent_*/tool_activity/handoff` 回答真实执行顺序。不得依据 owner 或静态 step 顺序猜测 coordinator 的实际交付链。旧 run 或 legacy/demo/replay 未声明 runtime 时，继续使用原 `step_*` 链路。

`run_error` 是诊断事件，不代表事件流终止；每个 run 最终都必须由 `run_completed`
收束，客户端只把 `run_completed` 视为终止信号。`coordinator_message(partial=true)`
是可替换的实时预览：在线 SSE 可见，但不写入 durable `events.jsonl`；最终完整 assistant
消息仍正常持久化，因此服务重启后只缺少打字过程，不缺少权威内容。

`run_completed.payload.summary`（公司模式，尽力提取，字段可缺省）：
```json
{
  "company_name": "...", "stock_code": "...", "report_year": "...", "as_of_date": "YYYY-MM-DD",
  "valuation_view": "undervalued|fair|overvalued|watch_only|unknown",
  "valuation_view_raw": "估值报告原始值，兼容 fairly_valued 等旧变体",
  "one_line_conclusion": "...",
  "current_price": 0, "market_cap": 0, "price_source": "...",
  "price_observation": {"status":"available|unavailable", "observation_date":"YYYY-MM-DD|null", "price":0, "source":"..."},
  "price_basis": "字符串或原始结构化口径说明",
  "cutoff_status": "at_cutoff|before_cutoff|after_cutoff|unknown",
  "fair_value": {"bear": 0, "base": 0, "bull": 0, "unit": "元/股"},
  "upside_downside": {"bear": -0.1, "base": 0.05, "bull": 0.2},
  "key_assumptions": ["..."],
  "valuation_falsifiers": ["..."],
  "market_context": {"status": "...", "source_count": 0, "tier_counts": {"S":0,"A":0,"B":0,"C":0}, "max_confidence": "..."},
  "layer_statuses": {"collector": "ready", "...": "..."},
  "artifact_paths": {"valuation_report_md": "...", "formal_financial_analysis_md": "...", "market_context_package_md": "..."},
  "confidence": "high|medium|low",
  "gaps": ["..."]
}
```

## 6. REST API（冻结）

| 方法+路径 | 请求 | 响应 |
|---|---|---|
| `GET /api/health` | — | `{ok:true, project_root, bocha_key_present:bool, claude_cli_version:str\|null, active_runs:int}` |
| `GET /api/catalog` | — | `{companies:[{stock_code, company_name, years:[{report_year, report_type, has_pdf, layers:{collector,processor,financial_evidence_draft,formal_financial_analysis,valuation,market_context}}], latest_state_path?}], states:[{stock_code, report_year, path, generated_at}]}`（layers 值为 true/false 的快速文件存在扫描） |
| `POST /api/audit` | `{target?, stock_code?, company_name?, report_year, report_type?, depth?, focus?, as_of_date?, force_refresh?}` | **只读预览**：完整 research_state JSON + `{state_path}`；不覆盖正式状态文件 |
| `POST /api/runs` | `{mode, llm_mode?, params:{...}}`（company 缺省 llm_mode 时使用 coordinator_cli；industry 不接受 coordinator_cli；company 至少给 target/stock_code/company_name；industry 至少给 target/industry_name 或完整公司锚点；replay 必须给 stock_code） | `{run_id}`；参数不足返回 HTTP 400；同一规范化公司已有活动 run 时 HTTP 409，并返回 `existing_run_id` |
| `GET /api/runs` | — | `{runs:[{run_id, mode, status, created_at, params, llm_mode, execution_mode, claude_session_id?, decision_status, baseline_date, review_count}]}`；非 company 的 `decision_status=unsupported` |
| `GET /api/runs/{id}` | — | `{run_id, mode, status, params, llm_mode, execution_mode, claude_session_id?, history:{decision_status,baseline_date,review_count}, events:[全部已发生事件]}` |
| `GET /api/runs/{id}/decision` | — | 新 run 返回 `{status:"frozen",materialized:true,snapshot,warnings}`；旧 run 无快照时从最后一个 `run_completed.summary` 返回 `{status:"derived",materialized:false,...}` 且 **GET 不落盘**。run 不存在 404 `run_not_found`；非 company 409 `unsupported_run_mode`；无 summary 409 `summary_unavailable`；已存在快照损坏 409 `snapshot_corrupt` |
| `GET /api/runs/{id}/reviews` | — | `{status:"available",run_id,reviews:[...],warnings:[...]}`；损坏 JSONL 行跳过并进入 warnings，不让整组读取失败 |
| `POST /api/runs/{id}/reviews` | `{review_date, current_price?, current_price_date?, current_price_source?, benchmark_code?, benchmark_baseline_price?, benchmark_baseline_date?, benchmark_baseline_source?, benchmark_current_price?, benchmark_current_date?, benchmark_current_source?, falsification_status?:"unknown"\|"held"\|"breached", falsification_notes?, note?}` | 本地 current/benchmark 对应端缺失时才使用手工值；手工价格/日期/来源必须成组提供。所有非正价格、非法/越界日期和非法证伪状态在物化 snapshot **之前**返回 400 `invalid_review_request`。成功后首次物化旧 run snapshot 并追加 review，201 `{status:"created",materialized:true,review,review_count,warnings}` |
| `GET /api/runs/{id}/events?after=N` | — | SSE 流：先补发 seq>N 的历史，再持续推送；创建/读取 review 不进入 events |
| `POST /api/runs/{id}/cancel` | — | `{ok}` |
| `POST /api/runs/{id}/steps/{step_id}/complete` | `{force?:bool}` | 期望产物齐 → `{ok}`；不齐且未 force → HTTP 409 `{missing:[...]}` |
| `POST /api/runs/{id}/steps/{step_id}/skip` | — | 仅实际存在 skip 消费窗口的 legacy 步骤返回 `{ok}`；未知、已开始的确定性步骤、synthetic 步骤和 coordinator display-only 步骤返回 HTTP 409 |
| `GET /api/artifact?path=<abs-or-rel>` | — | `{kind:"json"\|"md"\|"jsonl"\|"text"\|"pdf", name, path, size, mtime, content}`；jsonl 只返回前 200 行解析结果；pdf 只返回元信息；**路径必须落在白名单工作区根内**，否则 403 |
| `GET /` | — | static/index.html |

白名单根：五大工作区 + industry collector_workspace + orchestrator_workspace + research_console/console_workspace。

## 7. 角色 → 视觉（前端）

分类色板槽位固定绑定实体（浅色/深色两套，CSS 变量切换），名牌常驻：

| owner | 槽位/浅色 | 深色 | 角色名 | 站点道具 |
|---|---|---|---|---|
| `orchestrator` | 3 `#eda100` | `#c98500` | 调度官 | 指挥台+屏幕 |
| `information-collector` | 1 `#2a78d6` | `#3987e5` | 采集员 | 档案架+PDF 箱 |
| `information-processor` | 2 `#1baf7a` | `#199e70` | 处理员 | 传送带（4 子工位小灯：解析/digest/RAG/比对） |
| `financial-analyst` | 5 `#4a3aa7` | `#9085e9` | 财务分析师 | 白板+计算器 |
| `valuation-analyst` | 8 `#eb6834` | `#d95926` | 估值分析师 | 天平 |
| `market-context-collector` | 6 `#e34948` | `#e66767` | 市场情报员 | 雷达天线 |
| `industry-info-collector` | 4 `#008300` | `#008300` | 行业采集员 | 地图桌 |
| `industry-researcher` | 7 `#e87ba4` | `#d55181` | 行业研究员 | 书堆+望远镜 |

层状态 chip（保留 status 色，永远图标+文字，不裸色）：
`ready`→good `#0ca30c` ✓；`partial`→warning `#fab219` ◐;`stale`→serious `#ec835a` ⏱;
`missing`→中性灰 ○；`incompatible`→serious ⚠；`blocked`/`failed`→critical `#d03b3b` ✕;
`skipped(复用)`→中性灰 + ✓ + "复用" 文字。

小人状态机：`idle`（呼吸浮动）/`walk`（沿路径移动）/`work`（敲打+进度气泡）/
`wait_llm`（举牌"等待 Claude 分析"）/`blocked`（头顶红!）/`done`（跳跃欢呼）/
`sleep`（Zzz，表示该层复用跳过）。
交接动画：上一站完成 → 文件 token 沿路径飞向下一站。
回流动画：红色缺口 token 从 from_step 站点逆向飞到 to_owner 站点。
`run_completed(status=completed)` → 交付台彩带粒子 + 结论卡弹出。

图表规范：三档估值区间图为单轴水平条（悲观-基准-乐观 vs 现价竖线，蓝↔红发散色仅按低估/高估方向），
来源分层为 S/A/B/C 四段横条（顺序色阶蓝），一律直接标注数值文字，禁用双轴。

## 8. 公司历史决策冻结与现在回看

### 8.1 decision snapshot

每个新完成的 company run 在唯一 `run_completed` 之前冻结
`console_workspace/runs/<run_id>/decision_snapshot.json`，并先发布一条
`artifact_created{source:"decision_freeze"}`。冻结失败时已有结论继续交付，但
`completed` 必须降级为 `partial`；不得为了快照写入失败再发布第二个终态。

```json
{
  "schema_version": "1.0",
  "artifact_type": "company_decision_snapshot",
  "run_id": "r_xxx",
  "frozen_at": "ISO-8601",
  "knowledge_cutoff": "YYYY-MM-DD",
  "target": {"company_name":"...","stock_code":"000001","report_year":"2025","report_type":"annual","as_of_date":"YYYY-MM-DD"},
  "decision": {"run_completed.payload.summary 的深复制": "..."},
  "source_artifacts": {
    "valuation_report_md": {"path":"...","status":"available|unavailable","size":0,"mtime_ns":0,"sha256":"..."}
  }
}
```

冻结采用同目录完整临时文件 + fsync + 不可覆盖的原子发布。目标文件一旦存在，任何
重跑、GET 或 POST 都不得覆盖。旧 run 没有快照时，GET 只从**最后一个**
`run_completed.summary` 派生；首次 POST review 才物化。

### 8.2 review JSONL 与指标

reviews 追加写入 `console_workspace/runs/<run_id>/reviews.jsonl`，一行一个对象；review
不是研究运行事件，不写入 `events.jsonl`、不占 SSE seq。读取时坏行跳过并形成 warnings。

本地价格只识别两类宽容结构：腾讯任意嵌套 `qfqday`（数组第 1 项日期、第 3 项收盘价）
与东方财富任意嵌套对象中的 `TRADE_DATE/CLOSE_PRICE`。每端都取**不晚于请求日**的最近
合法交易日；零、负数、NaN/Infinity 价格不可用。优先选择同一 provider 同时覆盖
baseline/current，无法同源时才允许混源或 `unavailable`。

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
      "distances_to_points":{"bear":{"signed":0,"absolute":0,"pct":0},"base":{},"bull":{}}
    },
    "benchmark_change": 0.0,
    "excess_return": 0.0
  },
  "limitations": ["固定非 TSR 限制", "固定非因果限制"]
}
```

`spot_price_change` 是收盘价变化率。公司 baseline 首选能够与 current 同源覆盖两端的本地
日线；否则回退冻结 decision price 并写入 basis warning。本地 current 缺失时才使用用户手工
价格。benchmark 逐端优先本地、缺失端使用手工值，仍缺失则为 unavailable。

bucket 是四段区间分类，而不是最近点：低于 bear、bear 到 base、base 到 bull、高于 bull；
同时返回当前价到三档点的有符号、绝对和百分比距离。三档缺失、非正或不满足
`bear < base < bull` 时 bucket 必须 unavailable。benchmark 缺失时
`benchmark_change/excess_return` 为 null。所有 review 固定声明：股价变化不是股东总回报
（TSR），描述性回看不是因果归因。前端只用文字卡片展示，不引入图表或框架。

## 9. 运行持久化

`research_console/console_workspace/runs/<run_id>/meta.json` 保存
`mode/params/status/created_at/llm_mode/execution_mode/claude_session_id/coordinator_pid`；
`events.jsonl` 保存权威控制台事件（不保存 cumulative partial 预览），`claude_events.jsonl`
保存 coordinator_cli 原始 stdout NDJSON；company run 另保存不可覆盖的 `decision_snapshot.json`
与只追加的 `reviews.jsonl`。`meta.json` 与正式 `research_state.json` 使用同目录
临时文件原子替换；事件序号按历史最大 seq 延续，允许因瞬态事件或损坏行形成空洞但不得重复。
服务重启后历史 run 仍可只读查看；若事件文件已含 `run_completed`，以该终态修复 meta，
不得追加第二个终态。真正中断且无终态的运行才补 `failed`，阶段一不自动 resume。

同一规范化公司、报告类型和财年共享一个正式工作区租约；活动 run 结束、失败或取消前，
后续同目标 run 不得并发启动。运行终态发布前必须先取消并等待所有并行子任务与子进程。

## 10. 编码与平台约束

- 所有 subprocess：`env` 合并 `PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`，stdout 按 utf-8 `errors="replace"` 解码，逐行流式读取。
- Windows 路径统一 `pathlib.Path`；比较用 `resolve()`。
- 端口 8600；`python research_console/app.py` 直接可跑（内嵌 `uvicorn.run`）。
- 前端零构建：原生 ES modules，无 CDN 依赖（完全离线可用）；Canvas 用 devicePixelRatio 适配；`prefers-reduced-motion` 时关闭粒子与走动动画，仅保留状态切换。
