---
name: rec
description: Run the project's company research workflow for one A-share company, reusing local financial report artifacts and dispatching collector, processor, financial analyst, valuation analyst, and market context collector agents as needed.
argument-hint: "支持两种写法：1) target=<公司名或股票代码> [fiscal_year=YYYY] [depth=...]；2) 自然语言，例如：帮我研究中泰股份，重点看现金流"
---

# /rec

单家公司研究入口。这个 skill 的职责是把公司研究任务按固定链路分派给对应 custom agents：`information-collector`、`information-processor`、`financial-analyst`、`valuation-analyst`、`market-context-collector`。主会话只做调度、回流和汇总，不直接承担这些角色的具体职能。

默认交付不是财报摘要，也不是“继续跟踪若干变量”的模糊判断；默认必须给出可供 PM/IC 讨论的买方结论、参考估值区间、目标价或合理市值区间、相对当前价格的上行/下行空间、核心假设和证伪条件。

## 执行原则

- 主会话只负责：解析目标、先运行公司研究状态审计器、按 `research_state` 决定该复用/跳过/委派给谁、整合最终结果。市场上下文只能由 `market-context-collector` 使用 Bocha Web Search 采集，主会话不得把网页片段直接包装成投资结论。
- 主会话不要自己大量 `Read` / `Grep` `content.md`、`llm_digest`、`rag_index/rag_chunks.jsonl` 等长证据；需要核验时，优先要求对应 custom agent 回传精确证据定位。
- 具体职能必须优先委派给项目内 custom agents，而不是由主会话临时扮演，或退化成 generic subagent。
- 下列脚本是对应 custom agents 的工具，不是主会话的默认动作：
  - `run_cninfo_collection.py` → `information-collector`
  - `run_pdf_processing.py` / `build_llm_digest.py` / `build_report_rag_index.py` / `compare_digest_with_summary.py` → `information-processor`
  - `run_financial_analysis.py` → `financial-analyst`
  - `valuation_analyst_scripts/valuation_workspace/...` → `valuation-analyst` 输出目录；当前没有稳定脚本时由 `valuation-analyst` 直接生成报告和审计产物
- 正式财务研究判断由 `financial-analyst` custom agent 承担；正式估值判断由 `valuation-analyst` custom agent 承担。主会话只汇总它们的结论、证据路径、估值区间、缺口和下一步。
- `/rec` 默认必须进入估值环节，不得把“关注后续指标”“仍是优质资产”“基本面有韧性”这类表述当作最终结论。若估值所需市场价格或同业数据缺失，必须先让 `valuation-analyst` 给出补数请求或低置信临时估值边界，而不是让 `financial-analyst` 代替估值。

## 调用方式

支持两种入口写法：

1. 结构化参数：稳定性最高，适合固定流程、自动化调用和减少歧义。
2. 自然语言：主会话会先从文本里提取公司、年份、重点和深度；如果能唯一识别就继续执行，不能唯一识别时再补问。

结构化参数优先，自然语言次之。

示例：

- `/rec target=中泰股份 fiscal_year=2025 depth=standard`
- `/rec 帮我研究中泰股份`
- `/rec 研究一下中泰股份，重点看现金流和应收账款`

## 参数

- `target`：公司名、股票代码或二者同时提供。
- `fiscal_year`：财报年度；未指定时先查 manifest 或已有产物，选择最近可用的完整财报年度。
- `report_type`：默认 `annual`。
- `depth`：`quick`、`standard`、`deep`，默认 `standard`。
- `focus`：可选重点，如 `cashflow`、`receivable`、`growth`、`governance`、`capex`、`valuation`、`dividend`。
- `as_of_date`：全链路知识截止日；未指定时使用当前日期。财报、公告、市场上下文、行情、同行、利率和估值输入只有在可验证日期不晚于该日时才能进入结论。
- `force_refresh`：默认 `false`；只有用户明确要求重做时才设为 `true`，否则必须复用 `research_state` 标记为 `ready` 的层。
- `market_price`：可选，用户可指定观察日股价；未指定时由 `valuation-analyst` 先审计是否已有本地/公开行情快照，必要时要求 `information-collector` 补市场数据。
- `valuation_method`：可选，指定估值方法；未指定时按行业自动选择，银行优先使用 PB-ROE、股息率/DDM、PE 校验，制造业/消费/科技等按业务特征选择 PE、EV/EBITDA、DCF 或 SOTP。
- `run_market_context`：默认 `true`；使用 Bocha Web Search 采集公开市场叙事、热点、主题映射和反方信号，产物只能作为市场预期代理。
- `market_context_freshness`：默认 `oneMonth`；传给 Bocha Web Search 的时效参数。
- `run_industry`：是否在公司研究后做行业位置分析，默认 `false`。

