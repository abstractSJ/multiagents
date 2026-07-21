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
  orchestrator: 'Coordinator',
  'information-collector': 'Information Collector',
  'information-processor': 'Information Processor',
  'financial-analyst': 'Financial Analyst',
  'valuation-analyst': 'Valuation Analyst',
  'market-context-collector': 'Market Context Collector',
  'industry-info-collector': 'Industry Information Collector',
  'industry-researcher': 'Industry Researcher',
};

const LAYERS = [
  ['collector', 'Collection'],
  ['processor', 'Processing'],
  ['financial_evidence_draft', 'Evidence Draft'],
  ['formal_financial_analysis', 'Financial Analysis'],
  ['valuation', 'Valuation'],
  ['market_context', 'Market Context'],
];

const STATUS = {
  idle: { icon: '○', label: 'Idle', tone: 'neutral' },
  pending: { icon: '·', label: 'Pending', tone: 'neutral' },
  running: { icon: '↻', label: 'Running', tone: 'info' },
  in_progress: { icon: '↻', label: 'Running', tone: 'info' },
  waiting: { icon: '◷', label: 'Waiting', tone: 'warning' },
  ready: { icon: '✓', label: 'Ready', tone: 'good' },
  done: { icon: '✓', label: 'Completed', tone: 'good' },
  completed: { icon: '✓', label: 'Completed', tone: 'good' },
  skipped: { icon: '↷', label: 'Reused / Skipped', tone: 'neutral' },
  partial: { icon: '◐', label: 'Partially Completed', tone: 'warning' },
  degraded: { icon: '◐', label: 'Completed with Limitations', tone: 'warning' },
  stale: { icon: '◷', label: 'Stale', tone: 'warning' },
  missing: { icon: '○', label: 'Missing', tone: 'neutral' },
  incompatible: { icon: '!', label: 'Incompatible', tone: 'warning' },
  blocked: { icon: '×', label: 'Blocked', tone: 'critical' },
  failed: { icon: '×', label: 'Failed', tone: 'critical' },
  cancelled: { icon: '○', label: 'Cancelled', tone: 'neutral' },
  unknown: { icon: '·', label: 'Unknown', tone: 'neutral' },
};

const VIEW_LABELS = {
  undervalued: ['Undervalued · Opportunity Bias', 'good'],
  under_valued: ['Undervalued · Opportunity Bias', 'good'],
  fair: ['Broadly Fair', 'neutral'],
  fairly_valued: ['Broadly Fair', 'neutral'],
  fair_valued: ['Broadly Fair', 'neutral'],
  reasonably_valued: ['Broadly Fair', 'neutral'],
  overvalued: ['Overvalued · Caution Bias', 'critical'],
  over_valued: ['Overvalued · Caution Bias', 'critical'],
  watch_only: ['Watch Only', 'warning'],
  watchlist_only: ['Watch Only', 'warning'],
  unknown: ['View Undetermined', 'neutral'],
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
  // 初始 audit 先于 plan_ready 完成时，暂存 step 事件，待固定路线声明后回放。
  earlyStepPatches: new Map(),
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
  return OWNER_NAMES[owner] || owner || 'Coordinator';
}

