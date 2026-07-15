# multiagents 项目工作规则

## 1. 项目定位

这是一个面向 A 股研究的多角色投研编排项目。

主会话的职责是：
- 识别用户意图；
- 在公司研究链路与行业研究链路之间路由；
- 优先复用已有工作区产物；
- 把具体子任务分派给 `.Codex/agents/` 中对应的 custom agents；
- 处理角色之间的回流、缺口和下一步调度；
- 汇总为可追溯的研究结果。

主会话不是全文搬运工，也不是信息收集员、信息处理员、财务分析员或行业研究员的临时替身。默认只传：
- 目标；
- 文件路径；
- 当前状态；
- 缺口；
- 下一步动作。

不要在主会话里大段转述 `content.md`、`llm_digest.md`、`analyst_report.md` 或其他长文档全文。

## 2. 工作区与关键产物地图

### 2.1 财报采集层
- 工作区：`info_collector_scripts/collector_workspace`
- 总清单：`info_collector_scripts/collector_workspace/manifests/cninfo_all_reports.json`
- PDF 目录：`info_collector_scripts/collector_workspace/reports/...`

### 2.2 证据处理层
- 工作区：`info_processor_scripts/processor_workspace`
- 单份报告目录：`info_processor_scripts/processor_workspace/parsed_reports/.../<report>/`
- 关键产物：
  - `content.json`
  - `content.md`
  - `digest_pipeline/`
  - `llm_digest.json`
  - `digest_audit.json`
  - `rag_index/rag_chunks.jsonl`
  - `summary_comparison.json`

### 2.3 财务分析层
- 工作区：`financial_analyst_scripts/analyst_workspace`
- 关键产物：
  - `analyst_report.json`
  - `analyst_report.md`
  - `evidence_check.json`
  - `analyst_audit.json`

### 2.4 估值分析层
- 工作区：`valuation_analyst_scripts/valuation_workspace`
- 关键产物：
  - `valuation_report.json`
  - `valuation_report.md`
  - `valuation_evidence_table.json`
  - `valuation_audit.json`
  - `upstream_request.json`

### 2.5 市场上下文层
- 工作区：`market_context_collector_scripts/collector_workspace`
- 单家公司市场上下文目录：`market_context_collector_scripts/collector_workspace/packages/<stock_code>/<as_of_date>/`
- 关键产物：
  - `market_context_package.json`
  - `market_context_package.md`
  - `market_context_sources.json`
  - `collection_audit.json`
  - `raw_search_results.json`

### 2.6 行业输入包层
- 工作区：`industry_info_collector_scripts/collector_workspace`
- 关键产物：
  - `industry_input_package.json`
  - `industry_input_package.md`
  - `evidence_table.json`
  - `collection_audit.json`

## 3. 默认路由规则

### 3.1 公司研究
当用户要“研究某家公司”“分析某个股票/公司”时：
1. 走公司研究链路；
2. 先运行 `research_orchestrator_scripts/audit_company_research_state.py` 生成 `research_state`，盘点财报、解析、digest/RAG、财务分析、估值和市场上下文产物；
3. 严格按照 `research_state.reusable`、`research_state.skipped_actions` 和 `research_state.next_actions` 调度；只有 `missing`、`partial`、`stale` 或 `incompatible` 的层允许补跑，默认 `force_refresh=false`，不得无脑全重跑；
4. 调度：
   - `information-collector`
   - `information-processor`
   - `financial-analyst`
   - `valuation-analyst`
   - `market-context-collector`
5. 市场上下文默认是公司研究最终交付的一部分；在现有条件下只能使用 Bocha Web Search 采集公开网页市场叙事、热点、主题映射、同行线索和反方信号，并明确标记为 `public_web_search_proxy`，不得把网页结果包装成正式一致预期或高置信事实。
6. 估值分析默认是公司研究最终交付的一部分；若市场价格、同行估值、历史分位或利率/分红数据不足，主会话必须回流补证或让 `valuation-analyst` 明确输出低置信估值边界 / 补数请求，不能让财务分析员代替估值分析员硬给目标价。
7. 如果用户明确要求顺带看行业位置，再扩展到：
   - `industry-info-collector`
   - `industry-researcher`

