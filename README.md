# multiagents：A 股多角色投研编排系统

`multiagents` 是一个面向 A 股公司研究与行业研究的多 Agent 投研工作流项目。项目把资料采集、证据处理、财务分析、行业研究、估值分析和最终汇总拆成职责明确的角色，强调证据可追溯、结论可降级、研究链路可复核。

> 本项目仅用于投研辅助、工作流编排和多 Agent 协作实验，不构成任何证券投资建议。所有研究结论都需要人类复核、合规判断和风险控制。

---

## 核心能力

- **公司研究链路**：从 A 股财报采集、PDF 解析、digest/RAG 证据处理，到财务分析和估值分析。
- **行业研究链路**：优先收集行业层证据，再按需引入锚点公司验证行业判断。
- **Claude Code 编排入口**：通过 `/re`、`/rec`、`/rei` 三个 skill 路由公司或行业研究任务。
- **角色边界清晰**：信息收集员不做投资判断，财务分析员不直接给目标价，估值分析员不替代财务质量判断。
- **质量 Gate**：当需求、供给、价格、竞争格局、政策事件等证据不足时，研究状态必须降级，不能包装成高置信结论。
- **本地证据资产**：财报、解析结果、RAG、分析报告等默认落在本地 workspace，不进入 Git 仓库。

---

## 总体架构

```mermaid
flowchart TD
    U[用户研究请求] --> R[/re 统一入口]

    R --> C[/rec 公司研究链路]
    R --> I[/rei 行业研究链路]

    C --> IC[information-collector]
    IC --> IP[information-processor]
    IP --> FA[financial-analyst]
    FA --> VA[valuation-analyst]

    I --> IIC[industry-info-collector]
    IIC --> IR[industry-researcher]
    IIC -.必要时验证公司敞口.-> C

    VA --> S[主会话汇总]
    IR --> S
    S --> O[可追溯研究交付]
```

---

## 目录结构

```text
multiagents/
├── .claude/
│   ├── agents/                         # Claude Code custom agents 定义
│   ├── skills/
│   │   ├── re/                         # 统一研究入口
│   │   ├── rec/                        # 单家公司研究入口
│   │   └── rei/                        # 行业/板块研究入口
│   └── settings.json                   # 可共享的项目权限配置
├── info_collector_scripts/             # A 股财报采集脚本
├── info_processor_scripts/             # PDF 解析、digest、RAG 与摘要比对脚本
├── financial_analyst_scripts/          # 财务分析证据草稿生成脚本
├── industry_info_collector_scripts/    # 行业输入包收集与组装脚本
├── industry_researcher_scripts/        # 行业研究产物工作区，默认不提交运行结果
├── valuation_analyst_scripts/          # 估值分析参考数据与运行产物目录
├── overseas_company_research_scripts/  # 海外公开源公司资料采集实验链路
├── CLAUDE.md                           # 项目级 Claude Code 工作规则
├── README.md                           # 项目说明
└── *.md                                # 各角色职责说明文档
```

---

## 关键角色

| 角色 | 职责 | 典型产物 |
|---|---|---|
| `information-collector` | 检查/下载 A 股财报，维护披露清单和本地 PDF 路径 | manifest、PDF、采集状态 |
| `information-processor` | 解析 PDF，生成正文、digest、RAG 和摘要比对 | `content.json`、`llm_digest.json`、`rag_chunks.jsonl` |
| `financial-analyst` | 基于证据包形成公司经营、盈利质量、现金流和资产质量判断 | `analyst_report.json/md` |
| `valuation-analyst` | 形成估值区间、目标价、隐含回报、边际安全和估值风险 | `valuation_report.json/md` |
| `industry-info-collector` | 组装行业层证据、政策/价格/供需/事件变量和锚点公司验证材料 | `industry_input_package.json/md` |
| `industry-researcher` | 输出行业归属、供需、景气、竞争格局和公司位置判断 | `industry_research_report.json/md` |

---

## 快速开始

### 1. 准备环境

推荐使用 Python 3.11+。

```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r info_processor_scripts/requirements.txt
```

当前显式依赖主要服务于 PDF 解析链路：

```text
PyMuPDF
pypdf
Pillow
```

### 2. Claude Code 入口

在 Claude Code 中可直接使用项目 skill：

```text
/re mode=company target=贵州茅台 fiscal_year=2025 depth=standard
/rec target=中泰股份 fiscal_year=2025 depth=standard
/rei target=氦气 anchor_companies=中泰股份 deliverable_type=theme_event_study focus=geopolitics,price,supply
```

入口职责：

- `/re`：统一识别公司研究或行业研究，并路由到对应链路。
- `/rec`：单家公司研究，默认进入财务分析和估值分析。
- `/rei`：行业/板块研究，默认行业证据优先，锚点公司只作为验证样本。

### 3. 直接运行脚本

采集 A 股财报：

```bash
python "info_collector_scripts/run_cninfo_collection.py" \
  --start-date 2026-04-01 \
  --end-date 2026-04-30 \
  --report-types annual \
  --keyword 600519 \
  --download
```

解析本地财报 PDF：

```bash
python "info_processor_scripts/run_pdf_processing.py" \
  --stock-code 600519 \
  --report-type annual \
  --report-year 2025 \
  --limit 1
```

