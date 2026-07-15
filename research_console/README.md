# research_console 后端

A 股多智能体投研项目的图形化控制台：公司研究默认启动一个完整 `/rec` Claude Code 主协调会话，通过 stream-json + workspace audit 两条通道把实时状态映射为 SSE；原静态 Python DAG 继续作为 legacy/fallback。

接口契约见 [`CONTRACT.md`](./CONTRACT.md)（单一事实源，事件 schema / REST API / step_id 均以其为准）。

## 启动方式

```bash
# 依赖：Python 3.12 + fastapi + uvicorn + pydantic（标准库之外仅此三项）
python research_console/app.py
# 服务地址：http://127.0.0.1:8600
```

- `GET /` 返回 `static/index.html`（前端产物）；`static/` 缺失时返回占位说明页，后端可独立启动。
- 运行持久化落在 `research_console/console_workspace/runs/<run_id>/`：`meta.json` 保存执行模式与 `claude_session_id`，`events.jsonl` 保存权威控制台事件（不保存 cumulative partial 预览），coordinator 模式另存 `claude_events.jsonl` 原始 NDJSON；company run 在唯一终态前冻结不可覆盖的 `decision_snapshot.json`，后续回看追加到 `reviews.jsonl`。meta 与正式 research_state 使用原子替换；服务重启时优先复用已存在的 `run_completed` 修复 meta，只有确实无终态的异常中断才补写 failed，阶段一不自动 resume。

## API 一览

| 方法 + 路径 | 说明 |
|---|---|
| `GET /api/health` | 健康检查：项目根、Bocha key 是否存在（仅布尔）、claude CLI 版本、活跃 run 数、缺失脚本清单 |
| `GET /api/catalog` | 公司/年度产物目录：六层产物存在性快照 + `research_state` 清单 |
| `POST /api/audit` | 只读运行状态盘点，不覆盖正式 `research_state.json`；返回完整状态 + 规范化 `state_path` |
| `POST /api/runs` | 创建运行：`{mode, llm_mode?, params}`；按 mode 校验最小目标参数；同一公司的活动 run 冲突时返回 409 + `existing_run_id` |
| `GET /api/runs` | 列出全部运行 |
| `GET /api/runs/{id}` | 单个运行详情（含历史决策摘要元数据与全部已发生事件） |
| `GET /api/runs/{id}/decision` | 读取冻结决策；旧 company run 只派生不落盘，首次 POST review 才物化 |
| `GET /api/runs/{id}/reviews` | 读取追加回看；损坏 JSONL 行通过 warnings 降级返回 |
| `POST /api/runs/{id}/reviews` | 创建现在回看；支持 current/benchmark 手工价格补数、证伪状态与备注，所有非法价格/日期在 snapshot 物化前返回 400；review 不进入 SSE events |
| `GET /api/runs/{id}/events?after=N` | SSE 事件流：先补发 `seq>N` 历史再持续推送；每 15s 发 `: ping` 心跳；仅 `run_completed` 终止，`run_error` 只是诊断事件 |
| `POST /api/runs/{id}/cancel` | 取消运行（terminate→kill 子进程，收尾 `run_completed{status:"cancelled"}`） |
| `POST /api/runs/{id}/steps/{step_id}/complete` | 手动完成 LLM 步骤：产物齐 → `{ok}`；不齐且未 `force` → HTTP 409 `{missing:[...]}` |
| `POST /api/runs/{id}/steps/{step_id}/skip` | 仅跳过存在实际消费窗口的 legacy 步骤；未知、已开始、synthetic 或 coordinator display-only 步骤返回 409 |
| `GET /api/artifact?path=...` | 白名单内安全读取产物：json 解析返回、md/text 原文、jsonl 前 200 行、pdf 只回元信息、>2MB 截断标记 `truncated` |

产物读取白名单根：五大工作区（collector / processor / analyst / valuation / market_context）+ 行业收集工作区 + orchestrator 工作区 + 控制台工作区；白名单外一律 403。

## 架构说明