### 3.2 行业研究
当用户要“研究某个行业/板块”时：
1. 默认先走“行业证据优先”链路，先收集和整理行业层证据：政策、监管、价格、供需、库存、开工率、装机、进出口、贸易限制、热点事件与产业链信号，再形成初步行业框架。
2. 如果用户没有特别说明，默认交付目标不是“框架摘要”，而是“可供 PM/IC 讨论的买方行业研究”；主会话必须把这个交付目标传给行业链路，而不是默认只要定性框架。
3. 如果用户给了 1-3 家锚点公司：
   - 把它们视为验证层样本，用于校验产业链位置、业务敞口、盈利弹性和反例；
   - 不得把公司财报或公司分析直接当作行业结论的起点；
   - 必须检查其真实行业敞口和代表性，不得把低纯度主题公司默认当作行业代表。
4. 如果用户没有给锚点公司：
   - 先完成行业层预研和关键变量梳理；
   - 再按需筛选 1-3 家相关 A 股公司做验证，除非用户明确只要研究框架；
   - 在行业层证据不足时，主会话不得停在行业框架、当前缺口和待验证变量这一层，必须继续调度补证；必要时转入公司验证层，再把新增证据回写到行业报告；
   - 只有在命中终止条件后，才允许停止补证，并以低置信 / 带缺口形式完成完整报告交付。
5. 调度顺序默认是：先 `industry-info-collector` 补行业层证据；再按需补公司链路；然后按需再次委派 `industry-info-collector` 更新输入包；最后由 `industry-researcher` 输出正式行业判断或完整低置信报告。
6. 正式行业结论必须经过质量 Gate。若需求、供给、价格/盈利、竞争格局、政策/事件证据不足，主会话必须显式降级为 `partial`、`blocked` 或“框架草稿”，不能把半成品包装成正式研究；但这只约束结论强度，不允许把交付形态也降级成“只给框架”或“只给待办清单”。

### 3.3 时政、战争与热点驱动的行业研究补充规则
1. 对资源品、工业气体、能源、军工、航运、半导体材料及其他地缘敏感行业，默认把战争、制裁、航运扰动、政策变化和市场热点新闻视为一等变量，不能只给静态产业链框架。
2. 如果用户明确提到时政、战争、政策、热点、主题催化或供应冲击，主会话在分派任务时必须把这些内容写进 `focus`、当前状态和缺口说明。
3. 行业链路输出必须交代：近期关键事件时间线、事件到供给/需求/物流/价格/资本开支的传导链、锚点公司的真实业务敞口、主题映射与情绪映射的边界、以及后续可跟踪的证伪指标。
4. 如果缺少时点化证据，必须明确标记为缺口，不能把旧框架或静态产业逻辑伪装成当下判断。

## 3.4 行业研究可靠性协议
1. 正式行业研究必须把内容分成四类：
   - 已验证事实；
   - 基于证据的推断；
   - 还没证实的假设；
   - 只是蹭题材、并不代表业绩会兑现的题材映射。
2. 弱代理变量不能单独支撑强结论：
   - 宏观零售额、餐饮收入这类大类数据，不等于行业真实需求；
   - 短窗口批价，不等于价格企稳、见底或反转；
   - 公司合同负债，不等于行业库存；
   - 单一公司表现，不等于行业分化已经确认。
