/**
 * 研究工坊轻量前端主控。
 *
 * 功能：
 * 1. 通过既有 REST API 创建、读取和取消运行；
 * 2. 通过 SSE 消费任务状态，并在断线后使用 seq 游标补发；
 * 3. 把运行状态压缩为静态像素插画、任务路径和右侧状态栏；
 * 4. 在不引入框架、Canvas 或图表库的前提下查看关键产物与结论。
 *
 * 为什么保留单一文件：这个页面的目标是低维护成本。状态模型、接口适配和 DOM
 * 渲染集中在一处，可以直接由 FastAPI 静态托管，无需 npm、构建器或额外运行时。
 */

const API_BASE = '';

const OWNER_NAMES = {
  orchestrator: '调度官',
  'information-collector': '信息采集员',
  'information-processor': '信息处理员',
  'financial-analyst': '财务分析员',
  'valuation-analyst': '估值分析员',
  'market-context-collector': '市场上下文采集员',
  'industry-info-collector': '行业信息采集员',
  'industry-researcher': '行业研究员',
};

const LAYERS = [
  ['collector', '采集层'],
  ['processor', '解析层'],
  ['financial_evidence_draft', '证据草稿'],
  ['formal_financial_analysis', '财务分析'],
  ['valuation', '估值层'],
  ['market_context', '市场上下文'],
];

const STATUS = {
  idle: { icon: '○', label: '空闲', tone: 'neutral' },
  pending: { icon: '·', label: '待执行', tone: 'neutral' },
  running: { icon: '↻', label: '执行中', tone: 'info' },
  in_progress: { icon: '↻', label: '执行中', tone: 'info' },
  waiting: { icon: '◷', label: '等待', tone: 'warning' },
  ready: { icon: '✓', label: '就绪', tone: 'good' },
  done: { icon: '✓', label: '完成', tone: 'good' },
  completed: { icon: '✓', label: '完成', tone: 'good' },
  skipped: { icon: '↷', label: '复用/跳过', tone: 'neutral' },
  partial: { icon: '◐', label: '部分完成', tone: 'warning' },
  degraded: { icon: '◐', label: '降级完成', tone: 'warning' },
  stale: { icon: '◷', label: '已过期', tone: 'warning' },
  missing: { icon: '○', label: '缺失', tone: 'neutral' },
  incompatible: { icon: '!', label: '不兼容', tone: 'warning' },
  blocked: { icon: '×', label: '受阻', tone: 'critical' },
  failed: { icon: '×', label: '失败', tone: 'critical' },
  cancelled: { icon: '○', label: '已取消', tone: 'neutral' },
  unknown: { icon: '·', label: '未知', tone: 'neutral' },
};

const VIEW_LABELS = {
  undervalued: ['低估 · 偏机会', 'good'],
  under_valued: ['低估 · 偏机会', 'good'],
  fair: ['大致合理', 'neutral'],
  fairly_valued: ['大致合理', 'neutral'],
  fair_valued: ['大致合理', 'neutral'],
  reasonably_valued: ['大致合理', 'neutral'],
  overvalued: ['高估 · 偏谨慎', 'critical'],
  over_valued: ['高估 · 偏谨慎', 'critical'],
  watch_only: ['仅观察', 'warning'],
  watchlist_only: ['仅观察', 'warning'],
  unknown: ['判断未定', 'neutral'],
};

const state = {
  mode: 'company',
  health: null,
  catalog: null,
  runs: [],
  runId: null,
  runMeta: null,
  runStatus: null,
  events: [],
  lastSeq: 0,
  traceMode: null,
  steps: new Map(),
  stepOrder: [],
  workItems: new Map(),
  workOrder: [],
  agents: new Map(),
  agentOrder: [],
  tools: new Map(),
  layers: {},
  artifacts: new Map(),
  summary: null,
  decision: null,
  decisionStatus: null,
  reviews: [],
  reviewWarnings: [],
  reviewSubmitting: false,
  connection: 'idle',
  batchRender: false,
};

const $ = (id) => document.getElementById(id);

/** 创建 DOM 节点并安全写入文本，避免接口文本被解释为 HTML。 */
function element(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child == null) continue;
    node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  }
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function normalizeStatus(value) {
  const key = String(value || 'unknown').toLowerCase().replaceAll('-', '_');
  return STATUS[key] ? key : 'unknown';
}

function statusInfo(value) {
  return STATUS[normalizeStatus(value)];
}

function ownerName(owner) {
  return OWNER_NAMES[owner] || owner || '调度官';
}

function modeName(mode) {
  return ({ company: '公司研究', industry: '行业研究', demo: '演示', replay: '回放' })[mode] || mode || '—';
}

function formatTime(value) {
  if (!value) return '';
  let normalized = value;
  if (typeof normalized === 'number' && normalized < 1e12) normalized *= 1000;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? '' : date.toTimeString().slice(0, 8);
}

function todayLocal() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

