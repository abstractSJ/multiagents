"""research_console：A 股多智能体投研项目的图形化控制台后端包。

包结构：
- config：项目根定位、工作区路径、白名单、端口与超时等集中配置；
- state_reader：audit 脚本调用、research_state 解析、catalog 构建、artifact 安全读取；
- steps：公司/行业链路步骤定义、命令构建器、披露窗口推导、LLM 提示词模板；
- engine：Run/EventBus/执行器，负责脚本流式执行、文件监视、LLM 三模式、demo 与 replay；
- app：FastAPI 路由、SSE 推送与静态资源挂载。

本包只做编排：不搬运研究逻辑，只调用既有脚本并监视工作区文件落盘。
"""
