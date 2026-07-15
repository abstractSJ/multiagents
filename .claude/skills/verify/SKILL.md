---
name: verify
summary: 运行并端到端验证 research_console 的浏览器与 API 表面
---

# research_console 运行验证

1. 在项目根启动服务：

   ```bash
   python research_console/app.py
   ```

2. 使用 `playwright-cli` 打开 `http://127.0.0.1:8600`，确认页面标题为“研究工坊 · 多智能体投研控制台”。
3. 通过页面切换到“演示”，点击“播放演示”；运行中确认左栏同时出现“实时执行链”和“交付里程碑”，Agent 行能显示当前工具，里程碑随产物变为“产物就绪”，中央画板能看到任务卡与委派/回传轨迹。
4. 等待约 60 秒，确认结论卡出现“演示数据”，最后事件为 `run_completed`，倒数第二个事件为 `handoff(kind=final_delivery)`；事件总数以当前契约为准，不固定写死。
5. 在浏览器页面上下文请求 `/api/health` 与当前 `/api/runs/{run_id}`；确认健康状态为 `ok=true`，事件中包含 `work_item_upsert/agent_started/tool_activity/handoff`，且不存在 `agent_name=agent` 的假角色。
6. 邻接探测：向 `/api/runs` 提交空 company 参数，应返回 HTTP 400 且运行总数不增加；读取白名单外 artifact 应返回 HTTP 403。
7. 保存完成态截图，并在结束后关闭浏览器、停止服务，避免遗留端口和后台进程。

注意：历史 demo 事件不会被新版本回写；验证事件 schema 或状态刷新修复时必须新建 demo run，不能只重载旧 run。