```
research_console/
├── config.py        # 项目根定位（以文件位置向上推导）、工作区/脚本路径、白名单、端口 8600、超时、特性开关
├── state_reader.py  # audit 脚本调用与 research_state 解析、catalog 构建、artifact 安全读取、估值报告宽容提取
├── history.py       # 公司决策快照冻结、旧 run 派生、reviews JSONL、本地行情读取与描述性指标
├── steps.py         # 步骤定义（step_id 冻结）、计划构建、披露窗口推导、命令构建器、LLM 提示词模板（纯函数，零 IO）
├── engine.py        # Run / EventBus / 执行器：脚本流式执行、进度探针、回流检测、LLM 三模式、demo / replay
└── app.py           # FastAPI 路由 + SSE + 静态挂载 + uvicorn 入口
```

### 编排原则

#### company 默认：`coordinator_cli`

- 每个 company run 只启动一次完整 `/rec`：`claude -p <prompt> --output-format stream-json --verbose --include-partial-messages --permission-mode auto`；不使用 `--bare`，因此项目 `CLAUDE.md`、skills、custom agents、hooks 与 MCP 继续正常发现。
- stdout 按 Claude Code 顶层 NDJSON 宽容解析：保存 `system/init.session_id`，把 TaskCreate/TaskUpdate、真实 Agent invocation、工具活动、委派与结果回传映射为结构化事件；`async_launched` 不再误判为完成，后台 Bash 不再伪装成 `agent`。partial 文本按时间节流、内存只保留最新预览且不写 durable 事件文件，坏 JSON 行只降级为 warning。
- 左栏在 coordinator 模式拆为“实时执行链”和“交付里程碑”：前者跟随真实 Task/Agent/Tool/Handoff，后者由每 4 秒一次的 research_state audit 生成完整 milestone 快照；两者不混写。观察器只读状态，绝不代替 `/rec` 调度 Agent。
- 中央画板在角色站点旁显示任务卡与当前工具，并以显式委派、产物回传、结果回传和最终交付轨迹表现真实链路；legacy/demo/replay 未声明 runtime trace 时仍使用静态步骤动画。
- Claude 退出后执行 final audit，并复用原结论卡提取；非零退出、缺 result、permission denial 或关键层未 ready 会忠实降级为 partial/failed。

#### legacy/fallback

原静态 DAG 完整保留：

- `manual`：分步发 `step_waiting_llm`，轮询期望产物；
- `claude_cli`：正式财务分析、估值等步骤各自启动一个 `claude -p`；
- `skip`：跳过 LLM 步骤并降级交付。

行业链仍使用上述 legacy 三种模式；`coordinator_cli` 仅支持 company。demo/replay 不经过真实研究链路。

### Legacy 公司链路要点

- 先跑 audit 生成 `research_state`；`reusable=true` 的层直接映射为 skipped（复用已有产物），只有 missing / partial / stale / incompatible 的层进入待执行；processor 为 partial 时按 `quality_flags.missing_required_artifacts` 精确细化到 parse / digest / rag / compare 四个子步骤；`force_refresh=true` 时全部执行。
- 每个确定性步骤完成后重跑 audit（秒级）并发 `state_refreshed`，层状态面板实时刷新，同时刷新后续步骤所需路径。
- `market_context_update` 与主线并行（audit 后即启动）；`valuation_update` 等待正式财务分析完成且市场上下文分支结束（完成/跳过/失败均放行）。无 Bocha API key 时市场上下文自动加 `--dry-run` 并标记 `degraded`（key 值绝不进入日志与事件）。
- 回流（`backflow`，仅提示不自动重跑）：digest 不完整（`digest_audit.complete=false`）、证据核验通过率 <60%、草稿存在阻塞性补证请求、估值目录出现 `upstream_request.json`。
- 估值期望产物同时监视新布局 `valuation_workspace/reports/<code>/<date>/` 与旧布局 `valuation_workspace/<code>/<date>/`，任一凑齐四件套即完成。
- `deliver` 从 `valuation_report.json` / `formal_financial_analysis.json` / `market_context_package.json` 宽容提取结论卡（中英文字段变体、数字/字典/区间取值形态都兼容），写入 `run_completed.payload.summary`。summary 同时保留 `as_of_date`、规范化与原始估值观点、价格观察日/口径及 `cutoff_status`。