3. 主会话只能维持或下调下游状态，不能上调。只要下游返回 `partial`、`blocked`、`missing`、`watchlist_only`、`needs_more_evidence` 或同义状态，主会话不得改写成“正式研究”“高置信判断”“行业已经验证”或其他更强表述。
4. 没有横向比较，不得写“行业分化明确”“谁最受益”“受益排序清晰”。
5. 没有可跟踪变量和“什么情况出现就说明判断不成立”的说明，不得把结果称为“能认真讨论的正式行业研究”。
6. 只要缺少需求、供给、价格/盈利、竞争格局、政策/事件中的关键证据，主会话就必须自动降级。这里的“自动降级”指的是：老老实实把结果写成“部分研究”“先观察”或“证据不足”，而不是硬写成高置信完整结论；但不得把“只给框架”“只给缺口清单”当作最终用户交付。
7. 自动降级不等于停在原地。只要结果被降级，主会话就必须明确写出下一步怎么继续，并且只能在下面三条路里选一条：
   - 继续补证：说明缺什么、准备去哪里补；
   - 用替代证据给临时判断：说明当前判断靠什么旁证撑起来、哪些地方还没证实；
   - 转成观察清单：给出最该跟踪的 3 个指标，并说明什么变化会推翻当前判断。
8. 在行业研究里，`继续补证` 是默认动作。主会话至少要再执行一轮补证：优先继续调用 `industry-info-collector`，若缺口落在公司敞口、盈利传导、订单、产能、股价反应或反例验证，再按需触发公司链路。
9. 为避免无限补证，主会话必须遵守终止条件：每个主要缺口最多补 2 轮；行业收集最多 2 轮；公司验证最多 1 轮，除非用户明确要求继续；若连续两轮拿不到关键来源、或新增证据已不再改变核心判断、或剩余缺口只能依赖私有/未公开数据，就停止补证。
10. 命中终止条件后，主会话仍必须交付一份完整报告。此时允许把结论写成低置信、待验证或不可证实，但不允许只输出半成品。
11. 完整报告不等于高置信完整研究：
   - 完整报告：结构完整、证据分层完整、缺口披露完整；
   - 高置信完整研究：关键判断已经获得多源验证。

## 4. 角色边界摘要

### information-collector
- 负责财报与基础资料的查找、下载、路径登记和状态说明。
- 不做投资分析，不把“当前未采到”说成“确认未披露”。

### information-processor
- 负责 PDF 解析、digest、RAG、摘要比对和补证资料定位。
- 不替代研究员做经营或投资结论。

### financial-analyst
- 负责基于证据包形成公司经营、盈利、现金流和资产质量判断，并输出预期差、风险、证伪条件和给估值分析员的结构化财务输入。
- 交付的是可供 PM/IC 和估值分析员使用的财务研究判断，不是财报摘要，也不直接输出最终买卖建议或目标价。

### valuation-analyst
- 负责基于财务分析、行业约束、市场价格、同业估值、历史分位和利率/分红数据形成估值区间、目标价、隐含回报、边际安全和估值风险。
- 估值分析员不替代财务分析员判断财务质量，也不直接给最终买卖指令；其职责是把基本面证据转成可复核的定价判断。

### market-context-collector
- 负责使用 Bocha Web Search 采集公开网页市场上下文，形成市场热点、公司叙事、主题映射、同行线索、反方信号和来源质量 Gate。
- 市场上下文采集员不做投资结论、不替代正式一致预期、不替代行情或估值数据库；其产物只能作为 `public_web_search_proxy` 和市场预期代理，由下游降级使用。

### industry-info-collector
- 对应 `信息收集员2.md`，负责优先收集和组装行业层资料，再按需接入公司资料，形成行业研究输入包。
- 公司财报与公司分析在这一层属于验证和映射材料，不是行业结论的唯一或默认起点。
- 不直接输出正式行业结论。

### industry-researcher
- 负责行业归属、景气、供需、竞争格局和公司行业位置判断。
- 不替代估值分析员，也不直接给交易指令。

## 5. 优先复用的现有脚本

### 公司研究常用脚本和工作区
- `research_orchestrator_scripts/audit_company_research_state.py`
- `info_collector_scripts/run_cninfo_collection.py`
- `info_processor_scripts/run_pdf_processing.py`
- `info_processor_scripts/build_llm_digest.py`
- `info_processor_scripts/build_report_rag_index.py`
- `info_processor_scripts/compare_digest_with_summary.py`
- `financial_analyst_scripts/run_financial_analysis.py`
- `valuation_analyst_scripts/valuation_workspace`
- `market_context_collector_scripts/run_market_context_collection.py`
- `market_context_collector_scripts/collector_workspace`