## 标准执行链路

### 1. 主会话解析目标

主会话先做最小解析：

- 提取公司名称、证券代码、财报年度、报告类型、研究重点、`as_of_date` 和 `force_refresh`。
- 如果缺股票代码或公司名，先查 manifest 或本地目录，不要凭空猜测。
- 如果目标仍无法唯一识别，再一次性补问必要信息。

### 2. 主会话运行公司研究状态审计器

在委派任何 custom agent 之前，必须先运行：

```bash
python "research_orchestrator_scripts/audit_company_research_state.py" --target <公司或代码> --report-year <YYYY> --report-type <type> --depth <quick|standard|deep> --focus <focus> --as-of-date <YYYY-MM-DD> --write-state
```

若用户明确要求重做，再追加 `--force-refresh`。审计器会输出 `research_state`，主会话必须按以下规则调度：

- `status=ready`：默认复用，写入最终回复的“复用产物”和 `skipped_actions`，不得重新委派该层角色。
- `status=partial` / `missing`：只补缺失子产物；例如只缺 RAG 时只委派 `information-processor` 补 RAG，不重跑 PDF 解析和 digest。
- `status=stale`：只更新时效敏感层，主要是估值、行情和同业数据，不重跑财报处理层。
- `status=incompatible`：复用旧产物为底稿，只补本次 `depth` 或 `focus` 缺口；历史模式下若原因是 `cutoff_unverified`，不得把旧产物作为事实输入。
- `status=future_incompatible`：产物或上游资料晚于 `as_of_date`，必须隔离，不得作为 stale 参考或底稿传给下游。
- `status=ambiguous` / `blocked`：先解决目标或上游阻塞，不得继续包装成完整研究。

### 3. 主会话按 `research_state` 决定是否委派 `information-collector`

`information-collector` 负责：

- 检查 `info_collector_scripts/collector_workspace/manifests/cninfo_all_reports.json`
- 检查 `info_collector_scripts/collector_workspace/reports/...`
- 确认正式年报、摘要版 PDF、本地路径和缺口
- 必要时调用：

```bash
python "info_collector_scripts/run_cninfo_collection.py" --start-date <YYYY-MM-DD> --end-date <YYYY-MM-DD> --report-types annual --keyword <stock-code-or-company> --download
```

注意：`start-date` / `end-date` 是披露日期窗口，不是财报所属年度；`end-date` 绝不得晚于 `as_of_date`。manifest 中披露日在截止日之后的记录只能进入排除审计，不能下载或复用为本次研究证据。

主会话在这一层只拿回：目标、路径、状态、缺口和建议下一步。

### 4. 主会话按 `research_state` 决定是否委派 `information-processor`

`information-processor` 负责检查和补齐：

- `content.json`
- `content.md`
- `llm_digest.json`
- `digest_audit.json`
- `rag_index/rag_chunks.jsonl`
- `summary_comparison.json`

它可以按需调用：

```bash
python "info_processor_scripts/run_pdf_processing.py" --stock-code <code> --report-type annual --report-year <year>
python "info_processor_scripts/build_llm_digest.py" prepare --content-json <content-json>
python "info_processor_scripts/build_llm_digest.py" auto-digest --pipeline-dir <digest-pipeline-dir>
python "info_processor_scripts/build_llm_digest.py" merge --pipeline-dir <digest-pipeline-dir>
python "info_processor_scripts/build_report_rag_index.py" build --content-json <content-json>
python "info_processor_scripts/compare_digest_with_summary.py" --content-json <content-json>
```

如果摘要 PDF 无法自动定位，必须让 `information-collector` 或主会话提供 `summary_pdf_path`，不要猜测。

主会话在这一层只拿回：报告目录、关键产物路径、质量标记、缺口和下游交接。

### 5. 主会话按 `research_state` 决定是否委派 `financial-analyst`

当证据包可用后，正式财务研究判断由 `financial-analyst` custom agent 承担。传给它：

- 公司和证券代码。
- 财报年度和报告类型。
- `report_dir`。
- `content.json`、`llm_digest.json/md`、`rag_chunks.jsonl`、`summary_comparison.json/md`。
- `analyst_report.json/md` 和 `analyst_audit.json`。
- 用户指定的 `focus` 和 `depth`。
- `as_of_date`、来源财报 `published_at` 和正式分析输出目录；正式分析必须写入 `as_of/<as_of_date>/`，并输出 `cutoff_audit`。

`financial-analyst` 优先回答：