### 历史决策冻结与现在回看

- 新 company run 在唯一 `run_completed` 前把当时 summary 深复制为 `decision_snapshot.json`，保存 run/冻结时间/知识截止日/目标与来源产物 SHA-256；快照使用不可覆盖的原子首次发布，冻结失败只把交付降级为 partial，不丢已有结论。
- 旧 run 的 `GET decision` 只从最后一个 `run_completed.summary` 派生，不改历史目录；首次 `POST reviews` 才把派生快照物化。后续 reviews 只追加到 JSONL，读取坏行时返回 warnings。
- 回看价格宽容读取本地腾讯 `qfqday` 与东方财富 `TRADE_DATE/CLOSE_PRICE`，每端取不晚于请求日的最近合法交易日并优先 baseline/current 同源。公司 current 本地缺失时可用手工价格；baseline 无法同源时回退冻结 decision price并显示口径 warning。可选 benchmark 的本地缺失端也可用手工价格补齐，仍缺失则显示 `unavailable`。
- 指标包含间隔天数、股价变化、四段估值区间（below bear / bear-base / base-bull / above bull）及到三档点的距离，以及可选 benchmark 变化/超额；三档缺失或非单调时区间 unavailable。review 另保存并展示 `unknown/held/breached` 证伪状态和说明。固定声明股价变化不是 TSR，描述性回看不是因果归因。前端以“当时结论 / 现在回看”文字卡展示，不引入框架或图表。

### demo 与 replay

- `demo`：纯脚本化事件序列（总时长约 60-70 秒），覆盖全部事件类型（含 2 个 skipped 步骤、digest 进度 0/25→25/25、一次回流、`step_waiting_llm` 自动完成、市场上下文并行进度、庆祝收尾）；不碰真实工作区、不起子进程，结论卡数据明确标注"演示数据"。
- `replay`：给定 `stock_code/report_year`，扫描各工作区真实产物，按文件 mtime 升序合成完整事件序列（事件 `ts` 即产物 mtime），一次性写入事件流，run 状态直接 completed，由前端按倍速播放；估值 / 市场上下文取该代码下最新日期目录。

### 失败与降级策略

- 主线致命步骤（采集 / 解析 / digest / RAG / 草稿）失败 → run `failed`；
- 摘要比对失败降级为 `step_completed{degraded:true}`（摘要 PDF 缺失属可接受场景）；
- 市场上下文失败不阻塞估值；正式财务分析失败/被跳过时估值让路（`step_skipped`）；
- 到达交付但存在失败、运行期跳过、任意 `degraded` 步骤或 LLM 模式降级 → run `partial`，结论卡照常输出（可用结论优先，缺口如实披露）；
- run 取消/异常时先取消并等待市场上下文等并行任务与全部登记子进程，再发布唯一 `run_completed`，保证终态之后不再有迟到事件；
- 同一规范化公司、报告类型和财年由工作区租约串行化，避免两个 run 并发覆盖 collector/processor/analyst/research_state 正式产物。

## 阶段一边界

本阶段只接通一个完整 `/rec` 主协调会话与 stream-json 可视化。尚未加入独立的动态研究请求卡片、统一 `research_requests` schema、请求状态机、去重或 blocking/non-blocking 协议。

## 测试

```bash
python -m pytest tests/test_research_console.py -q
```

覆盖：计划构建、coordinator prompt/命令、stream-json mapper/后台 Agent 状态、坏行与 seq 空洞恢复、partial 持久化收敛、meta 原子替换/单终态恢复、audit/legacy 进程取消、并行子任务收尾、同目标工作区租约、skip 消费窗口、health 非阻塞、状态观察器只读去重、coordinator 不进入 legacy DAG、demo/replay 结构、估值 summary 宽容提取，以及 decision snapshot 不可覆盖冻结、旧 run 物化、reviews/API/UI 数据契约和腾讯/东方财富本地行情回看。普通单测零网络、零真实 Claude 调用。