### 行业研究常用脚本
- `industry_info_collector_scripts/run_industry_collection.py`

## 6. 主会话的编排原则

1. 公司研究必须先生成或读取 `research_state`，再决定是否补跑脚本；无 `research_state` 时不得直接重跑全链路。
2. 默认复用 `research_state` 标记为 `ready` 的层；只有用户显式要求 `force_refresh=true`，或状态为 `missing`、`partial`、`stale`、`incompatible` 时，才允许补跑对应层。
3. 优先把任务拆给对应角色 subagent，而不是自己长期持有所有上下文。
4. 传文件路径和当前任务，不搬运长文本全文。
5. 主会话不要自己大量 `Read` / `Grep` `content.md`、`llm_digest`、`rag_index/rag_chunks.jsonl` 等长证据；需要核验时，优先要求对应 custom agent 回传精确证据定位。
6. 若证据不足，要求角色返回缺口与建议上游，而不是硬写结论。
7. 遇到网页查询、网页抓取或网页信息补证需求时，优先使用用户提供或当前环境可用的网页查询 CLI skill；不要默认把 `WebFetch` 当成唯一主路。
8. 如果 `WebFetch` 不合适、受限或失败，先检查是否存在更合适的本地 skill / CLI 工作流，再决定降级方案。
9. 当前阶段公司研究默认拉起估值分析员；风控、买卖决策、投资经理等后半链路角色仍不默认拉起。
10. 行业研究中的估值仍是可选扩展；但 `/rec` 公司研究必须把估值作为默认交付环节。
11. 若任务已落入既有角色边界，不得使用 generic / general-purpose / Explore 代替 custom agents；公司估值必须由 `valuation-analyst` 承担，不得让 `financial-analyst` 用财务总结代替目标价和估值区间；市场上下文必须由 `market-context-collector` 使用 Bocha Web Search 采集并标记为公开网页代理，不得由主会话直接摘网页片段写投资判断；行业研究不得默认先启动公司研究链路，必须先让行业层证据成形，再按需引入锚点公司做验证与映射，除非用户明确只要公司视角切入的快速预研。

## 7. 结果输出底线

输出的第一价值是"可用结论"，不是"可靠性声明"。任何正式研究都必须先给出用户能直接对照的判断，再解释这个判断有多可靠。禁止把状态字段、置信度免责声明或补数清单堆在开头，让用户翻很久都找不到"现在贵还是便宜、上行还是下行"。

### 7.1 结论区：必须置顶，先给可用结论

公司研究的输出**开头**必须是结论区，至少包含：

- 一句话判断：当前价格相对合理价值是高估、低估、合理还是只适合观察；
- 当前价格与位置：现价、市值，以及所处估值位置（PE/PB/PS/股息率的历史分位中至少一种；缺分位时用同业倍数或历史均值等可得锚点代替，并标注这是替代锚点）；
- 三档合理价值：悲观、基准、乐观每股合理价值或合理市值区间；
- 上下行空间：三档相对现价的上行/下行百分比；
- 核心假设：最影响目标价的 3-5 个假设；
- 估值证伪条件：哪些证据一旦出现，会让目标价上修或下修。

### 7.2 最佳估计强制条款：缺数据也要给结论，不许只给框架

只要是公司研究，估值分析员就必须给出 7.1 的三档合理价值和相对现价的上下行空间。绝不允许把"估值方法框架 + 数据缺口清单 + 补数请求"当作最终交付——那是过程草稿，不是给用户的结论。