1. 本期业绩变化由什么驱动。
2. 哪些利润、现金流、资产负债表信号代表质量改善或恶化。
3. 哪些一次性项目、会计口径变化或附注风险会改变表面结论。
4. 可作为估值基数的规范化利润、EPS、BVPS、ROE/ROIC、自由现金流、DPS、分红率、资本充足率等字段。
5. 未来 1-3 年收入、利润率、ROE、分红、资本开支、信用成本或其他关键变量的合理边界。
6. 市场最可能误判的利润差、分红差、风险折价差、增长差或资产质量差。
7. 结论最容易被什么证据证伪，以及下一季度或下一期最该跟踪哪些会改变估值输入的信号。
8. 给 `valuation-analyst` 的 `valuation_handoff`：哪些数据可直接建模、哪些需要调整、哪些缺失。

如需 evidence draft，`financial-analyst` 可按需调用：

```bash
python "financial_analyst_scripts/run_financial_analysis.py" --report-dir <report-dir> --analysis-depth <depth>
```

但这个脚本只生成 evidence draft，不能替代正式财务研究结论。

### 6. 主会话按需补估值数据

财务分析完成后，主会话必须检查估值分析员需要的输入是否齐备：

- 当前股价、市值、总股本或流通口径；
- 当前 PE、PB、PS、EV/EBITDA、股息率等适用估值快照；
- 同行估值倍数、同行 ROE/增速/资产质量或现金流质量；
- 历史估值分位；
- 无风险利率、信用利差、分红历史或行业估值参照；
- 用户指定的 `market_price`、`valuation_method` 或估值观察日。

若缺口影响估值，优先委派 `information-collector` 补市场和同业估值数据；如果当前环境拿不到稳定来源，则把缺口明确传给 `valuation-analyst`，要求其输出低置信估值边界或 `blocked` 补数请求。

### 7. 主会话按 `research_state` 决定是否委派 `valuation-analyst`

当财务分析报告和必要估值输入可用后，正式估值判断由 `valuation-analyst` custom agent 承担。传给它：

- 公司、证券代码、交易所、币种和估值日期；该日期同时是所有财务、行情、同行、利率和市场输入的硬性知识截止日；
- `financial-analyst` 的 `analyst_report.json/md`、`evidence_check.json`、`analyst_audit.json`；
- `valuation_handoff`、`normalized_financials`、`forecast_boundaries`、财务风险和证伪条件；
- 市场快照、同行估值、历史估值分位、一致预期或其缺口；
- 行业研究路径或行业约束，如有；
- 用户指定的 `market_price`、`valuation_method`、`focus` 和 `depth`。

`valuation-analyst` 的最终返回必须包含以下字段，除非明确 `status=blocked`：

- `valuation_view`：当前价格相对合理价值是低估、合理、高估、价值陷阱风险或不确定。
- `market_snapshot`：观察日、当前股价、总股本或流通口径、市值、PE/PB/股息率等，含来源和缺口。
- `valuation_method`：主方法、交叉校验方法、失效方法和适用原因。
- `base_case_target_price`：基准目标价或合理市值。
- `fair_value_range_per_share` / `valuation_range`：悲观/基准/乐观三档目标价或市值区间。
- `upside_downside_vs_current_price`：相对当前价格的上行/下行空间；没有当前价格时必须说明不能计算，并给公式。
- `key_assumptions`：目标价最敏感的 3-5 个假设。
- `implied_expectations`：当前价格隐含的增长、ROE、利润率、分红或风险折价。
- `valuation_falsifiers`：哪些数据会让目标价下修或上修。

禁止让 `financial-analyst` 用“还需跟踪”“保持关注”“优质核心资产”替代估值分析员的上述字段。

### 8. 主会话按 `research_state` 决定是否委派 `market-context-collector`

`market-context-collector` 只负责利用 Bocha Web Search 采集公开网页市场上下文，不做投资结论。若 `research_state.layers.market_context.status=ready` 且日期匹配，默认复用已有 `market_context_package.json/md`；若为 `missing`、`partial` 或 `stale`，委派它运行：

```bash
python "market_context_collector_scripts/run_market_context_collection.py" --target <公司或代码> --stock-code <code> --company-name <公司名> --industry <行业> --as-of-date <YYYY-MM-DD> --depth <quick|standard|deep> --focus <focus> --strict-cutoff
```

它必须输出：

- `market_context_package.json/md`：公开市场叙事、热点、主题映射、同行线索、反方信号和质量 Gate。
- `market_context_sources.json`：可追溯来源表。
- `collection_audit.json`：查询计划、错误、来源数量、降级状态和凭证处理说明。
- `raw_search_results.json`：原始搜索结果缓存，供审计和复跑。
- 所有正式产物必须包含 `cutoff_audit`；未来来源保留在原始审计但不能进入 claim，无日期来源只能作为 discovery-only。