准备 LLM digest 分段任务：

```bash
python "info_processor_scripts/build_llm_digest.py" prepare \
  --content-json "info_processor_scripts/processor_workspace/parsed_reports/.../content.json" \
  --overwrite
```

构建 RAG 索引：

```bash
python "info_processor_scripts/build_report_rag_index.py" build \
  --content-json "info_processor_scripts/processor_workspace/parsed_reports/.../content.json" \
  --overwrite
```

生成财务分析证据草稿：

```bash
python "financial_analyst_scripts/run_financial_analysis.py" \
  --report-dir "info_processor_scripts/processor_workspace/parsed_reports/..."
```

组装行业输入包：

```bash
python "industry_info_collector_scripts/run_industry_collection.py" \
  --target "氦气" \
  --as-of-date 2026-06-03 \
  --deliverable-type investment_research
```

海外公开源公司资料实验链路需要 SEC User-Agent：

```bash
SEC_USER_AGENT="your-name your-email@example.com" \
python "overseas_company_research_scripts/run_public_company_research.py" \
  --ticker MU \
  --as-of-date 2026-06-03
```

---

## 运行产物约定

项目默认把运行结果写入各自 workspace：

| 目录 | 内容 | Git 策略 |
|---|---|---|
| `info_collector_scripts/collector_workspace/` | 财报 PDF、CNINFO manifest、采集审计 | 不提交 |
| `info_processor_scripts/processor_workspace/` | PDF 解析结果、digest、RAG、摘要比对 | 不提交 |
| `financial_analyst_scripts/analyst_workspace/` | 财务分析报告和审计文件 | 不提交 |
| `industry_info_collector_scripts/collector_workspace/` | 行业输入包、证据表、收集审计 | 不提交 |
| `industry_researcher_scripts/researcher_workspace/` | 行业研究报告 | 不提交 |
| `valuation_analyst_scripts/valuation_workspace/` | 估值报告、市场快照、证据表 | 不提交 |
| `overseas_company_research_scripts/research_workspace/` | SEC/公开网页原始资料和输入包 | 不提交 |

如需公开示例数据，建议新建 `examples/` 或 `sample_data/`，只放体量小、已脱敏、来源可说明的样例，不要直接提交真实运行 workspace。

---

## GitHub 提交建议

### 应提交

- 项目文档：`README.md`、`CLAUDE.md`、根目录角色说明文档。
- Claude 编排资产：`.claude/agents/`、`.claude/skills/`、`.claude/settings.json`。
- 源码脚本：各 `*_scripts/*.py`。
- 依赖文件：`info_processor_scripts/requirements.txt`。
- 可复用参考数据：`industry_info_collector_scripts/reference/`；`valuation_analyst_scripts/reference/` 需确认来源和授权后再决定是否提交。

### 不应提交

- `.claude/settings.local.json`：包含本机绝对路径和本地权限 allowlist。
- `.playwright-cli/`：浏览器调试日志和页面快照。
- `*_workspace/`：所有采集、解析、RAG、分析、估值和研究报告产物。
- `tmp_*/`：临时资料包。
- `__pycache__/`、`*.pyc`、`*.log`：运行缓存和日志。
- `*.pdf`、图片、Excel、数据库文件：通常体量大，且可能涉及版权或数据来源边界。

---

## 推荐上传流程

项目当前适合按“源码公开、数据不入库”的方式上传 GitHub。

```bash
git init

git add README.md .gitignore CLAUDE.md \
  .claude/settings.json .claude/agents .claude/skills \
  *.md \
  info_collector_scripts/*.py \
  info_processor_scripts/*.py info_processor_scripts/requirements.txt \
  financial_analyst_scripts/*.py \
  industry_info_collector_scripts/*.py industry_info_collector_scripts/reference \
  overseas_company_research_scripts/*.py

# 可选：确认估值样例数据来源和授权后再加入。
# git add valuation_analyst_scripts/reference

git status --short
git diff --cached --stat

git commit -m "Initial public release"
```

创建远程仓库后再推送：

```bash
git branch -M main
git remote add origin <your-github-repo-url>
git push -u origin main
```

如果使用 GitHub CLI：

```bash
gh repo create <owner>/<repo> --private --source . --remote origin --push
```

首次建议先建 **private repository**，确认没有误提交财报、日志、本地路径、研究产物或敏感配置后，再决定是否改为 public。

---

## 发布前检查清单

```bash
git status --short
git diff --cached --stat
git ls-files | grep -E "(_workspace/|settings.local.json|\.playwright-cli/|__pycache__|\.pyc$|\.pdf$|\.log$)"
```

最后一条命令应当没有输出；如果有输出，说明仍有不该公开的文件被加入暂存区，需要先从暂存区移除。

---

## 研究质量边界

- 行业研究不得只用单一公司表现替代行业结论。
- 弱代理变量不能支撑强结论，例如短期价格、单家公司合同负债或宏观大类数据。
- 估值输出应给出悲观、基准、乐观三档，而不是单点价格。
- 证据不足时应明确标记为低置信、部分研究或观察清单。
- 任何投资相关输出都必须保留核心假设、风险、缺口和证伪条件。