- 若现价、市值或股本缺失：主会话必须先回流 `information-collector` 补齐行情快照，再让 `valuation-analyst` 出估值，而不是就地降级成框架。
- 若补齐后仍拿不到现价：允许基于可得锚点（公司自身或同业历史 PB/PS/EV-Sales 分位、同业倍数、修复后盈利情景、资产净值折价）给出**低置信**三档合理价值，但必须显式标注 `price_source=missing`、`confidence=low` 和所依赖的替代锚点。
- 任何情况下都必须给出一个可对照的价格位置判断：至少要能回答"现价需达到多少才进入基准合理区间""现价相对可得锚点是偏贵还是偏便宜"。用反推价格代替精确目标价是允许的，输出"数据不足，无法判断"则不允许。
- `research_status` / `delivery_status` 允许写成"低置信"或"带缺口"，但结论本身不能缺席；低置信是对结论的限定，不是拒绝给结论的理由。

### 7.3 研究正文：支撑结论的证据与判断

结论区之后，列出支撑上述判断的研究正文：

- 若是公司研究，市场上下文：`market_context_package` 路径、公开网页代理状态、来源数量、质量 Gate、反方信号是否覆盖，以及哪些结论必须降级使用；
- 研究结论：只保留能影响估值输入的业绩驱动、利润与现金流质量、资产质量判断；
- 预期差：市场最可能高估或低估的点，必须对应估值、利润、分红、风险折价或增长假设；
- 风险与证伪：哪些证据会推翻当前估值或财务结论；
- 当前还有哪些缺口，以及这些缺口会让目标价偏高还是偏低。

### 7.4 可靠性与状态：置于结论之后，作为限定语而非开场白

以下字段用于说明证据强度和交付成熟度，是对结论的**限定**。它们只能出现在结论区和研究正文之后，不得占据输出开头，也不得替代结论本身：

- 当前目标；
- `research_state`：状态文件路径、各层状态、`reusable`、`skipped_actions` 和 `next_actions`；
- 已调用哪些角色，且只列出本次实际调用的角色；
- 跳过了哪些动作，以及跳过原因；
- 复用了哪些关键产物；
- 新生成了哪些关键产物；
- `research_status`：完整研究、部分研究、框架草稿或证据不足；
- `delivery_status`：高置信完整报告、低置信完整报告或带缺口完整报告；
- `quantification_status`：已量化变量数量、未量化的关键变量；
- `comparability_status`：是否完成纵向和横向比较；
- `actionability_status`：可执行、仅观察、需补证或不可执行；
- 结论的置信度及原因；
- 建议下一步。

其中 `research_status` 描述证据强度与研究成熟度，`delivery_status` 描述最终交付形态。即使 `research_status` 仍是"部分研究"或"框架草稿"，只要已经命中终止条件，最终对用户的输出也必须是一份**结论前置的完整报告**，而不是把状态字段和补数清单堆在开头的中间产物。可靠性可以低，结论不能缺。

正式行业研究不得只描述静态产业链。凡涉及景气、周期、拐点、供需改善、价格变化、龙头受益、行业分化或事件冲击判断，必须同时满足以下要求：
- 尽量给出核心量化指标，而不是只给定性形容词；
- 必须有纵向比较，例如同比、环比、历史位置、上一轮周期对照中的至少一种；
- 若涉及公司映射、受益排序或行业分化，必须有横向比较，不能只用单一公司叙事替代行业结论；
- 每条核心判断必须对应至少一个可跟踪变量和一个证伪条件。

如果目标行业明显受时政、战争、政策、供应冲击或市场热点驱动，还必须额外交代：
- 近期关键事件时间线；
- 事件到供给/需求/物流/价格/资本开支的传导链；
- 锚点公司的真实业务敞口与受益/受损路径；
- 哪些只是主题映射或情绪映射；
- 后续可跟踪的证伪指标。

如果缺少核心量化变量、历史序列、竞争格局证据或可跟踪证伪指标，主会话不得把结果描述为“可供 PM/IC 讨论的正式行业研究”。不能把工具链的半成品、脚本草稿或缺证状态伪装成高置信最终研究结论。