function shortText(value, max = 96) {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/** 显示轻量通知；错误和警告不会阻塞后续状态流。 */
function toast(message, tone = 'info', ttl = 3600) {
  const box = element('div', { class: 'toast', dataset: { tone } }, [
    element('span', { text: message }),
    element('button', { type: 'button', text: '×' }),
  ]);
  const remove = () => box.remove();
  box.querySelector('button').addEventListener('click', remove);
  $('toastHost').appendChild(box);
  window.setTimeout(remove, ttl);
}

/** 统一 REST 客户端，保留 FastAPI detail 与业务错误体。 */
const api = {
  async request(path, options = {}) {
    const response = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const text = await response.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch (error) { body = text; }
    if (!response.ok) {
      const detail = body && body.detail != null ? body.detail : body;
      const message = typeof detail === 'string' ? detail : JSON.stringify(detail || {}).slice(0, 240);
      const apiError = new Error(message || `HTTP ${response.status}`);
      apiError.status = response.status;
      apiError.body = typeof detail === 'object' ? detail : body;
      throw apiError;
    }
    return body;
  },
  health() { return this.request('/api/health'); },
  catalog() { return this.request('/api/catalog'); },
  runs() { return this.request('/api/runs'); },
  run(id) { return this.request(`/api/runs/${encodeURIComponent(id)}`); },
  createRun(body) { return this.request('/api/runs', { method: 'POST', body: JSON.stringify(body) }); },
  audit(body) { return this.request('/api/audit', { method: 'POST', body: JSON.stringify(body) }); },
  cancel(id) { return this.request(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST' }); },
  decision(id) { return this.request(`/api/runs/${encodeURIComponent(id)}/decision`); },
  reviews(id) { return this.request(`/api/runs/${encodeURIComponent(id)}/reviews`); },
  createReview(id, body) {
    return this.request(`/api/runs/${encodeURIComponent(id)}/reviews`, { method: 'POST', body: JSON.stringify(body) });
  },
  artifact(path) { return this.request(`/api/artifact?path=${encodeURIComponent(path)}`); },
};

/**
 * SSE 管理器。
 * 为什么自行重连：浏览器原生 EventSource 的自动重连无法动态更新 after 游标；
 * 主动重建连接可确保断线期间只补发未消费的权威事件。
 */
const stream = {
  source: null,
  runId: null,
  retries: 0,
  timer: null,
  closed: true,

  connect(runId) {
    this.disconnect();
    this.runId = runId;
    this.closed = false;
    this.open();
  },

  open() {
    if (this.closed || !this.runId || this.runId !== state.runId) return;
    setConnection('reconnecting');
    const url = `${API_BASE}/api/runs/${encodeURIComponent(this.runId)}/events?after=${state.lastSeq}`;
    const source = new EventSource(url);
    this.source = source;
    source.onopen = () => {
      this.retries = 0;
      setConnection('connected');
    };
    source.onmessage = (message) => {
      let event;
      try { event = JSON.parse(message.data); } catch (error) { return; }
      receiveEvent(event);
    };
    source.onerror = () => {
      source.close();
      if (this.source === source) this.source = null;
      if (this.closed) return;
      setConnection('reconnecting');
      const delays = [1000, 2000, 5000, 10000, 30000];
      const delay = delays[Math.min(this.retries, delays.length - 1)];
      this.retries += 1;
      window.clearTimeout(this.timer);
      this.timer = window.setTimeout(() => this.open(), delay);
    };
  },

  disconnect() {
    this.closed = true;
    window.clearTimeout(this.timer);
    if (this.source) this.source.close();
    this.source = null;
    this.runId = null;
  },
};

function setConnection(status) {
  state.connection = status;
  const badge = $('connectionBadge');
  badge.dataset.status = status;
  badge.textContent = ({ idle: '未连接', connected: '已连接', reconnecting: '重连中', error: '连接失败' })[status] || status;
}

function rememberOrder(list, id) {
  if (id && !list.includes(id)) list.push(id);
}

function resetRunState() {
  stream.disconnect();
  state.runId = null;
  state.runMeta = null;
  state.runStatus = null;
  state.events = [];
  state.lastSeq = 0;
  state.traceMode = null;
  state.steps.clear();
  state.stepOrder = [];
  state.workItems.clear();
  state.workOrder = [];
  state.agents.clear();
  state.agentOrder = [];
  state.tools.clear();
  state.layers = {};
  state.artifacts.clear();
  state.summary = null;
  state.decision = null;
  state.decisionStatus = null;
  state.reviews = [];
  state.reviewWarnings = [];
  state.reviewSubmitting = false;
  setConnection('idle');
  renderAll();
}

function receiveEvent(event) {
  if (event.run_id && state.runId && event.run_id !== state.runId) return;
  const isPartial = event.type === 'coordinator_message' && event.payload && event.payload.partial;
  if (typeof event.seq === 'number' && !isPartial) {
    if (event.seq <= state.lastSeq) return;
    state.lastSeq = event.seq;
  }
  applyEvent(event);
  if (event.type === 'run_completed') {
    stream.disconnect();
    setConnection('idle');
  }
}

/** 把一条后端事件归一化进前端状态；事件类型保持与 CONTRACT.md 一致。 */
function applyEvent(event) {
  const payload = event.payload || {};
  state.events.push(event);

  switch (event.type) {
    case 'run_started':
      state.runStatus = 'running';
      state.runMeta = { mode: payload.mode, params: payload.params || {} };
      break;

    case 'plan_ready':
      state.traceMode = payload.trace_mode || null;
      state.steps.clear();
      state.stepOrder = [];
      for (const step of payload.steps || []) {
        const id = step.step_id;
        if (!id) continue;
        state.steps.set(id, {
          id,
          title: step.title || id,
          owner: step.owner || 'orchestrator',
          status: step.status === 'skipped' ? 'skipped' : 'pending',
          layer: step.layer || '',
          reason: step.skip_reason || '',
          progress: null,
        });
        state.stepOrder.push(id);
      }
      if (payload.layer_statuses) state.layers = { ...payload.layer_statuses };
      applyMilestones(payload.milestone_states);
      break;

    case 'work_item_upsert': {
      const id = String(payload.work_item_id || '').trim();
      if (!id) break;
      const previous = state.workItems.get(id) || { id };
      state.workItems.set(id, {
        ...previous,
        ...payload,
        id,
        title: payload.title || payload.active_form || previous.title || `任务 #${id}`,
        owner: payload.owner || event.owner || previous.owner || 'orchestrator',
        status: normalizeStatus(payload.status || previous.status || 'pending'),
      });
      rememberOrder(state.workOrder, id);
      break;
    }

    case 'agent_started': {
      const id = String(payload.invocation_id || payload.tool_use_id || payload.runtime_task_id || '').trim();
      if (!id) break;
      state.agents.set(id, {
        ...(state.agents.get(id) || {}),
        ...payload,
        id,
        owner: event.owner || payload.agent_name || 'orchestrator',
        title: payload.description || `${ownerName(payload.agent_name)} 子任务`,
        status: 'running',
      });
      rememberOrder(state.agentOrder, id);
      break;
    }

    case 'agent_completed': {
      const id = String(payload.invocation_id || payload.tool_use_id || payload.runtime_task_id || '').trim();
      if (!id) break;
      const previous = state.agents.get(id) || { id };
      state.agents.set(id, {
        ...previous,
        ...payload,
        id,
        owner: event.owner || payload.agent_name || previous.owner || 'orchestrator',
        title: previous.title || payload.description || `${ownerName(payload.agent_name)} 子任务`,
        status: payload.is_error ? 'failed' : 'completed',
      });
      rememberOrder(state.agentOrder, id);
      break;
    }

    case 'tool_activity': {
      const id = String(payload.tool_use_id || `${payload.invocation_id || event.owner}:${payload.tool_name || 'tool'}`);
      if (payload.phase === 'completed') state.tools.delete(id);
      else state.tools.set(id, { ...payload, id, owner: event.owner || payload.agent_name || 'orchestrator' });
      const invocationId = String(payload.invocation_id || '');
      if (invocationId && state.agents.has(invocationId)) {
        const agent = state.agents.get(invocationId);
        agent.currentTool = payload.phase === 'completed' ? '' : (payload.tool_name || 'tool');
        if (payload.is_error) agent.status = 'failed';
      }
      break;
    }

    case 'step_started':
      updateStep(event.step_id, { status: 'running' });
      break;
    case 'step_progress':
      updateStep(event.step_id, { progress: payload });
      break;
    case 'step_waiting_llm':
      updateStep(event.step_id, { status: 'waiting', reason: payload.instructions || '等待 LLM' });
      break;
    case 'step_completed':
      updateStep(event.step_id, { status: payload.degraded ? 'degraded' : 'done', reason: payload.summary || '' });
      break;
    case 'step_failed':
      updateStep(event.step_id, { status: 'failed', reason: payload.error || '' });
      break;
    case 'step_skipped':
      updateStep(event.step_id, { status: 'skipped', reason: payload.reason || '' });
      break;

    case 'state_refreshed':
      if (payload.layer_statuses) state.layers = { ...state.layers, ...payload.layer_statuses };
      applyMilestones(payload.milestone_states);
      break;

    case 'artifact_created':
      if (payload.path) {
        state.artifacts.set(payload.path, {
          path: payload.path,
          name: payload.name || payload.path.split(/[\\/]/).pop(),
          kind: payload.kind || 'other',
        });
      }
      break;

    case 'run_completed':
      state.runStatus = normalizeStatus(payload.status || 'completed');
      state.summary = payload.summary || null;
      for (const agent of state.agents.values()) {
        if (agent.status === 'running') agent.status = state.runStatus === 'cancelled' ? 'cancelled' : 'completed';
      }
      state.tools.clear();
      refreshRuns();
      if (state.runMeta?.mode === 'company' && state.runId) refreshDecisionHistory(state.runId);
      if (state.summary) {
        $('conclusionSection').open = true;
        toast('研究运行已结束，结论已写入侧栏', state.runStatus === 'completed' ? 'good' : 'warning');
      }
      break;

    case 'run_error':
      toast(`运行诊断：${shortText(payload.error, 160)}`, 'critical', 7000);
      break;

    default:
      break;
  }

  if (!state.batchRender) renderAll();
}

function updateStep(id, patch) {
  if (!id) return;
  const previous = state.steps.get(id) || { id, title: id, owner: 'orchestrator', status: 'pending' };
  state.steps.set(id, { ...previous, ...patch });
  rememberOrder(state.stepOrder, id);
}

function applyMilestones(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return;
  for (const [id, item] of Object.entries(snapshot)) {
    const status = item.run_status === 'completed' ? 'ready' : (item.run_status || item.readiness_status || 'pending');
    updateStep(id, {
      title: item.title || state.steps.get(id)?.title || id,
      owner: item.owner || state.steps.get(id)?.owner || 'orchestrator',
      status: normalizeStatus(status),
      reason: item.summary || state.steps.get(id)?.reason || '',
    });
  }
}

function eventDescription(event) {
  const payload = event.payload || {};
  const step = event.step_id ? state.steps.get(event.step_id) : null;
  const stepTitle = step ? step.title : event.step_id;
  switch (event.type) {
    case 'run_started': return `启动${modeName(payload.mode)}`;
    case 'plan_ready': return `计划就绪，共 ${(payload.steps || []).length} 个步骤`;
    case 'coordinator_session_started': return '主协调会话已启动';
    case 'work_item_upsert': return `${payload.title || payload.active_form || '调度任务'} · ${statusInfo(payload.status).label}`;
    case 'agent_started': return `${ownerName(payload.agent_name)}开始：${payload.description || '子任务'}`;
    case 'agent_completed': return `${ownerName(payload.agent_name)}${payload.is_error ? '失败' : '完成'}`;
    case 'tool_activity': return `${ownerName(event.owner || payload.agent_name)} ${payload.phase === 'completed' ? '完成' : '使用'} ${payload.tool_name || '工具'}`;
    case 'handoff': return `${ownerName(payload.from_owner)} → ${ownerName(payload.to_owner)} · ${payload.label || payload.description || payload.kind || '交接'}`;
    case 'step_started': return `${stepTitle || '步骤'}开始`;
    case 'step_progress': return `${stepTitle || '步骤'} ${payload.done || 0}/${payload.total || 0} ${payload.unit || ''}`;
    case 'step_waiting_llm': return `${stepTitle || '步骤'}等待 LLM`;
    case 'step_completed': return `${stepTitle || '步骤'}完成${payload.degraded ? '（降级）' : ''}`;
    case 'step_failed': return `${stepTitle || '步骤'}失败：${shortText(payload.error)}`;
    case 'step_skipped': return `${stepTitle || '步骤'}跳过：${shortText(payload.reason)}`;
    case 'artifact_created': return `产出 ${payload.name || payload.path || '文件'}`;
    case 'backflow': return `回流至${ownerName(payload.to_owner)}：${shortText(payload.reason)}`;
    case 'state_refreshed': return '研究状态已刷新';
    case 'run_error': return `运行诊断：${shortText(payload.error)}`;
    case 'run_completed': return `运行结束：${statusInfo(payload.status).label}`;
    case 'coordinator_message': return payload.partial ? '协调会话输出中…' : shortText(payload.text, 120);
    default: return event.type;
  }
}

function eventTone(event) {
  if (event.type === 'run_error' || event.type === 'step_failed') return 'critical';
  if (event.type === 'run_completed') return event.payload?.status === 'completed' ? 'good' : 'warning';
  if (event.type === 'step_completed' || event.type === 'agent_completed' || event.type === 'artifact_created') return 'good';
  if (event.type === 'step_waiting_llm' || event.type === 'backflow') return 'warning';
  return 'info';
}

/** 当前路径优先展示真实调度任务；旧运行则退回静态步骤。 */
function pathItems() {
  if (state.workOrder.length) {
    return state.workOrder.map((id) => {
      const item = state.workItems.get(id);
      const activeAgent = [...state.agents.values()].find((agent) => String(agent.work_item_id || '') === id && agent.status === 'running');
      return item ? {
        id,
        title: item.title,
        owner: item.owner,
        status: item.status,
        blockedBy: Array.isArray(item.blocked_by) ? item.blocked_by.map(String) : [],
        tool: activeAgent?.currentTool || '',
        detail: item.description || item.active_form || '',
      } : null;
    }).filter(Boolean);
  }
  return state.stepOrder.map((id) => {
    const step = state.steps.get(id);
    return step ? { ...step, blockedBy: [], tool: '', detail: step.reason || '' } : null;
  }).filter(Boolean);
}

/** 侧栏同时列出调度任务、Agent 和旧式步骤，便于只看文字状态。 */
function taskItems() {
  const rows = [];
  for (const id of state.workOrder) {
    const item = state.workItems.get(id);
    if (item) rows.push({ ...item, kind: '任务' });
  }
  for (const id of state.agentOrder) {
    const item = state.agents.get(id);
    if (item) rows.push({ ...item, kind: 'Agent' });
  }
  if (!rows.length) {
    for (const id of state.stepOrder) {
      const item = state.steps.get(id);
      if (item) rows.push({ ...item, kind: '步骤' });
    }
  }
  return rows;
}

function renderAll() {
  renderRunSummary();
  renderScene();
  renderLayers();
  renderPath();
  renderTasks();
  renderEvents();
  renderArtifacts();
  renderConclusion();
}

function renderRunSummary() {
  $('runId').textContent = state.runId || '—';
  $('runStatus').textContent = state.runStatus ? statusInfo(state.runStatus).label : '空闲';
  $('runMode').textContent = modeName(state.runMeta?.mode || state.mode);
  $('eventCount').textContent = String(state.events.length);
  $('cancelRun').classList.toggle('hidden', !(state.runId && state.runStatus === 'running'));
  $('runSelect').value = state.runId || '';
}

function renderScene() {
  const items = taskItems();
  const current = [...items].reverse().find((item) => ['running', 'in_progress', 'waiting'].includes(normalizeStatus(item.status)));
  const latest = current || items.at(-1);
  const runInfo = statusInfo(state.runStatus || 'idle');
  $('sceneStatus').dataset.status = normalizeStatus(state.runStatus || 'idle');
  $('sceneStatus').textContent = `${runInfo.icon} ${runInfo.label}`;
  renderAgentRoom(items);

  if (!state.runId) {
    $('sceneTitle').textContent = '调度员与研究团队待命';
    $('sceneSubtitle').textContent = '前排调度员接收目标，后方七名专业 Agent 在各自工作站完成研究链路。';
    $('sceneOwner').textContent = '调度官待命';
    $('sceneTool').textContent = '无活动工具';
    return;
  }

  const params = state.runMeta?.params || {};
  const target = params.company_name || params.target || params.stock_code || params.industry_name || '';
  $('sceneTitle').textContent = current ? current.title : `${target || modeName(state.runMeta?.mode)} · ${runInfo.label}`;
  $('sceneSubtitle').textContent = current?.description || current?.detail || latest?.reason || `当前运行已收到 ${state.events.length} 条状态事件。`;
  $('sceneOwner').textContent = current ? ownerName(current.owner) : '调度官';
  const activeTool = current?.currentTool || current?.tool || [...state.tools.values()].at(-1)?.tool_name;
  $('sceneTool').textContent = activeTool ? `工具：${activeTool}` : '无活动工具';
}

/**
 * 把真实 Task / Agent 状态映射到像素房间。
 * 角色不会在房间中来回移动，只通过工作站边框和信号灯表达活跃或完成，
 * 这样既保留多 Agent 空间感，也避免重新引入实时角色动画的维护成本。
 */
function renderAgentRoom(items) {
  const room = document.querySelector('.pixel-room');
  if (!room) return;
  const running = state.runStatus === 'running';
  room.classList.toggle('run-active', running);
  const activeToolOwners = new Set([...state.tools.values()].map((tool) => tool.owner).filter(Boolean));
  const terminalStatuses = new Set(['completed', 'done', 'ready', 'skipped', 'degraded', 'failed', 'cancelled']);

  for (const station of room.querySelectorAll('.agent-station')) {
    const owner = station.dataset.agent;
    const ownerItems = items.filter((item) => item.owner === owner);
    const active = ownerItems.some((item) => ['running', 'in_progress', 'waiting'].includes(normalizeStatus(item.status)))
      || activeToolOwners.has(owner)
      || (owner === 'orchestrator' && running);
    const done = ownerItems.length > 0 && ownerItems.every((item) => terminalStatuses.has(normalizeStatus(item.status)));
    station.classList.toggle('active', active);
    station.classList.toggle('done', done && !active);
  }
}

function renderLayers() {
  const host = $('layerList');
  clear(host);
  for (const [key, label] of LAYERS) {
    const info = statusInfo(state.layers[key] || 'unknown');
    host.appendChild(element('div', { class: 'layer-item' }, [
      element('span', { class: 'layer-name', text: label }),
      element('span', { class: 'state-label', dataset: { tone: info.tone }, text: `${info.icon} ${info.label}` }),
    ]));
  }
  $('layerSection').classList.toggle('hidden', state.runMeta?.mode === 'industry' || state.mode === 'industry' && !state.runId);
}

function renderPath() {
  const host = $('pathNodes');
  const items = pathItems();
  clear(host);
  $('pathCount').textContent = `${items.length} 项`;
  if (!items.length) {
    host.appendChild(element('div', { class: 'empty-state', text: '运行开始后显示任务节点与路径连线' }));
    clear($('pathLines'));
    return;
  }

  items.forEach((item, index) => {
    const info = statusInfo(item.status);
    const button = element('button', {
      type: 'button',
      class: 'path-node',
      dataset: { id: item.id, index: String(index + 1).padStart(2, '0'), status: normalizeStatus(item.status) },
      title: item.detail || item.title,
    }, [
      element('span', { class: 'node-title', text: item.title }),
      element('span', { class: 'node-meta', text: `${ownerName(item.owner)}${item.detail ? ` · ${shortText(item.detail, 42)}` : ''}` }),
      element('span', { class: 'node-bottom' }, [
        element('span', { class: 'node-status', text: `${info.icon} ${info.label}` }),
        item.tool ? element('span', { class: 'node-tool', text: item.tool, title: item.tool }) : null,
      ]),
    ]);
    button.addEventListener('click', () => focusTask(item.id));
    host.appendChild(button);
  });
  window.requestAnimationFrame(() => {
    layoutPathNodes();
    drawPathLines();
  });
}

/**
 * 按容器宽度计算每行列数，并让奇数行反向占位，形成连续 S 形阅读顺序。
 * 节点宽度保持在约 170px 以上；窄屏自动降为两列或一列，不产生页面横向滚动。
 */
function layoutPathNodes() {
  const host = $('pathNodes');
  const nodes = [...host.querySelectorAll('.path-node')];
  if (!nodes.length) return;

  const minimumNodeWidth = 170;
  const columnGap = 46;
  const horizontalPadding = 48;
  const availableWidth = Math.max(0, host.clientWidth - horizontalPadding);
  const columns = Math.max(1, Math.min(
    nodes.length,
    Math.floor((availableWidth + columnGap) / (minimumNodeWidth + columnGap)),
  ));
  host.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
  host.dataset.columns = String(columns);

  nodes.forEach((node, index) => {
    const row = Math.floor(index / columns);
    const positionInRow = index % columns;
    const column = row % 2 === 0 ? positionInRow : columns - 1 - positionInRow;
    node.style.gridRow = String(row + 1);
    node.style.gridColumn = String(column + 1);
    node.dataset.row = String(row);
    node.dataset.column = String(column);
  });
}

/**
 * 根据 S 形节点实际位置绘制路径线和方向箭头。
 * 同一行按照实际方向从左到右或从右到左连接；行尾则垂直向下折返到下一行，
 * 因此任务再长也只增加行数，不会继续把页面向右撑开。
 */
function drawPathLines() {
  const host = $('pathNodes');
  const viewport = $('pathViewport');
  const svg = $('pathLines');
  const nodes = [...host.querySelectorAll('.path-node')];
  clear(svg);
  if (nodes.length < 2) return;

  const width = Math.max(host.clientWidth, viewport.clientWidth);
  const height = Math.max(host.scrollHeight, viewport.clientHeight);
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));
  svg.style.width = `${width}px`;
  svg.style.height = `${height}px`;

  const itemMap = new Map(pathItems().map((item) => [String(item.id), item]));
  const nodeMap = new Map(nodes.map((node) => [String(node.dataset.id), node]));
  const namespace = 'http://www.w3.org/2000/svg';

  nodes.forEach((targetNode, index) => {
    if (index === 0) return;
    const targetItem = itemMap.get(String(targetNode.dataset.id));
    const dependencyId = targetItem?.blockedBy?.find((id) => nodeMap.has(String(id)));
    const sourceNode = dependencyId ? nodeMap.get(String(dependencyId)) : nodes[index - 1];
    if (!sourceNode) return;

    const sameRow = sourceNode.dataset.row === targetNode.dataset.row;
    const targetStatus = normalizeStatus(targetNode.dataset.status);
    const lineClass = `route-line ${['completed', 'done', 'ready', 'skipped'].includes(targetStatus) ? 'done' : ''} ${['running', 'in_progress'].includes(targetStatus) ? 'active' : ''}`;
    const path = document.createElementNS(namespace, 'path');
    const arrow = document.createElementNS(namespace, 'polygon');
    path.setAttribute('class', lineClass);
    arrow.setAttribute('class', 'route-arrow');

    if (sameRow) {
      const movingRight = targetNode.offsetLeft > sourceNode.offsetLeft;
      const startX = movingRight ? sourceNode.offsetLeft + sourceNode.offsetWidth : sourceNode.offsetLeft;
      const startY = sourceNode.offsetTop + sourceNode.offsetHeight / 2;
      const targetX = movingRight ? targetNode.offsetLeft : targetNode.offsetLeft + targetNode.offsetWidth;
      const targetY = targetNode.offsetTop + targetNode.offsetHeight / 2;
      const lineEndX = movingRight ? targetX - 8 : targetX + 8;
      const bend = Math.max(18, Math.abs(lineEndX - startX) / 2);
      path.setAttribute('d', movingRight
        ? `M ${startX} ${startY} C ${startX + bend} ${startY}, ${lineEndX - bend} ${targetY}, ${lineEndX} ${targetY}`
        : `M ${startX} ${startY} C ${startX - bend} ${startY}, ${lineEndX + bend} ${targetY}, ${lineEndX} ${targetY}`);
      arrow.setAttribute('points', movingRight
        ? `${targetX - 8},${targetY - 5} ${targetX},${targetY} ${targetX - 8},${targetY + 5}`
        : `${targetX + 8},${targetY - 5} ${targetX},${targetY} ${targetX + 8},${targetY + 5}`);
    } else {
      const startX = sourceNode.offsetLeft + sourceNode.offsetWidth / 2;
      const startY = sourceNode.offsetTop + sourceNode.offsetHeight;
      const targetX = targetNode.offsetLeft + targetNode.offsetWidth / 2;
      const targetY = targetNode.offsetTop;
      const lineEndY = targetY - 8;
      const bend = Math.max(18, Math.abs(lineEndY - startY) / 2);
      path.setAttribute('d', `M ${startX} ${startY} C ${startX} ${startY + bend}, ${targetX} ${lineEndY - bend}, ${targetX} ${lineEndY}`);
      arrow.setAttribute('points', `${targetX - 5},${targetY - 8} ${targetX},${targetY} ${targetX + 5},${targetY - 8}`);
    }

    if (['completed', 'done', 'ready', 'skipped'].includes(targetStatus)) arrow.style.fill = 'var(--good)';
    if (['running', 'in_progress'].includes(targetStatus)) arrow.style.fill = 'var(--blue)';
    svg.appendChild(path);
    svg.appendChild(arrow);
  });
}

function focusTask(id) {
  const row = [...$('taskList').querySelectorAll('.task-row')].find((node) => node.dataset.id === String(id));
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  row.animate([{ background: 'var(--accent)' }, { background: 'transparent' }], { duration: 700 });
}

function renderTasks() {
  const host = $('taskList');
  const items = taskItems();
  clear(host);
  $('taskCount').textContent = String(items.length);
  if (!items.length) {
    host.appendChild(element('div', { class: 'empty-state small', text: '暂无任务' }));
    return;
  }
  for (const item of items) {
    const info = statusInfo(item.status);
    const meta = [item.kind, ownerName(item.owner), item.currentTool ? `工具 ${item.currentTool}` : '', item.summary || item.reason || ''].filter(Boolean).join(' · ');
    host.appendChild(element('div', { class: 'task-row', dataset: { id: item.id, status: normalizeStatus(item.status) }, title: meta }, [
      element('span', { class: 'task-dot' }),
      element('span', { class: 'task-copy' }, [
        element('span', { class: 'task-title', text: item.title || item.description || item.id }),
        element('span', { class: 'task-meta', text: meta }),
      ]),
      element('span', { class: 'state-label', dataset: { tone: info.tone }, text: `${info.icon} ${info.label}` }),
    ]));
  }
}

function renderEvents() {
  const host = $('eventList');
  clear(host);
  const visible = state.events.filter((event) => !(event.type === 'coordinator_message' && event.payload?.partial)).slice(-80);
  $('latestEvent').textContent = visible.length ? shortText(eventDescription(visible.at(-1)), 40) : '等待事件';
  if (!visible.length) {
    host.appendChild(element('div', { class: 'empty-state small', text: '暂无事件' }));
    return;
  }
  for (const event of visible) {
    host.appendChild(element('div', { class: 'event-row', dataset: { tone: eventTone(event) } }, [
      element('span', { class: 'event-time', text: formatTime(event.ts) }),
      element('span', { class: 'event-mark' }),
      element('span', { text: eventDescription(event), title: eventDescription(event) }),
    ]));
  }
  host.scrollTop = host.scrollHeight;
}

function renderArtifacts() {
  const host = $('artifactList');
  clear(host);
  const artifacts = [...state.artifacts.values()];
  $('artifactCount').textContent = `${artifacts.length} 个文件`;
  if (!artifacts.length) {
    host.appendChild(element('div', { class: 'empty-state small', text: '暂无产物' }));
    return;
  }
  for (const artifact of artifacts) {
    host.appendChild(element('button', {
      type: 'button',
      class: 'artifact-button',
      title: artifact.path,
      onclick: () => openArtifact(artifact),
    }, [
      element('span', { text: artifact.name }),
      element('span', { class: 'artifact-kind', text: artifact.kind }),
    ]));
  }
}

function renderConclusion() {
  const host = $('conclusionBody');
  clear(host);
  const snapshot = state.decision?.snapshot || null;
  const summary = snapshot?.decision || state.summary;
  if (!summary) {
    $('conclusionHint').textContent = '运行完成后显示';
    host.appendChild(element('div', { class: 'empty-state small', text: '暂无结论' }));
    return;
  }

  const [viewText, tone] = VIEW_LABELS[summary.valuation_view] || VIEW_LABELS.unknown;
  $('conclusionHint').textContent = `${viewText} · ${state.reviews.length} 次回看`;

  const decisionPanel = element('section', { class: 'history-panel decision-panel' });
  decisionPanel.appendChild(element('div', { class: 'history-panel-head' }, [
    element('div', {}, [
      element('span', { class: 'history-kicker', text: '当时结论' }),
      element('strong', { text: snapshot?.knowledge_cutoff || summary.as_of_date || '基准日 unavailable' }),
    ]),
    element('span', {
      class: 'history-state',
      text: state.decisionStatus === 'frozen' ? '已冻结' : (state.decisionStatus === 'derived' ? '旧运行派生' : '运行摘要'),
    }),
  ]));
  decisionPanel.appendChild(element('span', { class: 'conclusion-view', dataset: { tone }, text: viewText }));
  decisionPanel.appendChild(element('h3', { text: summary.one_line_conclusion || `${summary.company_name || summary.stock_code || '研究'}已完成` }));

  const observation = summary.price_observation || {};
  if (summary.current_price != null) {
    const observationDate = observation.observation_date || summary.as_of_date || '';
    decisionPanel.appendChild(element('p', {
      text: `基准价：${formatNumber(summary.current_price)}${observationDate ? ` · 观察日 ${observationDate}` : ''}${summary.price_source ? ` · 来源 ${summary.price_source}` : ''}`,
    }));
  } else {
    decisionPanel.appendChild(element('p', { class: 'unavailable', text: '基准价：unavailable' }));
  }
  if (summary.cutoff_status && summary.cutoff_status !== 'unknown') {
    decisionPanel.appendChild(element('p', { text: `价格截止状态：${cutoffLabel(summary.cutoff_status)}` }));
  }
  if (summary.valuation_view_raw && summary.valuation_view_raw !== summary.valuation_view) {
    decisionPanel.appendChild(element('p', { text: `估值观点原始值：${summary.valuation_view_raw}` }));
  }

  const fairValue = summary.fair_value || {};
  if ([fairValue.bear, fairValue.base, fairValue.bull].some((value) => value != null)) {
    const grid = element('div', { class: 'value-grid' });
    for (const [key, label] of [['bear', '悲观'], ['base', '基准'], ['bull', '乐观']]) {
      grid.appendChild(element('div', {}, [
        element('small', { text: label }),
        element('strong', { text: fairValue[key] == null ? 'unavailable' : `${formatNumber(fairValue[key])}${fairValue.unit ? ` ${fairValue.unit}` : ''}` }),
      ]));
    }
    decisionPanel.appendChild(grid);
  }
  if (summary.confidence) decisionPanel.appendChild(element('p', { text: `置信度：${summary.confidence}` }));
  if (Array.isArray(summary.gaps) && summary.gaps.length) decisionPanel.appendChild(element('p', { text: `缺口：${summary.gaps.join('；')}` }));
  host.appendChild(decisionPanel);

  const reviewPanel = element('section', { class: 'history-panel review-panel' });
  reviewPanel.appendChild(element('div', { class: 'history-panel-head' }, [
    element('div', {}, [
      element('span', { class: 'history-kicker', text: '现在回看' }),
      element('strong', { text: state.reviews.length ? `${state.reviews.length} 条记录` : '尚未创建' }),
    ]),
  ]));

  const latest = state.reviews.at(-1);
  if (latest) {
    reviewPanel.appendChild(renderReview(latest));
  } else {
    reviewPanel.appendChild(element('p', { class: 'unavailable', text: '当前回看：unavailable。选择回看日后可用本地行情生成描述性对照。' }));
  }
  if (state.reviewWarnings.length) {
    reviewPanel.appendChild(element('p', { class: 'history-warning', text: `读取警告：${state.reviewWarnings.join('；')}` }));
  }
  reviewPanel.appendChild(buildReviewForm());
  host.appendChild(reviewPanel);
}

function cutoffLabel(value) {
  return ({ at_cutoff: '与知识截止日一致', before_cutoff: '早于知识截止日', after_cutoff: '晚于知识截止日', unknown: '未知' })[value] || value;
}

function formatPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? '+' : ''}${(number * 100).toFixed(2)}%` : 'unavailable';
}

function priceObservationText(label, observation) {
  if (!observation || observation.status !== 'available' || observation.close_price == null) return `${label}：unavailable`;
  return `${label}：${formatNumber(observation.close_price)} · ${observation.observation_date || '日期 unavailable'} · ${observation.source || '来源 unavailable'}`;
}

function valuationBucketLabel(value) {
  return ({
    below_bear: '低于悲观值',
    bear_to_base: '悲观—基准区间',
    base_to_bull: '基准—乐观区间',
    above_bull: '高于乐观值',
  })[value] || 'unavailable';
}

function renderReview(review) {
  const metrics = review.metrics || {};
  const stock = review.prices?.stock || {};
  const benchmark = review.prices?.benchmark || {};
  const bucket = metrics.valuation_bucket || {};
  const distances = bucket.distances_to_points || {};
  const block = element('div', { class: 'review-result' });
  block.appendChild(element('p', { class: 'review-date', text: `回看日 ${review.review_date || 'unavailable'} · 间隔 ${metrics.elapsed_days ?? 'unavailable'} 天` }));
  block.appendChild(element('p', { text: priceObservationText('基准价格', stock.baseline) }));
  block.appendChild(element('p', { text: priceObservationText('回看价格', stock.current) }));
  for (const warning of stock.basis_warnings || []) {
    block.appendChild(element('p', { class: 'history-warning', text: `价格口径提示：${warning}` }));
  }
  block.appendChild(element('div', { class: 'review-metrics' }, [
    element('div', {}, [element('small', { text: '股价变化' }), element('strong', { text: formatPercent(metrics.spot_price_change) })]),
    element('div', {}, [element('small', { text: '估值区间' }), element('strong', { text: bucket.status === 'available' ? valuationBucketLabel(bucket.bucket) : 'unavailable' })]),
    element('div', {}, [element('small', { text: '距基准点' }), element('strong', { text: distances.base ? formatPercent(distances.base.pct) : 'unavailable' })]),
  ]));
  if (bucket.status === 'available') {
    block.appendChild(element('p', {
      text: `距三档点：悲观 ${formatPercent(distances.bear?.pct)} · 基准 ${formatPercent(distances.base?.pct)} · 乐观 ${formatPercent(distances.bull?.pct)}`,
    }));
  } else if (bucket.reason) {
    block.appendChild(element('p', { class: 'unavailable', text: `估值区间 unavailable：${bucket.reason}` }));
  }
  if (review.benchmark_code) {
    block.appendChild(element('p', { text: priceObservationText(`基准 ${review.benchmark_code} 起点`, benchmark.baseline) }));
    block.appendChild(element('p', { text: priceObservationText(`基准 ${review.benchmark_code} 终点`, benchmark.current) }));
    block.appendChild(element('p', { text: `基准变化 ${formatPercent(metrics.benchmark_change)} · 超额 ${formatPercent(metrics.excess_return)}` }));
    for (const warning of benchmark.basis_warnings || []) {
      block.appendChild(element('p', { class: 'history-warning', text: `基准口径提示：${warning}` }));
    }
  }
  const falsificationLabels = { unknown: '未知', held: '证伪条件未触发', breached: '证伪条件已触发' };
  block.appendChild(element('p', {
    class: `falsification-status ${review.falsification_status || 'unknown'}`,
    text: `证伪状态：${falsificationLabels[review.falsification_status] || '未知'}`,
  }));
  if (review.falsification_notes) block.appendChild(element('p', { class: 'review-note', text: `证伪说明：${review.falsification_notes}` }));
  if (review.note) block.appendChild(element('p', { class: 'review-note', text: `备注：${review.note}` }));
  const limitations = Array.isArray(review.limitations) && review.limitations.length
    ? review.limitations
    : ['股价变化不是股东总回报（TSR）。', '回看是描述性对照，不构成因果归因。'];
  block.appendChild(element('div', { class: 'history-limitations' }, limitations.map((item) => element('p', { text: item }))));
  return block;
}

function reviewFormField(label, input) {
  return element('label', { class: 'review-field' }, [element('span', { text: label }), input]);
}

function optionalNumber(input) {
  return input.value.trim() ? Number(input.value) : null;
}

function buildReviewForm() {
  const form = element('form', { class: 'review-form' });
  const dateInput = element('input', { type: 'date', name: 'review_date', value: todayLocal(), required: 'required' });
  const currentPrice = element('input', { type: 'number', name: 'current_price', min: '0', step: 'any', placeholder: '本地缺失时填写' });
  const currentDate = element('input', { type: 'date', name: 'current_price_date' });
  const currentSource = element('input', { type: 'text', name: 'current_price_source', placeholder: '例如券商收盘截图', maxlength: '500' });
  const benchmarkCode = element('input', { type: 'text', name: 'benchmark_code', placeholder: '例如 000300', maxlength: '32' });
  const benchmarkBaselinePrice = element('input', { type: 'number', name: 'benchmark_baseline_price', min: '0', step: 'any', placeholder: '本地缺失时填写' });
  const benchmarkBaselineDate = element('input', { type: 'date', name: 'benchmark_baseline_date' });
  const benchmarkBaselineSource = element('input', { type: 'text', name: 'benchmark_baseline_source', placeholder: '基准起点来源', maxlength: '500' });
  const benchmarkCurrentPrice = element('input', { type: 'number', name: 'benchmark_current_price', min: '0', step: 'any', placeholder: '本地缺失时填写' });
  const benchmarkCurrentDate = element('input', { type: 'date', name: 'benchmark_current_date' });
  const benchmarkCurrentSource = element('input', { type: 'text', name: 'benchmark_current_source', placeholder: '基准终点来源', maxlength: '500' });
  const falsificationStatus = element('select', { name: 'falsification_status' }, [
    element('option', { value: 'unknown', text: '未知' }),
    element('option', { value: 'held', text: '未触发' }),
    element('option', { value: 'breached', text: '已触发' }),
  ]);
  const falsificationNotes = element('textarea', { name: 'falsification_notes', placeholder: '说明哪些证伪条件已验证或触发', maxlength: '4000' });
  const noteInput = element('textarea', { name: 'note', placeholder: '可选回看备注', maxlength: '2000' });
  const button = element('button', { type: 'submit', class: 'button secondary', text: state.reviewSubmitting ? '生成中…' : '创建回看' });
  button.disabled = state.reviewSubmitting || !state.runId || state.runMeta?.mode !== 'company';

  form.append(
    element('div', { class: 'review-form-title', text: '回看设置' }),
    reviewFormField('回看日期', dateInput),
    element('div', { class: 'review-form-title', text: '当前价格手工补数（本地缺失时使用）' }),
    reviewFormField('当前价格', currentPrice),
    reviewFormField('价格日期', currentDate),
    reviewFormField('价格来源', currentSource),
    element('div', { class: 'review-form-title', text: '可选 benchmark 与手工补数' }),
    reviewFormField('基准代码', benchmarkCode),
    reviewFormField('基准起点价格', benchmarkBaselinePrice),
    reviewFormField('基准起点日期', benchmarkBaselineDate),
    reviewFormField('基准起点来源', benchmarkBaselineSource),
    reviewFormField('基准终点价格', benchmarkCurrentPrice),
    reviewFormField('基准终点日期', benchmarkCurrentDate),
    reviewFormField('基准终点来源', benchmarkCurrentSource),
    element('div', { class: 'review-form-title', text: '证伪检查' }),
    reviewFormField('证伪状态', falsificationStatus),
    reviewFormField('证伪说明', falsificationNotes),
    reviewFormField('其他备注', noteInput),
    button,
  );
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.runId || state.reviewSubmitting) return;
    await createDecisionReview({
      review_date: dateInput.value,
      current_price: optionalNumber(currentPrice),
      current_price_date: currentDate.value || null,
      current_price_source: currentSource.value.trim() || null,
      benchmark_code: benchmarkCode.value.trim() || null,
      benchmark_baseline_price: optionalNumber(benchmarkBaselinePrice),
      benchmark_baseline_date: benchmarkBaselineDate.value || null,
      benchmark_baseline_source: benchmarkBaselineSource.value.trim() || null,
      benchmark_current_price: optionalNumber(benchmarkCurrentPrice),
      benchmark_current_date: benchmarkCurrentDate.value || null,
      benchmark_current_source: benchmarkCurrentSource.value.trim() || null,
      falsification_status: falsificationStatus.value,
      falsification_notes: falsificationNotes.value.trim() || null,
      note: noteInput.value.trim() || null,
    });
  });
  return form;
}

function formatNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—';
}

async function openArtifact(artifact) {
  $('modalTitle').textContent = artifact.name;
  clear($('modalBody'));
  $('modalBody').appendChild(element('div', { class: 'empty-state', text: '正在加载产物…' }));
  $('modal').classList.remove('hidden');
  try {
    const data = await api.artifact(artifact.path);
    clear($('modalBody'));
    $('modalBody').appendChild(element('div', { class: 'artifact-meta' }, [
      element('span', { text: `类型 ${data.kind}` }),
      data.size != null ? element('span', { text: `大小 ${formatBytes(data.size)}` }) : null,
      data.mtime ? element('span', { text: `修改 ${data.mtime}` }) : null,
      data.truncated ? element('span', { text: '内容已截断' }) : null,
    ]));
    const content = data.content;
    if (data.kind === 'json' || data.kind === 'jsonl') {
      $('modalBody').appendChild(element('pre', { class: 'artifact-json', text: JSON.stringify(content, null, 2) }));
    } else if (data.kind === 'md') {
      $('modalBody').appendChild(renderMarkdown(String(content || '')));
    } else if (data.kind === 'pdf') {
      $('modalBody').appendChild(element('p', { text: `PDF 仅显示元信息，请在文件系统中打开：${data.path}` }));
    } else {
      $('modalBody').appendChild(element('pre', { class: 'artifact-pre', text: String(content || '') }));
    }
  } catch (error) {
    clear($('modalBody'));
    $('modalBody').appendChild(element('div', { class: 'empty-state', text: `加载失败：${error.message}` }));
  }
}

function formatBytes(value) {
  const number = Number(value) || 0;
  if (number < 1024) return `${number} B`;
  if (number < 1024 * 1024) return `${(number / 1024).toFixed(1)} KB`;
  return `${(number / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * 极简 Markdown 查看器。
 * 先逐字符转义，再只开放标题、列表、粗体和代码块，避免产物中的原始 HTML
 * 进入控制台页面；完整富文本并不是状态前端的核心职责。
 */
function renderMarkdown(markdown) {
  const root = element('div', { class: 'markdown' });
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  let code = false;
  let codeLines = [];
  let list = null;

  const closeList = () => {
    if (list) root.appendChild(list);
    list = null;
  };
  const closeCode = () => {
    root.appendChild(element('pre', {}, [element('code', { text: codeLines.join('\n') })]));
    codeLines = [];
  };

  for (const line of lines) {
    if (line.trim().startsWith('```')) {
      if (code) closeCode();
      else closeList();
      code = !code;
      continue;
    }
    if (code) {
      codeLines.push(line);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      closeList();
      root.appendChild(element(`h${heading[1].length}`, { text: heading[2] }));
      continue;
    }
    const listItem = line.match(/^\s*[-*+]\s+(.+)$/);
    if (listItem) {
      if (!list) list = element('ul');
      list.appendChild(element('li', { text: listItem[1] }));
      continue;
    }
    closeList();
    if (line.trim()) root.appendChild(element('p', { text: line }));
  }
  if (code) closeCode();
  closeList();
  return root;
}

/**
 * 独立读取决策与 reviews。review 不进入 SSE 事件流，因此切换历史运行或创建回看后
 * 必须显式刷新这两个只读接口，不能从 events 推断。
 */
async function refreshDecisionHistory(runId, { quiet = false } = {}) {
  if (!runId || runId !== state.runId || state.runMeta?.mode !== 'company') return;
  const [decisionResult, reviewsResult] = await Promise.allSettled([api.decision(runId), api.reviews(runId)]);
  if (runId !== state.runId) return;

  if (decisionResult.status === 'fulfilled') {
    state.decision = decisionResult.value;
    state.decisionStatus = decisionResult.value.status || null;
    state.reviewWarnings = [...(decisionResult.value.warnings || [])];
  } else if (!quiet && decisionResult.reason?.body?.status !== 'summary_unavailable') {
    toast(`历史决策加载失败：${decisionResult.reason.message}`, 'warning');
  }

  if (reviewsResult.status === 'fulfilled') {
    state.reviews = reviewsResult.value.reviews || [];
    state.reviewWarnings = [...state.reviewWarnings, ...(reviewsResult.value.warnings || [])];
  } else if (!quiet) {
    toast(`回看记录加载失败：${reviewsResult.reason.message}`, 'warning');
  }
  renderConclusion();
}

async function createDecisionReview(body) {
  state.reviewSubmitting = true;
  renderConclusion();
  try {
    const response = await api.createReview(state.runId, body);
    state.reviews.push(response.review);
    state.reviewWarnings = response.warnings || [];
    await refreshDecisionHistory(state.runId, { quiet: true });
    await refreshRuns();
    toast('现在回看已保存', 'good');
  } catch (error) {
    toast(`创建回看失败：${error.message}`, 'critical', 6000);
  } finally {
    state.reviewSubmitting = false;
    renderConclusion();
  }
}

async function refreshRuns() {
  try {
    const response = await api.runs();
    state.runs = response.runs || [];
    renderRunSelect();
  } catch (error) {
    toast(`运行历史加载失败：${error.message}`, 'critical');
  }
}

function renderRunSelect() {
  const select = $('runSelect');
  clear(select);
  select.appendChild(element('option', { value: '', text: state.runs.length ? '选择历史运行' : '暂无运行' }));
  for (const run of state.runs) {
    const target = run.params?.company_name || run.params?.target || run.params?.stock_code || run.params?.industry_name || '';
    const info = statusInfo(run.status);
    const historyText = run.mode === 'company'
      ? ` · 基准 ${run.baseline_date || 'unavailable'} · 回看 ${run.review_count || 0}`
      : '';
    select.appendChild(element('option', {
      value: run.run_id,
      text: `${info.icon} ${target || modeName(run.mode)}${historyText} · ${run.run_id}`,
    }));
  }
  select.value = state.runId || '';
}

/** 加载历史快照后只渲染一次，避免数百条事件逐条触发 DOM 重排。 */
async function loadRun(runId) {
  if (!runId) return;
  resetRunState();
  state.runId = runId;
  location.hash = `run=${encodeURIComponent(runId)}`;
  renderRunSummary();
  try {
    const detail = await api.run(runId);
    state.runMeta = { mode: detail.mode, params: detail.params || {} };
    state.runStatus = normalizeStatus(detail.status);
    state.batchRender = true;
    for (const event of detail.events || []) {
      if (typeof event.seq === 'number' && !(event.type === 'coordinator_message' && event.payload?.partial)) {
        state.lastSeq = Math.max(state.lastSeq, event.seq);
      }
      applyEvent(event);
    }
    state.batchRender = false;
    renderAll();
    const finished = ['completed', 'partial', 'failed', 'cancelled'].includes(state.runStatus);
    if (detail.mode === 'company' && finished) await refreshDecisionHistory(runId, { quiet: true });
    if (!finished) stream.connect(runId);
  } catch (error) {
    state.batchRender = false;
    setConnection('error');
    toast(`运行加载失败：${error.message}`, 'critical');
  }
}

async function startRun(mode, llmMode, params) {
  try {
    const response = await api.createRun({ mode, llm_mode: llmMode, params });
    await refreshRuns();
    await loadRun(response.run_id);
    toast(`已启动${modeName(mode)}`, 'good');
  } catch (error) {
    const existingId = error.status === 409 && error.body?.existing_run_id;
    if (existingId) {
      toast('同一目标已有运行，已接回现有任务', 'warning');
      await loadRun(existingId);
      return;
    }
    toast(`启动失败：${error.message}`, 'critical', 6000);
  }
}

function companyParams() {
  const target = $('companyTarget').value.trim();
  const params = {
    target,
    report_year: $('companyYear').value ? Number($('companyYear').value) : undefined,
    report_type: $('companyReport').value,
    depth: $('companyDepth').value,
    focus: $('companyFocus').value.trim() || undefined,
    as_of_date: $('companyDate').value || todayLocal(),
    force_refresh: $('companyForce').checked,
    run_market_context: $('companyMarket').checked,
  };
  if (/^\d{6}$/.test(target)) params.stock_code = target;
  else if (target) params.company_name = target;
  return params;
}

function industryParams() {
  return {
    target: $('industryTarget').value.trim(),
    industry_name: $('industryName').value.trim() || undefined,
    deliverable_type: $('industryDeliverable').value,
    stock_code: $('anchorCode').value.trim() || undefined,
    company_name: $('anchorName').value.trim() || undefined,
    fiscal_year: $('anchorYear').value ? Number($('anchorYear').value) : undefined,
    as_of_date: $('industryDate').value || todayLocal(),
  };
}

function switchMode(mode) {
  state.mode = mode;
  for (const button of document.querySelectorAll('.mode-tab')) button.classList.toggle('active', button.dataset.mode === mode);
  $('companyForm').classList.toggle('hidden', mode !== 'company');
  $('industryForm').classList.toggle('hidden', mode !== 'industry');
  $('demoForm').classList.toggle('hidden', mode !== 'demo');
  $('replayForm').classList.toggle('hidden', mode !== 'replay');
  renderLayers();
  renderRunSummary();
}

async function runAudit() {
  const params = companyParams();
  if (!params.target) {
    toast('请先填写公司或股票代码', 'warning');
    return;
  }
  toast('正在读取研究状态…', 'info', 1800);
  try {
    const result = await api.audit(params);
    state.layers = {};
    for (const [key, value] of Object.entries(result.layers || {})) {
      state.layers[key] = typeof value === 'object' ? value.status : value;
    }
    renderLayers();
    const ready = Object.values(state.layers).filter((value) => value === 'ready').length;
    toast(`体检完成：${ready} 层就绪`, 'good');
  } catch (error) {
    toast(`体检失败：${error.message}`, 'critical');
  }
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('research_console_theme', next); } catch (error) { /* 不影响使用 */ }
}

async function copyRunId() {
  if (!state.runId) {
    toast('当前没有 run_id', 'warning');
    return;
  }
  try {
    await navigator.clipboard.writeText(state.runId);
    toast('run_id 已复制', 'good', 1800);
  } catch (error) {
    toast(`run_id：${state.runId}`, 'info', 5000);
  }
}

function closeModal() {
  $('modal').classList.add('hidden');
}

function bindEvents() {
  for (const button of document.querySelectorAll('.mode-tab')) {
    button.addEventListener('click', () => switchMode(button.dataset.mode));
  }
  $('themeToggle').addEventListener('click', toggleTheme);
  $('refreshRuns').addEventListener('click', refreshRuns);
  $('runSelect').addEventListener('change', () => {
    if ($('runSelect').value) loadRun($('runSelect').value);
  });
  $('auditButton').addEventListener('click', runAudit);
  $('copyRun').addEventListener('click', copyRunId);
  $('cancelRun').addEventListener('click', async () => {
    if (!state.runId) return;
    try {
      await api.cancel(state.runId);
      toast('已请求取消运行', 'warning');
    } catch (error) {
      toast(`取消失败：${error.message}`, 'critical');
    }
  });

  $('companyForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const params = companyParams();
    if (!params.target) return;
    startRun('company', $('companyLlm').value, params);
  });
  $('industryForm').addEventListener('submit', (event) => {
    event.preventDefault();
    startRun('industry', $('industryLlm').value, industryParams());
  });
  $('demoForm').addEventListener('submit', (event) => {
    event.preventDefault();
    startRun('demo', 'skip', {});
  });
  $('replayForm').addEventListener('submit', (event) => {
    event.preventDefault();
    startRun('replay', null, {
      stock_code: $('replayCode').value.trim(),
      report_year: Number($('replayYear').value),
    });
  });

  $('modalClose').addEventListener('click', closeModal);
  $('modal').addEventListener('click', (event) => {
    if (event.target === $('modal')) closeModal();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeModal();
  });
  window.addEventListener('hashchange', () => {
    const match = location.hash.match(/run=([^&]+)/);
    const runId = match ? decodeURIComponent(match[1]) : '';
    if (runId && runId !== state.runId) loadRun(runId);
  });

  const resizeObserver = new ResizeObserver(() => window.requestAnimationFrame(() => {
    layoutPathNodes();
    drawPathLines();
  }));
  resizeObserver.observe($('pathViewport'));
}