function modeName(mode) {
  return ({ company: 'Company Research', industry: 'Industry Research', demo: 'Demo', replay: 'Replay' })[mode] || mode || '—';
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
  badge.textContent = ({ idle: 'Disconnected', connected: 'Connected', reconnecting: 'Reconnecting', error: 'Connection Failed' })[status] || status;
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
  state.earlyStepPatches.clear();
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
      // python_agent_coordinator 的 initial audit 会在 plan_ready 之前发布完成事件。路线创建后
      // 必须回放这些补丁，否则 Audit Research State 会永久停留在 Pending。
      for (const [id, patch] of state.earlyStepPatches.entries()) {
        if (state.steps.has(id)) updateStep(id, patch);
      }
      state.earlyStepPatches.clear();
      break;

    case 'work_item_upsert': {
      const id = String(payload.work_item_id || '').trim();
      if (!id) break;
      const previous = state.workItems.get(id) || { id };
      state.workItems.set(id, {
        ...previous,
        ...payload,
        id,
        title: payload.title || payload.active_form || previous.title || `Task #${id}`,
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
        title: payload.description || `${ownerName(payload.agent_name)} subtask`,
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
        title: previous.title || payload.description || `${ownerName(payload.agent_name)} subtask`,
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
      updateStep(event.step_id, { status: 'waiting', reason: payload.instructions || 'Waiting for LLM' });
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

    case 'target_resolved':
      state.runMeta = state.runMeta || { mode: 'company', params: {} };
      state.runMeta.params = { ...(state.runMeta.params || {}), ...payload, target: payload.stock_code || state.runMeta.params?.target };
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
        toast('Research run finished; the conclusion is available in the status rail', state.runStatus === 'completed' ? 'good' : 'warning');
      }
      break;

    case 'run_error':
      toast(`Run diagnostic: ${shortText(payload.error, 160)}`, 'critical', 7000);
      break;

    default:
      break;
  }

  if (!state.batchRender) renderAll();
}

/**
 * 只更新 plan_ready 已声明的固定里程碑。
 * 运行期事件可能携带临时或兼容性 step_id，但这些事件不能扩展或重排 Task Route；
 * 动态调度活动由 workItems、agents 与 tools 独立承载。
 */
function updateStep(id, patch) {
  if (!id) return;
  if (!state.steps.has(id)) {
    const previous = state.earlyStepPatches.get(id) || {};
    state.earlyStepPatches.set(id, { ...previous, ...patch });
    return;
  }
  const previous = state.steps.get(id);
  state.steps.set(id, { ...previous, ...patch });
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
    case 'run_started': return `Started ${modeName(payload.mode)}`;
    case 'plan_ready': return `Plan ready with ${(payload.steps || []).length} steps`;
    case 'coordinator_session_started': return 'Coordinator session started';
    case 'work_item_upsert': return `${payload.title || payload.active_form || 'Coordination task'} · ${statusInfo(payload.status).label}`;
    case 'agent_started': return `${ownerName(payload.agent_name)} started: ${payload.description || 'subtask'}`;
    case 'agent_completed': return `${ownerName(payload.agent_name)} ${payload.is_error ? 'failed' : 'completed'}`;
    case 'tool_activity': return `${ownerName(event.owner || payload.agent_name)} ${payload.phase === 'completed' ? 'completed' : 'used'} ${payload.tool_name || 'tool'}`;
    case 'handoff': return `${ownerName(payload.from_owner)} → ${ownerName(payload.to_owner)} · ${payload.label || payload.description || payload.kind || 'handoff'}`;
    case 'step_started': return `${stepTitle || 'Step'} started`;
    case 'step_progress': return `${stepTitle || 'Step'} ${payload.done || 0}/${payload.total || 0} ${payload.unit || ''}`;
    case 'step_waiting_llm': return `${stepTitle || 'Step'} is waiting for LLM`;
    case 'step_completed': return `${stepTitle || 'Step'} completed${payload.degraded ? ' with limitations' : ''}`;
    case 'step_failed': return `${stepTitle || 'Step'} failed: ${shortText(payload.error)}`;
    case 'step_skipped': return `${stepTitle || 'Step'} skipped: ${shortText(payload.reason)}`;
    case 'artifact_created': return `Created ${payload.name || payload.path || 'artifact'}`;
    case 'backflow': return `Returned to ${ownerName(payload.to_owner)}: ${shortText(payload.reason)}`;
    case 'state_refreshed': return 'Research state refreshed';
    case 'target_resolved': return `Resolved target: ${payload.company_name || 'Company'} (${payload.stock_code || 'unknown code'}) · FY${payload.report_year || 'unknown'}`;
    case 'run_error': return `Run diagnostic: ${shortText(payload.error)}`;
    case 'run_completed': return `Run finished: ${statusInfo(payload.status).label}`;
    case 'coordinator_message': return payload.partial ? 'Coordinator session is streaming…' : shortText(payload.text, 120);
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

/**
 * 按 plan_ready 的声明顺序返回固定路线里程碑。
 * Task Route 只表达正式研究阶段及其完成状态，不混入运行期临时任务、Agent 调用或工具活动，
 * 从而保证同一次运行的路线结构不会随协调器的动态调度而改变。
 */
function routeMilestones() {
  return state.stepOrder.map((id) => {
    const step = state.steps.get(id);
    return step ? { ...step, detail: step.reason || '' } : null;
  }).filter(Boolean);
}

/** 侧栏同时列出调度任务、Agent 和旧式步骤，便于只看文字状态。 */
function taskItems() {
  const rows = [];
  for (const id of state.workOrder) {
    const item = state.workItems.get(id);
    if (item) rows.push({ ...item, kind: 'Task' });
  }
  for (const id of state.agentOrder) {
    const item = state.agents.get(id);
    if (item) rows.push({ ...item, kind: 'Agent' });
  }
  if (!rows.length) {
    for (const id of state.stepOrder) {
      const item = state.steps.get(id);
      if (item) rows.push({ ...item, kind: 'Step' });
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
  $('runStatus').textContent = state.runStatus ? statusInfo(state.runStatus).label : 'Idle';
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
    $('sceneTitle').textContent = 'Coordinator and Research Team Ready';
    $('sceneSubtitle').textContent = 'Python owns the research plan and dispatches registered agents with explicit input/output contracts; specialists execute only their assigned step.';
    $('sceneOwner').textContent = 'Coordinator ready';
    $('sceneTool').textContent = 'No active tool';
    return;
  }

  const params = state.runMeta?.params || {};
  const target = params.company_name || params.target || params.stock_code || params.industry_name || '';
  $('sceneTitle').textContent = current ? current.title : `${target || modeName(state.runMeta?.mode)} · ${runInfo.label}`;
  $('sceneSubtitle').textContent = current?.description || current?.detail || latest?.reason || `The current run has received ${state.events.length} status events.`;
  $('sceneOwner').textContent = current ? ownerName(current.owner) : 'Coordinator';
  const activeTool = current?.currentTool || current?.tool || [...state.tools.values()].at(-1)?.tool_name;
  $('sceneTool').textContent = activeTool ? `Tool: ${activeTool}` : 'No active tool';
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
  const milestones = routeMilestones();
  clear(host);
  $('pathCount').textContent = `${milestones.length} milestones`;
  if (!milestones.length) {
    host.appendChild(element('div', { class: 'empty-state', text: 'Route milestones will appear when a run starts.' }));
    clear($('pathLines'));
    return;
  }

  milestones.forEach((milestone, index) => {
    const info = statusInfo(milestone.status);
    host.appendChild(element('div', {
      class: 'path-node',
      dataset: { id: milestone.id, index: String(index + 1).padStart(2, '0'), status: normalizeStatus(milestone.status) },
      title: milestone.detail || milestone.title,
    }, [
      element('span', { class: 'node-title', text: milestone.title }),
      element('span', { class: 'node-meta', text: `${ownerName(milestone.owner)}${milestone.detail ? ` · ${shortText(milestone.detail, 42)}` : ''}` }),
      element('span', { class: 'node-bottom' }, [
        element('span', { class: 'node-status', text: `${info.icon} ${info.label}` }),
      ]),
    ]));
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

  const namespace = 'http://www.w3.org/2000/svg';

  nodes.forEach((targetNode, index) => {
    if (index === 0) return;
    const sourceNode = nodes[index - 1];

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

function renderTasks() {
  const host = $('taskList');
  const items = taskItems();
  clear(host);
  $('taskCount').textContent = String(items.length);
  if (!items.length) {
    host.appendChild(element('div', { class: 'empty-state small', text: 'No tasks yet' }));
    return;
  }
  for (const item of items) {
    const info = statusInfo(item.status);
    const meta = [item.kind, ownerName(item.owner), item.currentTool ? `Tool ${item.currentTool}` : '', item.summary || item.reason || ''].filter(Boolean).join(' · ');
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
  $('latestEvent').textContent = visible.length ? shortText(eventDescription(visible.at(-1)), 40) : 'Waiting for events';
  if (!visible.length) {
    host.appendChild(element('div', { class: 'empty-state small', text: 'No events yet' }));
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
  $('artifactCount').textContent = `${artifacts.length} files`;
  if (!artifacts.length) {
    host.appendChild(element('div', { class: 'empty-state small', text: 'No artifacts yet' }));
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
    $('conclusionHint').textContent = 'Available after completion';
    host.appendChild(element('div', { class: 'empty-state small', text: 'No conclusion yet' }));
    return;
  }

  const [viewText, tone] = VIEW_LABELS[summary.valuation_view] || VIEW_LABELS.unknown;
  $('conclusionHint').textContent = `${viewText} · ${state.reviews.length} reviews`;

  const decisionPanel = element('section', { class: 'history-panel decision-panel' });
  decisionPanel.appendChild(element('div', { class: 'history-panel-head' }, [
    element('div', {}, [
      element('span', { class: 'history-kicker', text: 'Original View' }),
      element('strong', { text: snapshot?.knowledge_cutoff || summary.as_of_date || 'As-of date unavailable' }),
    ]),
    element('span', {
      class: 'history-state',
      text: state.decisionStatus === 'frozen' ? 'Frozen' : (state.decisionStatus === 'derived' ? 'Derived from Legacy Run' : 'Run Summary'),
    }),
  ]));
  decisionPanel.appendChild(element('span', { class: 'conclusion-view', dataset: { tone }, text: viewText }));
  decisionPanel.appendChild(element('h3', { text: summary.one_line_conclusion || `${summary.company_name || summary.stock_code || 'Research'} completed` }));

  const observation = summary.price_observation || {};
  if (summary.current_price != null) {
    const observationDate = observation.observation_date || summary.as_of_date || '';
    decisionPanel.appendChild(element('p', {
      text: `Baseline price: ${formatNumber(summary.current_price)}${observationDate ? ` · Observation date ${observationDate}` : ''}${summary.price_source ? ` · Source ${summary.price_source}` : ''}`,
    }));
  } else {
    decisionPanel.appendChild(element('p', { class: 'unavailable', text: 'Baseline price: unavailable' }));
  }
  if (summary.cutoff_status && summary.cutoff_status !== 'unknown') {
    decisionPanel.appendChild(element('p', { text: `Price cutoff status: ${cutoffLabel(summary.cutoff_status)}` }));
  }
  if (summary.valuation_view_raw && summary.valuation_view_raw !== summary.valuation_view) {
    decisionPanel.appendChild(element('p', { text: `Raw valuation view: ${summary.valuation_view_raw}` }));
  }

  const fairValue = summary.fair_value || {};
  if ([fairValue.bear, fairValue.base, fairValue.bull].some((value) => value != null)) {
    const grid = element('div', { class: 'value-grid' });
    for (const [key, label] of [['bear', 'Bear'], ['base', 'Base'], ['bull', 'Bull']]) {
      grid.appendChild(element('div', {}, [
        element('small', { text: label }),
        element('strong', { text: fairValue[key] == null ? 'unavailable' : `${formatNumber(fairValue[key])}${fairValue.unit ? ` ${fairValue.unit}` : ''}` }),
      ]));
    }
    decisionPanel.appendChild(grid);
  }
  if (summary.confidence) decisionPanel.appendChild(element('p', { text: `Confidence: ${summary.confidence}` }));
  if (Array.isArray(summary.gaps) && summary.gaps.length) decisionPanel.appendChild(element('p', { text: `Gaps: ${summary.gaps.join('; ')}` }));
  host.appendChild(decisionPanel);

  const reviewPanel = element('section', { class: 'history-panel review-panel' });
  reviewPanel.appendChild(element('div', { class: 'history-panel-head' }, [
    element('div', {}, [
      element('span', { class: 'history-kicker', text: 'Current Review' }),
      element('strong', { text: state.reviews.length ? `${state.reviews.length} records` : 'Not created yet' }),
    ]),
  ]));

  const latest = state.reviews.at(-1);
  if (latest) {
    reviewPanel.appendChild(renderReview(latest));
  } else {
    reviewPanel.appendChild(element('p', { class: 'unavailable', text: 'Current review: unavailable. Select a review date to generate a descriptive comparison using local market data.' }));
  }
  if (state.reviewWarnings.length) {
    reviewPanel.appendChild(element('p', { class: 'history-warning', text: `Read warnings: ${state.reviewWarnings.join('; ')}` }));
  }
  reviewPanel.appendChild(buildReviewForm());
  host.appendChild(reviewPanel);
}

function cutoffLabel(value) {
  return ({ at_cutoff: 'Matches knowledge cutoff', before_cutoff: 'Before knowledge cutoff', after_cutoff: 'After knowledge cutoff', unknown: 'Unknown' })[value] || value;
}

function formatPercent(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `${number >= 0 ? '+' : ''}${(number * 100).toFixed(2)}%` : 'unavailable';
}

function priceObservationText(label, observation) {
  if (!observation || observation.status !== 'available' || observation.close_price == null) return `${label}: unavailable`;
  return `${label}: ${formatNumber(observation.close_price)} · ${observation.observation_date || 'Date unavailable'} · ${observation.source || 'Source unavailable'}`;
}

function valuationBucketLabel(value) {
  return ({
    below_bear: 'Below Bear Case',
    bear_to_base: 'Bear-to-Base Range',
    base_to_bull: 'Base-to-Bull Range',
    above_bull: 'Above Bull Case',
  })[value] || 'unavailable';
}

function renderReview(review) {
  const metrics = review.metrics || {};
  const stock = review.prices?.stock || {};
  const benchmark = review.prices?.benchmark || {};
  const bucket = metrics.valuation_bucket || {};
  const distances = bucket.distances_to_points || {};
  const block = element('div', { class: 'review-result' });
  block.appendChild(element('p', { class: 'review-date', text: `Review date ${review.review_date || 'unavailable'} · ${metrics.elapsed_days ?? 'unavailable'} days elapsed` }));
  block.appendChild(element('p', { text: priceObservationText('Baseline Price', stock.baseline) }));
  block.appendChild(element('p', { text: priceObservationText('Review Price', stock.current) }));
  for (const warning of stock.basis_warnings || []) {
    block.appendChild(element('p', { class: 'history-warning', text: `Price basis note: ${warning}` }));
  }
  block.appendChild(element('div', { class: 'review-metrics' }, [
    element('div', {}, [element('small', { text: 'Share Price Change' }), element('strong', { text: formatPercent(metrics.spot_price_change) })]),
    element('div', {}, [element('small', { text: 'Valuation Range' }), element('strong', { text: bucket.status === 'available' ? valuationBucketLabel(bucket.bucket) : 'unavailable' })]),
    element('div', {}, [element('small', { text: 'Distance to Base' }), element('strong', { text: distances.base ? formatPercent(distances.base.pct) : 'unavailable' })]),
  ]));
  if (bucket.status === 'available') {
    block.appendChild(element('p', {
      text: `Distance to cases: Bear ${formatPercent(distances.bear?.pct)} · Base ${formatPercent(distances.base?.pct)} · Bull ${formatPercent(distances.bull?.pct)}`,
    }));
  } else if (bucket.reason) {
    block.appendChild(element('p', { class: 'unavailable', text: `Valuation range unavailable: ${bucket.reason}` }));
  }
  if (review.benchmark_code) {
    block.appendChild(element('p', { text: priceObservationText(`Benchmark ${review.benchmark_code} Start`, benchmark.baseline) }));
    block.appendChild(element('p', { text: priceObservationText(`Benchmark ${review.benchmark_code} End`, benchmark.current) }));
    block.appendChild(element('p', { text: `Benchmark change ${formatPercent(metrics.benchmark_change)} · Excess return ${formatPercent(metrics.excess_return)}` }));
    for (const warning of benchmark.basis_warnings || []) {
      block.appendChild(element('p', { class: 'history-warning', text: `Benchmark basis note: ${warning}` }));
    }
  }
  const falsificationLabels = { unknown: 'Unknown', held: 'Falsification Conditions Not Triggered', breached: 'Falsification Conditions Triggered' };
  block.appendChild(element('p', {
    class: `falsification-status ${review.falsification_status || 'unknown'}`,
    text: `Falsification status: ${falsificationLabels[review.falsification_status] || 'Unknown'}`,
  }));
  if (review.falsification_notes) block.appendChild(element('p', { class: 'review-note', text: `Falsification notes: ${review.falsification_notes}` }));
  if (review.note) block.appendChild(element('p', { class: 'review-note', text: `Notes: ${review.note}` }));
  const limitations = Array.isArray(review.limitations) && review.limitations.length
    ? review.limitations
    : ['Share price change is not total shareholder return (TSR).', 'The review is a descriptive comparison and does not establish causality.'];
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
  const currentPrice = element('input', { type: 'number', name: 'current_price', min: '0', step: 'any', placeholder: 'Enter if local data is unavailable' });
  const currentDate = element('input', { type: 'date', name: 'current_price_date' });
  const currentSource = element('input', { type: 'text', name: 'current_price_source', placeholder: 'e.g. broker closing-price screenshot', maxlength: '500' });
  const benchmarkCode = element('input', { type: 'text', name: 'benchmark_code', placeholder: 'e.g. 000300', maxlength: '32' });
  const benchmarkBaselinePrice = element('input', { type: 'number', name: 'benchmark_baseline_price', min: '0', step: 'any', placeholder: 'Enter if local data is unavailable' });
  const benchmarkBaselineDate = element('input', { type: 'date', name: 'benchmark_baseline_date' });
  const benchmarkBaselineSource = element('input', { type: 'text', name: 'benchmark_baseline_source', placeholder: 'Benchmark start source', maxlength: '500' });
  const benchmarkCurrentPrice = element('input', { type: 'number', name: 'benchmark_current_price', min: '0', step: 'any', placeholder: 'Enter if local data is unavailable' });
  const benchmarkCurrentDate = element('input', { type: 'date', name: 'benchmark_current_date' });
  const benchmarkCurrentSource = element('input', { type: 'text', name: 'benchmark_current_source', placeholder: 'Benchmark end source', maxlength: '500' });
  const falsificationStatus = element('select', { name: 'falsification_status' }, [
    element('option', { value: 'unknown', text: 'Unknown' }),
    element('option', { value: 'held', text: 'Not Triggered' }),
    element('option', { value: 'breached', text: 'Triggered' }),
  ]);
  const falsificationNotes = element('textarea', { name: 'falsification_notes', placeholder: 'Describe which falsification conditions were tested or triggered', maxlength: '4000' });
  const noteInput = element('textarea', { name: 'note', placeholder: 'Optional review notes', maxlength: '2000' });
  const button = element('button', { type: 'submit', class: 'button secondary', text: state.reviewSubmitting ? 'Generating…' : 'Create Review' });
  button.disabled = state.reviewSubmitting || !state.runId || state.runMeta?.mode !== 'company';

  form.append(
    element('div', { class: 'review-form-title', text: 'Review Settings' }),
    reviewFormField('Review Date', dateInput),
    element('div', { class: 'review-form-title', text: 'Manual Current Price (when local data is unavailable)' }),
    reviewFormField('Current Price', currentPrice),
    reviewFormField('Price Date', currentDate),
    reviewFormField('Price Source', currentSource),
    element('div', { class: 'review-form-title', text: 'Optional Benchmark and Manual Inputs' }),
    reviewFormField('Benchmark Code', benchmarkCode),
    reviewFormField('Benchmark Start Price', benchmarkBaselinePrice),
    reviewFormField('Benchmark Start Date', benchmarkBaselineDate),
    reviewFormField('Benchmark Start Source', benchmarkBaselineSource),
    reviewFormField('Benchmark End Price', benchmarkCurrentPrice),
    reviewFormField('Benchmark End Date', benchmarkCurrentDate),
    reviewFormField('Benchmark End Source', benchmarkCurrentSource),
    element('div', { class: 'review-form-title', text: 'Falsification Check' }),
    reviewFormField('Falsification Status', falsificationStatus),
    reviewFormField('Falsification Notes', falsificationNotes),
    reviewFormField('Additional Notes', noteInput),
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
  return Number.isFinite(number) ? number.toLocaleString('en-US', { maximumFractionDigits: 2 }) : '—';
}

async function openArtifact(artifact) {
  $('modalTitle').textContent = artifact.name;
  clear($('modalBody'));
  $('modalBody').appendChild(element('div', { class: 'empty-state', text: 'Loading artifact…' }));
  $('modal').classList.remove('hidden');
  try {
    const data = await api.artifact(artifact.path);
    clear($('modalBody'));
    $('modalBody').appendChild(element('div', { class: 'artifact-meta' }, [
      element('span', { text: `Type ${data.kind}` }),
      data.size != null ? element('span', { text: `Size ${formatBytes(data.size)}` }) : null,
      data.mtime ? element('span', { text: `Modified ${data.mtime}` }) : null,
      data.truncated ? element('span', { text: 'Content truncated' }) : null,
    ]));
    const content = data.content;
    if (data.kind === 'json' || data.kind === 'jsonl') {
      $('modalBody').appendChild(element('pre', { class: 'artifact-json', text: JSON.stringify(content, null, 2) }));
    } else if (data.kind === 'md') {
      $('modalBody').appendChild(renderMarkdown(String(content || '')));
    } else if (data.kind === 'pdf') {
      $('modalBody').appendChild(element('p', { text: `Only PDF metadata is shown. Open the file from the filesystem: ${data.path}` }));
    } else {
      $('modalBody').appendChild(element('pre', { class: 'artifact-pre', text: String(content || '') }));
    }
  } catch (error) {
    clear($('modalBody'));
    $('modalBody').appendChild(element('div', { class: 'empty-state', text: `Load failed: ${error.message}` }));
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
    toast(`Historical decision failed to load: ${decisionResult.reason.message}`, 'warning');
  }

  if (reviewsResult.status === 'fulfilled') {
    state.reviews = reviewsResult.value.reviews || [];
    state.reviewWarnings = [...state.reviewWarnings, ...(reviewsResult.value.warnings || [])];
  } else if (!quiet) {
    toast(`Review records failed to load: ${reviewsResult.reason.message}`, 'warning');
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
    toast('Current review saved', 'good');
  } catch (error) {
    toast(`Failed to create review: ${error.message}`, 'critical', 6000);
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
    toast(`Run history failed to load: ${error.message}`, 'critical');
  }
}

function renderRunSelect() {
  const select = $('runSelect');
  clear(select);
  select.appendChild(element('option', { value: '', text: state.runs.length ? 'Select a historical run' : 'No runs yet' }));
  for (const run of state.runs) {
    const target = run.params?.company_name || run.params?.target || run.params?.stock_code || run.params?.industry_name || '';
    const info = statusInfo(run.status);
    const historyText = run.mode === 'company'
      ? ` · Baseline ${run.baseline_date || 'unavailable'} · Reviews ${run.review_count || 0}`
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
    toast(`Run failed to load: ${error.message}`, 'critical');
  }
}

async function startRun(mode, llmMode, params) {
  try {
    const response = await api.createRun({ mode, llm_mode: llmMode, params });
    await refreshRuns();
    await loadRun(response.run_id);
    toast(`Started ${modeName(mode)}`, 'good');
  } catch (error) {
    const existingId = error.status === 409 && error.body?.existing_run_id;
    if (existingId) {
      toast('A run for the same target already exists; reconnected to the active task', 'warning');
      await loadRun(existingId);
      return;
    }
    toast(`Failed to start: ${error.message}`, 'critical', 6000);
  }
}

function companyParams() {
  const target = $('companyTarget').value.trim();
  const params = {
    target,
    report_year: $('companyYear').value ? Number($('companyYear').value) : undefined,
    report_type: $('companyReport').value || undefined,
    filing_policy: $('companyReport').value ? 'single_filing' : 'recent_history',
    annual_lookback: 2,
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
    toast('Enter a company name or stock code first', 'warning');
    return;
  }
  toast('Reading research state…', 'info', 1800);
  try {
    const result = await api.audit(params);
    state.layers = {};
    for (const [key, value] of Object.entries(result.layers || {})) {
      state.layers[key] = typeof value === 'object' ? value.status : value;
    }
    renderLayers();
    const ready = Object.values(state.layers).filter((value) => value === 'ready').length;
    toast(`Audit complete: ${ready} layers ready`, 'good');
  } catch (error) {
    toast(`Audit failed: ${error.message}`, 'critical');
  }
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('research_console_theme', next); } catch (error) { /* 不影响使用 */ }
}

async function copyRunId() {
  if (!state.runId) {
    toast('No run_id is currently selected', 'warning');
    return;
  }
  try {
    await navigator.clipboard.writeText(state.runId);
    toast('run_id copied', 'good', 1800);
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
      toast('Run cancellation requested', 'warning');
    } catch (error) {
      toast(`Cancellation failed: ${error.message}`, 'critical');
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
    badge.querySelector('span').textContent = 'Service unavailable';
    return;
  }
  const warnings = [];
  if (!state.health.claude_cli_version) warnings.push('Claude CLI');
  if (!state.health.bocha_key_present) warnings.push('Bocha');
  badge.dataset.tone = warnings.length ? 'warning' : 'good';
  badge.querySelector('span').textContent = warnings.length ? `Limited: ${warnings.join(' / ')}` : `Service healthy · ${state.health.active_runs || 0} active runs`;
  badge.title = state.health.claude_cli_version || 'Claude CLI not detected';
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