使用边界必须写清楚：网页搜索只能形成 `public_web_search_proxy`，用于识别市场关注点、主题映射和预期代理；不得单独支撑正式一致预期、精确行情涨跌幅、高置信目标价或完整行业数据库结论。若 Bocha API 缺失、失败或只返回低质量来源，最终公司研究必须降级为 `fundamental_only`、`watchlist` 或 `public_proxy_only`，不能把网页片段包装成高置信行动结论。

### 9. 回流补证规则

如果 `financial-analyst` 在分析过程中发现：

- 关键附注没有被 digest 覆盖；
- 需要定位收入确认政策、非经常性损益、应收/合同资产、审计事项；
- 需要解释单季度与累计口径差异；

则由主会话把补证请求回流给 `information-processor`，而不是主会话自己接管分析或临时用 generic subagent 顶替。

如果 `valuation-analyst` 在估值过程中发现：

- 当前股价、市值、股本或 EV 缺失；
- 同行估值、历史估值分位、无风险利率、股息率或一致预期缺失；
- 财务分析员没有给出可建模的规范化利润、现金流、BVPS、ROE、DPS、资本约束或假设边界；

则由主会话按缺口回流：市场和同业数据交给 `information-collector`，财务假设边界交回 `financial-analyst`，行业和同行可比性问题交给 `/rei` 或 `industry-researcher`。

### 10. 主会话汇总结果

主会话最后的汇总必须结论前置：先给用户能直接对照的估值判断，再给支撑证据，最后才是可靠性与状态字段。禁止把 `research_state`、调用清单或补数清单堆在开头。

**第一层：结论区（置顶）**

- 一句话结论：当前价格相对合理价值是高估、低估、合理还是只适合观察。
- 当前价格与位置：现价、市值，以及所处估值位置（PE/PB/PS/股息率历史分位中至少一种；缺分位时用同业倍数或历史均值等可得锚点代替，并标注这是替代锚点）。
- 三档合理价值：悲观、基准、乐观每股合理价值或合理市值区间，以及采用方法。
- 上下行空间：三档相对现价的上行/下行百分比。
- 核心假设：最影响目标价的 3-5 个假设。
- 估值证伪条件：哪些证据一旦出现，会让目标价上修或下修。

若现价缺失，按 CLAUDE.md 7.2 最佳估计强制条款执行：先回流补行情快照；仍缺失时基于可得锚点给低置信三档，并标注 `price_source=missing`；绝不允许用"数据不足，无法判断"或"估值框架 + 补数请求"代替结论。

**第二层：研究正文**

- 市场上下文：说明 `market_context_package` 路径、网页代理状态、来源数量、质量 Gate 和必须降级使用的边界。
- 研究结论：只保留能影响估值的业绩驱动、质量判断和核心财务观点。
- 预期差：市场最可能高估或低估的点，必须对应估值、利润、分红、风险折价、增长假设或公开网页市场叙事代理。
- 风险与证伪：哪些证据会推翻当前估值或财务结论。
- 缺口：缺哪些证据或外部数据，以及这些缺口会让目标价偏高还是偏低。

**第三层：可靠性与状态（置于最后，作为限定语）**

- 目标：公司、代码、财年、报告类型。
- `research_state`：状态文件路径、各层 `status`、`reusable`、`next_actions`。
- 已调用角色：只列出本次实际调用的 `information-collector` / `information-processor` / `financial-analyst` / `valuation-analyst` / `market-context-collector`。
- 跳过动作：来自 `research_state.skipped_actions`，说明为什么没有重复执行。
- 复用产物：关键路径。
- 新生成产物：关键路径。
- 置信度：高/中/低及原因。
- 下一步：优先补能改变目标价的证据；不要把普通跟踪清单当作结论。

### 11. 可选行业衔接

如果 `run_industry=true`，或者当前公司研究产物将被 `/rei` 当作行业验证样本使用，主会话不要在公司 skill 内硬写行业结论。应把公司产物路径交给 `/rei` 链路，由：

- 公司研究链路补齐锚点公司产物
- `industry-info-collector` 组装行业输入包
- `industry-researcher` 输出行业判断

主会话只负责交接和汇总。

### 12. 行业衔接 Handoff Gate

当公司研究产物将被用于行业研究验证时，主会话必须要求公司链路补充或明确以下字段；若缺失，也要显式返回缺口：

- 相关业务收入占比；
- 相关业务毛利或利润贡献；
- 产能、销量、订单、客户、区域、AUM、费率或其他最贴近行业映射的业务量字段；
- 上游成本暴露；
- 下游需求暴露；
- 价格传导能力；
- 与目标行业关键变量的敏感性；
- 无法量化的字段及缺口原因。

缺少上述字段时，该公司只能作为“背景样本”或“弱验证样本”，不得作为强验证样本直接支持行业受益排序或行业分化结论。