function renderHealth() {
  const badge = $('healthBadge');
  if (!state.health) {
    badge.dataset.tone = 'critical';
    badge.querySelector('span').textContent = '服务不可用';
    return;
  }
  const warnings = [];
  if (!state.health.claude_cli_version) warnings.push('Claude CLI');
  if (!state.health.bocha_key_present) warnings.push('Bocha');
  badge.dataset.tone = warnings.length ? 'warning' : 'good';
  badge.querySelector('span').textContent = warnings.length ? `降级：${warnings.join(' / ')}` : `服务正常 · ${state.health.active_runs || 0} 运行中`;
  badge.title = state.health.claude_cli_version || '未检测到 Claude CLI';
}

async function boot() {
  $('companyDate').value = todayLocal();
  $('industryDate').value = todayLocal();
  bindEvents();
  switchMode('company');
  renderAll();

  const [health, catalog, runs] = await Promise.allSettled([api.health(), api.catalog(), api.runs()]);
  if (health.status === 'fulfilled') state.health = health.value;
  renderHealth();
  if (catalog.status === 'fulfilled') state.catalog = catalog.value;
  if (runs.status === 'fulfilled') state.runs = runs.value.runs || [];
  renderRunSelect();

  const match = location.hash.match(/run=([^&]+)/);
  if (match) loadRun(decodeURIComponent(match[1]));
}

boot();
