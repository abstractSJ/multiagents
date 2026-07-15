/**
 * game.js — “研究工坊”游戏舞台引擎
 *
 * 职责：
 *   在 Canvas 上以“代理小人 + 站点车间”的形式实时可视化多智能体
 *   投研流水线。接收 app.js 转发的 SSE 事件（dispatch），驱动小人
 *   状态机、交接/回流 token、彩带庆祝、镜头运动与站点交互。
 *
 * 对 app.js 暴露的接口（默认导出单例）：
 *   init(canvas, opts)       挂载画布；opts 可带 onStationClick/onSignClick 回调
 *   setTheme()               主题切换后刷新调色板缓存并重绘静态背景
 *   setPlan(steps, mode, extra) 依据步骤清单搭建舞台（站点/小人/复用蒙层）
 *   dispatch(event, opts)    消费一条运行事件；opts.instant=true 时跳过过场动画
 *   focusStation(owner)      镜头对准某角色的主站点
 *   resize()                 容器尺寸变化后重设画布（DPR 适配）
 *   setReducedMotion(bool)   开关减少动效模式（禁用走动/粒子/浮动）
 *   onStationClick(cb)       注册站点点击回调 cb({stationId, owner})
 *   onSignClick(cb)          注册“等待 Claude”牌子点击回调 cb(stepId)
 *   reset()                  清空舞台回到空闲状态
 */

import {
  lerp, clamp, ease, withAlpha, shade, roundRectPath,
  drawAgent, drawNameTag, drawStation, drawInboxTray, drawFileToken, drawGapToken,
  drawConfettiPiece, drawHalfMast, signHitRect,
} from './sprites.js';

/* ============================================================
 * 一、角色与步骤的静态映射表
 * ============================================================ */

/** 角色元信息：中文名 + 手持道具。颜色一律走 CSS 变量，避免双处维护。 */
const OWNER_META = {
  'orchestrator': { name: '调度官', prop: 'flag' },
  'information-collector': { name: '采集员', prop: 'folder' },
  'information-processor': { name: '处理员', prop: 'gear' },
  'financial-analyst': { name: '财务分析师', prop: 'calc' },
  'valuation-analyst': { name: '估值分析师', prop: 'scale' },
  'market-context-collector': { name: '市场情报员', prop: 'radar' },
  'industry-info-collector': { name: '行业采集员', prop: 'map' },
  'industry-researcher': { name: '行业研究员', prop: 'scope' },
};

/** step_id → 站点 id（公司链路 + 行业链路合并表）。 */
const STEP_STATION = {
  audit: 'dispatch',
  collector_fetch: 'collector',
  processor_parse: 'processor',
  processor_digest: 'processor',
  processor_rag: 'processor',
  processor_compare: 'processor',
  financial_evidence_draft: 'financial',
  formal_financial_analysis: 'financial',
  market_context_update: 'market',
  valuation_update: 'valuation',
  final_audit: 'deliver',
  deliver: 'deliver',
  industry_collect: 'industry_map',
  industry_validate: 'gate',
  industry_research: 'industry_lab',
  industry_deliver: 'deliver',
};

/** 解析车间四子工位：step_id → 灯位下标（解析/digest/RAG/比对）。 */
const PROCESSOR_LIGHT = {
  processor_parse: 0,
  processor_digest: 1,
  processor_rag: 2,
  processor_compare: 3,
};

/** 层状态中文名，用于站点悬浮 tooltip。 */
const LAYER_LABEL = {
  collector: '采集层',
  processor: '解析层',
  financial_evidence_draft: '证据草稿',
  formal_financial_analysis: '正式财务分析',
  valuation: '估值层',
  market_context: '市场上下文',
};

/** 步骤状态中文短语，用于 tooltip 内步骤概览。 */
const STEP_STATUS_TEXT = {
  pending: '待执行', running: '执行中', waiting: '等待LLM',
  done: '完成', degraded: '降级完成', failed: '失败', skipped: '跳过/复用',
};

/* ============================================================
 * 二、折线（走廊）几何工具
 * 为什么用折线而不是逐段贝塞尔：走廊是办公室地贴，直角转弯更有
 * “车间流水线”感；小人转弯处的生硬感恰好符合像素风格。
 * ============================================================ */

/** 预计算折线的分段长度与总长，返回 {pts, segLens, total}。 */
function buildPoly(pts) {
  const segLens = [];
  let total = 0;
  for (let i = 0; i < pts.length - 1; i++) {
    const d = Math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y);
    segLens.push(d); total += d;
  }
  return { pts, segLens, total };
}

/** 取折线上弧长 d 处的坐标（越界自动夹取端点）。 */
function polyPointAt(poly, d) {
  d = clamp(d, 0, poly.total);
  let acc = 0;
  for (let i = 0; i < poly.segLens.length; i++) {
    const L = poly.segLens[i];
    if (d <= acc + L || i === poly.segLens.length - 1) {
      const t = L === 0 ? 0 : (d - acc) / L;
      return {
        x: lerp(poly.pts[i].x, poly.pts[i + 1].x, t),
        y: lerp(poly.pts[i].y, poly.pts[i + 1].y, t),
      };
    }
    acc += L;
  }
  return poly.pts[poly.pts.length - 1];
}

/** 把任意点投影到折线上，返回 {d, x, y}（弧长参数 + 最近点）。 */
function polyProject(poly, p) {
  let best = { d: 0, x: poly.pts[0].x, y: poly.pts[0].y, dist: Infinity };
  let acc = 0;
  for (let i = 0; i < poly.pts.length - 1; i++) {
    const a = poly.pts[i], b = poly.pts[i + 1];
    const abx = b.x - a.x, aby = b.y - a.y;
    const L2 = abx * abx + aby * aby;
    const t = L2 === 0 ? 0 : clamp(((p.x - a.x) * abx + (p.y - a.y) * aby) / L2, 0, 1);
    const px = a.x + abx * t, py = a.y + aby * t;
    const dist = Math.hypot(p.x - px, p.y - py);
    if (dist < best.dist) best = { d: acc + Math.sqrt(L2) * t, x: px, y: py, dist };
    acc += poly.segLens[i];
  }
  return best;
}

/** 截取折线弧长 d0→d1 之间的点序列（方向随 d0/d1 大小自动反转）。 */
function polySlice(poly, d0, d1) {
  const rev = d1 < d0;
  const lo = Math.min(d0, d1), hi = Math.max(d0, d1);
  const out = [polyPointAt(poly, lo)];
  let acc = 0;
  for (let i = 0; i < poly.pts.length - 1; i++) {
    const end = acc + poly.segLens[i];
    if (end > lo && end < hi) out.push({ ...poly.pts[i + 1] });
    acc = end;
  }
  out.push(polyPointAt(poly, hi));
  if (rev) out.reverse();
  return out;
}

/* ============================================================
 * 三、Agent（小人）运行时对象
 * ============================================================ */

let agentSeq = 0;

/** 舞台上的一个角色小人：持有位置、状态机与行走路线。 */
class Agent {
  /**
   * @param owner   角色 id（事件里的 owner 字符串）
   * @param station 归属主站点对象
   */
  constructor(owner, station) {
    const meta = OWNER_META[owner] || { name: owner, prop: null };
    this.owner = owner;
    this.name = meta.name;
    this.prop = meta.prop;
    this.home = station;
    this.curStationId = station ? station.id : null;
    this.x = station ? station.workX : 0;
    this.y = station ? station.workY : 0;
    this.state = 'idle';
    this.stateT = 0;
    this.phase = (agentSeq++) * 1.7 % (Math.PI * 2); // 呼吸相位错开，避免全场同步起伏
    this.antenna = [(-1), 0, 1][agentSeq % 3];
    this.facing = 1;
    this.walkPhase = 0;
    this.blink = 0;
    this._blinkTimer = 2 + Math.random() * 3;
    this.progress = null;
    this.route = null;          // {poly, traveled, speed, delay, onArrive}
    this.bubble = null;         // {text, until} 回流原因等临时气泡
    this.signStepId = null;     // wait_llm 时对应的 step_id（牌子点击用）
    this.signRect = null;       // 牌子的世界坐标命中区
    this.cheerUntil = 0;
    this.reduced = false;
    this.color = '#888888';     // 每帧由调色板刷新
  }

  /** 切换状态并清零状态计时。 */
  setState(s) {
    if (this.state !== s) { this.state = s; this.stateT = 0; }
  }

  /**
   * 规划一段行走：给定路径点序列，按恒定速度移动，抵达后回调。
   * 速度按路程放大：跨越整个车间的长途也要在 ~3s 内到岗，
   * 避免“步骤都做完了小人还在路上”的脱节感。
   * reduced 模式下直接瞬移（保留业务语义、去掉运动过程）。
   */
  walkTo(pts, onArrive, delay = 0) {
    if (!pts || pts.length < 2) { if (onArrive) onArrive(); return; }
    if (this.reduced) {
      const end = pts[pts.length - 1];
      this.x = end.x; this.y = end.y; this.route = null;
      if (onArrive) onArrive();
      return;
    }
    const poly = buildPoly(pts);
    const speed = Math.max(380, poly.total / 2.8);
    this.route = { poly, traveled: 0, speed, delay, onArrive: onArrive || null };
    this.setState('walk');
  }

  /** 每帧推进：行走、眨眼、状态自衰减。 */
  update(dt, time) {
    this.stateT += dt;

    // 眨眼计时：随机 2~5s 眨一次，闭眼 0.12s
    this._blinkTimer -= dt;
    if (this._blinkTimer < 0) { this._blinkTimer = 2 + Math.random() * 3; }
    this.blink = this._blinkTimer < 0.12 ? 1 : 0;

    // 行走推进
    if (this.route) {
      const r = this.route;
      if (r.delay > 0) { r.delay -= dt; return; }
      const before = polyPointAt(r.poly, r.traveled);
      r.traveled += r.speed * dt;
      const now = polyPointAt(r.poly, r.traveled);
      if (Math.abs(now.x - before.x) > 0.1) this.facing = now.x >= before.x ? 1 : -1;
      this.x = now.x; this.y = now.y;
      this.walkPhase += dt * 9;
      if (r.traveled >= r.poly.total) {
        this.route = null;
        const cb = r.onArrive;
        this.setState('idle');
        if (cb) cb();
      }
      return;
    }

    // done 欢呼自衰减回 idle（run 完成时的集体欢呼由 cheerUntil 控制时长）
    if (this.state === 'done') {
      const hold = this.cheerUntil > time ? 3.2 : 1.8;
      if (this.stateT > hold) this.setState('idle');
    }
    // 临时气泡到期清理
    if (this.bubble && time > this.bubble.until) this.bubble = null;
  }
}

/* ============================================================
 * 四、布局构建（公司 / 行业两张地图）
 * ============================================================ */

/**
 * 公司链路布局：7 站点 S 形动线（大比例紧凑版）。
 * 上排自左向右（调度台→采集→解析→财务），右侧下行，
 * 下排自右向左（估值→交付）；市场雷达站抬升为“夹层”悬浮平台。
 * 夹层与估值室的关联不再画静态支路，由运行期的流转轨迹动态表达。
 * 走廊折线仅作为小人步行路径存在，不再绘制任何地贴。
 */
function buildCompanyLayout() {
  const mk = (id, kind, owner, x, y, label, layers) => ({
    id, kind, owner, x, y, label,
    layers: layers || [],
    // 小人站位：站台前方 54px，给放大后的桌台留出纵深
    workX: x, workY: y + 54,
    active: false, dim: false, failed: false, waiting: false,
    subLights: kind === 'processor' ? [0, 0, 0, 0] : null,
    digestProgress: null,
    tray: { count: 0 },
  });
  const stations = [
    mk('dispatch', 'orchestrator', 'orchestrator', 230, 400, '调度台', []),
    mk('collector', 'collector', 'information-collector', 640, 400, '资料采集站', ['collector']),
    mk('processor', 'processor', 'information-processor', 1050, 400, '解析车间', ['processor']),
    mk('financial', 'financial', 'financial-analyst', 1460, 400, '财务分析室', ['financial_evidence_draft', 'formal_financial_analysis']),
    mk('market', 'market', 'market-context-collector', 860, 140, '市场雷达站', ['market_context']),
    mk('valuation', 'valuation', 'valuation-analyst', 1460, 760, '估值室', ['valuation']),
    mk('deliver', 'deliver', 'orchestrator', 1050, 760, '交付台', []),
  ];
  const corridor = buildPoly([
    { x: 80, y: 515 }, { x: 230, y: 515 }, { x: 640, y: 515 }, { x: 1050, y: 515 },
    { x: 1460, y: 515 }, { x: 1630, y: 515 }, { x: 1630, y: 875 },
    { x: 1460, y: 875 }, { x: 1050, y: 875 }, { x: 940, y: 875 },
  ]);
  // 市场情报员入场路线：沿左墙上到夹层（避免穿越上排桌面）
  const marketEntry = [{ x: 80, y: 515 }, { x: 160, y: 515 }, { x: 160, y: 194 }, { x: 760, y: 194 }, { x: 860, y: 194 }];
  return {
    mode: 'company', stations, corridor, marketEntry,
    worldW: 1780, worldH: 990, door: { x: 80, y: 515 },
  };
}

/**
 * 行业链路布局：4 站点一字动线（大比例紧凑版）。
 * 地图桌（行业采集）→ 验证闸门（发光 Gate）→ 研究室 → 交付台。
 */
function buildIndustryLayout() {
  const mk = (id, kind, owner, x, y, label, layers) => ({
    id, kind, owner, x, y, label, layers: layers || [],
    workX: x, workY: y + 54,
    active: false, dim: false, failed: false, waiting: false,
    subLights: null, digestProgress: null,
    tray: { count: 0 },
  });
  const stations = [
    mk('industry_map', 'industry_map', 'industry-info-collector', 260, 430, '行业地图桌', []),
    mk('gate', 'gate', 'industry-info-collector', 700, 430, '验证闸门', []),
    mk('industry_lab', 'industry_research', 'industry-researcher', 1140, 430, '行业研究室', []),
    mk('deliver', 'deliver', 'orchestrator', 1560, 430, '交付台', []),
  ];
  const corridor = buildPoly([
    { x: 100, y: 545 }, { x: 260, y: 545 }, { x: 700, y: 545 },
    { x: 1140, y: 545 }, { x: 1560, y: 545 }, { x: 1680, y: 545 },
  ]);
  return {
    mode: 'industry', stations, corridor, marketEntry: null,
    worldW: 1780, worldH: 700, door: { x: 100, y: 545 },
  };
}

/* ============================================================
 * 五、游戏单例
 * ============================================================ */

const game = {
  /* —— 挂载与环境 —— */
  canvas: null, ctx: null,
  cssW: 0, cssH: 0, dpr: 1,
  running: false, lastFrame: 0, time: 0,
  reduced: false,
  paused: false,

  /* —— 主题调色板缓存 —— */
  pal: null,

  /* —— 场景状态 —— */
  layout: null,
  agents: new Map(),          // owner → Agent
  stepMap: new Map(),         // step_id → {owner, stationId, status, title, kind}
  stepOrder: [],
  layerStatuses: {},
  reusable: {},
  traceMode: null,
  runStatus: null,            // running | completed | partial | failed | cancelled
  runtimeJobs: new Map(),     // invocation_id → {owner,title,status,currentTool,...}
  activeCoordinatorAgents: new Map(), // owner → Set<invocation id>，支持同角色并发实例
  activeCoordinatorTools: new Map(),  // owner → Set<tool id>，工具完成不能抢先结束 Agent
  flagState: null,            // full | half | null
  veil: 0, veilTarget: 0,

  /* —— 效果对象池 —— */
  tokens: [],                 // {kind:'file'|'gap', p0,p1,cp1,cp2,t,dur,onArrive,trace}
  confetti: [],
  floaties: [],               // {x,y,text,color,life}

  /* —— 流转轨迹层 —— */
  traces: [],                 // 见 §六 _spawnToken 内联结构说明
  traceKeys: new Map(),       // "from>to>kind" → trace 对象，用于同对交接合并加粗
  dashPhase: 0,               // 全局流动短划线相位（随时间累加）

  /* —— 相机 —— */
  cam: { x: 800, y: 430, zoom: 0.8, tx: 800, ty: 430, userLockUntil: 0 },

  /* —— 交互 —— */
  hoverStationId: null,
  hoverSign: false,
  pointer: { down: false, moved: false, sx: 0, sy: 0, camX: 0, camY: 0, lastX: 0, lastY: 0 },
  cbStationClick: null,
  cbSignClick: null,

  /* —— 静态背景离屏层 —— */
  bgCanvas: null, bgDirty: true,
  /** 离屏背景的超采样倍率：2 倍保证 dpr≤2、zoom≤1.6 下地贴文字仍清晰。 */
  BG_SCALE: 2,

  /* ---------------------------------------------------------
   * 生命周期
   * --------------------------------------------------------- */

  /**
   * 初始化舞台。
   * @param canvas HTMLCanvasElement
   * @param opts   {onStationClick?, onSignClick?}
   */
  init(canvas, opts = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.cbStationClick = opts.onStationClick || null;
    this.cbSignClick = opts.onSignClick || null;
    this.refreshPalette();
    this.resize();
    this._bindPointer();

    // 标签页隐藏时暂停 rAF，避免空转耗电；恢复时重置时间基准防跳帧
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { this.paused = true; }
      else { this.paused = false; this.lastFrame = performance.now(); }
    });

    // 容器尺寸变化自动重设画布（app 也可以显式调 resize，二者幂等）
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => this.resize()).observe(canvas.parentElement || canvas);
    }

    this.running = true;
    this.lastFrame = performance.now();
    requestAnimationFrame((ts) => this._loop(ts));
  },

  /** 主题切换：重读 CSS 变量并标记静态背景重绘。 */
  setTheme() {
    this.refreshPalette();
    this.bgDirty = true;
  },

  /**
   * 从文档根元素读取 CSS 自定义属性，缓存为调色板。
   * 为什么缓存：getComputedStyle 每帧调用开销大，只在主题切换时刷新。
   */
  refreshPalette() {
    const cs = getComputedStyle(document.documentElement);
    const v = (name, fb) => (cs.getPropertyValue(name) || '').trim() || fb;
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    this.pal = {
      dark,
      page: v('--bg-page', '#f9f9f7'),
      card: v('--bg-card', '#fcfcfb'),
      fg: v('--fg', '#0b0b0b'),
      mut: v('--fg-mut', '#52514e'),
      weak: v('--fg-weak', '#898781'),
      grid: v('--line-grid', '#e1e0d9'),
      base: v('--line-base', '#c3c2b7'),
      border: v('--border', 'rgba(11,11,11,.1)'),
      inset: dark ? 'rgba(255,255,255,.08)' : 'rgba(11,11,11,.06)',
      good: v('--status-good', '#0ca30c'),
      warning: v('--status-warning', '#fab219'),
      serious: v('--status-serious', '#ec835a'),
      critical: v('--status-critical', '#d03b3b'),
      seq1: v('--seq-1', '#86b6ef'), seq2: v('--seq-2', '#5598e7'),
      seq3: v('--seq-3', '#2a78d6'), seq4: v('--seq-4', '#1c5cab'),
      stageFloor: v('--stage-floor', '#f3f2ee'),
      stageWall: v('--stage-wall', '#ecebe5'),
      stageShadow: dark ? 'rgba(0,0,0,.45)' : 'rgba(11,11,11,.1)',
      // 小人脸色与眼睛：不入主题变量，按明暗微调保持亲和
      faceFill: dark ? '#e9dcc8' : '#fdf0de',
      eye: '#26241f',
      roles: {},
      role(owner) { return this.roles[owner] || this.weak; },
    };
    for (const owner of Object.keys(OWNER_META)) {
      this.pal.roles[owner] = v(`--role-${owner}`, '#888888');
    }
  },

  /** 画布尺寸与 DPR 适配：CSS 尺寸 × devicePixelRatio 作为位图尺寸。 */
  resize() {
    if (!this.canvas) return;
    const rect = this.canvas.getBoundingClientRect();
    this.cssW = Math.max(80, rect.width);
    this.cssH = Math.max(80, rect.height);
    this.dpr = Math.min(window.devicePixelRatio || 1, 2.5);
    this.canvas.width = Math.round(this.cssW * this.dpr);
    this.canvas.height = Math.round(this.cssH * this.dpr);
  },

  /** 减少动效模式：同步到所有小人并即时结束在途动画。 */
  setReducedMotion(flag) {
    this.reduced = !!flag;
    for (const a of this.agents.values()) {
      a.reduced = this.reduced;
      if (this.reduced && a.route) {
        // 直接落到路线终点，保持业务位置正确
        const end = a.route.poly.pts[a.route.poly.pts.length - 1];
        const cb = a.route.onArrive;
        a.x = end.x; a.y = end.y; a.route = null; a.setState('idle');
        if (cb) cb();
      }
    }
    if (this.reduced) { this.tokens = []; this.confetti = []; this.floaties = []; }
  },

  /** 注册站点点击回调。 */
  onStationClick(cb) { this.cbStationClick = cb; },
  /** 注册等待牌点击回调。 */
  onSignClick(cb) { this.cbSignClick = cb; },

  /** 清空舞台：新 run 开始或切换观看对象时调用。 */
  reset() {
    this.layout = null;
    this.agents.clear();
    this.stepMap.clear();
    this.stepOrder = [];
    this.layerStatuses = {};
    this.reusable = {};
    this.traceMode = null;
    this.runStatus = null;
    this.runtimeJobs.clear();
    this.activeCoordinatorAgents.clear();
    this.activeCoordinatorTools.clear();
    this.flagState = null;
    this.veil = 0; this.veilTarget = 0;
    this.tokens = []; this.confetti = []; this.floaties = [];
    this.traces = []; this.traceKeys.clear();
    this.hoverStationId = null;
    this.bgDirty = true;
  },

  /* ---------------------------------------------------------
   * 计划装配
   * --------------------------------------------------------- */

  /**
   * 依据步骤清单搭建舞台。
   * @param steps  plan_ready.payload.steps
   * @param mode   run 模式（demo/replay 也会依据步骤自动识别地图形态）
   * @param extra  {layer_statuses?, reusable?, instant?}
   */
  setPlan(steps, mode, extra = {}) {
    // 地图形态以步骤内容为准：demo/replay 播放的公司步骤同样使用公司地图
    const isIndustry = (steps || []).some((s) => String(s.step_id || '').startsWith('industry'));
    this.layout = isIndustry ? buildIndustryLayout() : buildCompanyLayout();
    this.bgDirty = true;
    this.tokens = []; this.confetti = []; this.floaties = [];
    this.traces = []; this.traceKeys.clear();
    this.veil = 0; this.veilTarget = 0;
    this.flagState = null;
    this.runStatus = 'running';
    this.traceMode = extra.trace_mode || null;
    this.runtimeJobs.clear();
    this.activeCoordinatorAgents.clear();
    this.activeCoordinatorTools.clear();

    // —— 建立步骤索引 —— //
    this.stepMap.clear();
    this.stepOrder = [];
    for (const s of steps || []) {
      const stationId = STEP_STATION[s.step_id] || this._stationOfOwner(s.owner) || 'dispatch';
      this.stepMap.set(s.step_id, {
        stepId: s.step_id,
        owner: s.owner,
        stationId,
        status: s.status === 'skipped' ? 'skipped' : 'pending',
        title: s.title || s.step_id,
        kind: s.kind || 'script',
        skipReason: s.skip_reason || '',
      });
      this.stepOrder.push(s.step_id);
    }

    if (extra.layer_statuses) this.layerStatuses = { ...extra.layer_statuses };
    if (extra.reusable) this.reusable = { ...extra.reusable };
    if (extra.milestone_states) this._applyMilestoneStates(extra.milestone_states);

    // —— 计划期就跳过的站点盖“复用”蒙层，子工位灯直接点绿 —— //
    for (const st of this.layout.stations) {
      const stSteps = this.stepOrder.map((id) => this.stepMap.get(id)).filter((x) => x.stationId === st.id);
      st.dim = stSteps.length > 0 && stSteps.every((x) => x.status === 'skipped');
      if (st.subLights) {
        for (const x of stSteps) {
          const li = PROCESSOR_LIGHT[x.stepId];
          if (li != null && x.status === 'skipped') st.subLights[li] = 1;
        }
      }
    }

    // —— 生成小人：计划涉及的 owner + 调度官必到场 —— //
    this.agents.clear();
    const owners = new Set(['orchestrator']);
    for (const s of steps || []) if (s.owner) owners.add(s.owner);
    let idx = 0;
    for (const owner of owners) {
      const home = this._homeStation(owner);
      if (!home) continue;
      const agent = new Agent(owner, home);
      agent.reduced = this.reduced;
      this.agents.set(owner, agent);

      const allSkipped = home.dim;
      const finalize = () => {
        if (allSkipped) { agent.setState('sleep'); agent.facing = 1; }
      };
      if (extra.instant || this.reduced) {
        agent.x = home.workX; agent.y = home.workY; finalize();
      } else {
        // 入场小戏：从大门鱼贯走向各自岗位，间隔 0.12s 依次出发
        agent.x = this.layout.door.x; agent.y = this.layout.door.y;
        const route = this._entryRoute(home);
        agent.walkTo(route, finalize, idx * 0.12);
      }
      idx++;
    }

    this.cam.userLockUntil = 0;
    this._fitCamera();
  },

  /** 某 owner 的主站点（orchestrator 归位调度台/交付台首个匹配）。 */
  _homeStation(owner) {
    if (!this.layout) return null;
    return this.layout.stations.find((s) => s.owner === owner) || null;
  },

  /** owner → 站点 id（用于未知 step 的兜底映射）。 */
  _stationOfOwner(owner) {
    if (!this.layout) return null;
    const st = this.layout.stations.find((s) => s.owner === owner);
    return st ? st.id : null;
  },

  /** 从大门到某站点的入场路线（市场雷达站走专用夹层楼梯路线）。 */
  _entryRoute(station) {
    const L = this.layout;
    if (station.id === 'market' && L.marketEntry) {
      return L.marketEntry.map((p) => ({ ...p }));
    }
    const proj = polyProject(L.corridor, { x: station.workX, y: station.workY });
    const doorProj = polyProject(L.corridor, L.door);
    const pts = polySlice(L.corridor, doorProj.d, proj.d);
    pts.unshift({ ...L.door });
    pts.push({ x: station.workX, y: station.workY });
    return pts;
  },

  /** 任意当前位置 → 目标站点的走廊路线。 */
  _routeToStation(agent, station) {
    const L = this.layout;
    if (!L) return [{ x: agent.x, y: agent.y }, { x: station.workX, y: station.workY }];
    if (station.id === 'market') {
      // 夹层站点：直线斜插（仅发生在极少数兜底场景）
      return [{ x: agent.x, y: agent.y }, { x: station.workX, y: station.workY }];
    }
    const from = polyProject(L.corridor, { x: agent.x, y: agent.y });
    const to = polyProject(L.corridor, { x: station.workX, y: station.workY });
    const pts = polySlice(L.corridor, from.d, to.d);
    pts.unshift({ x: agent.x, y: agent.y });
    pts.push({ x: station.workX, y: station.workY });
    return pts;
  },

  /* ---------------------------------------------------------
   * 事件消费
   * --------------------------------------------------------- */

  /**
   * 消费一条运行事件，驱动舞台演出。
   * @param ev   SSE 事件对象 {type, step_id?, owner?, payload}
   * @param opts {instant?:boolean} 快照恢复时置 true，跳过过场动画
   */
  dispatch(ev, opts = {}) {
    const instant = !!opts.instant;
    const type = ev.type;
    const payload = ev.payload || {};

    if (type === 'run_started') {
      // 舞台在 plan_ready 时才真正搭建；这里只复位收场状态
      this.runStatus = 'running';
      this.veilTarget = 0;
      return;
    }
    if (type === 'plan_ready') {
      this.setPlan(payload.steps || [], null, {
        layer_statuses: payload.layer_statuses,
        reusable: payload.reusable,
        milestone_states: payload.milestone_states,
        trace_mode: payload.trace_mode,
        instant,
      });
      return;
    }
    if (!this.layout) return; // 未搭台前的其他事件（异常序列）安全忽略

    const step = ev.step_id ? this.stepMap.get(ev.step_id) : null;
    const station = step ? this._station(step.stationId) : null;
    const agent = step ? this.agents.get(step.owner) : (ev.owner ? this.agents.get(ev.owner) : null);

    switch (type) {
      case 'coordinator_session_started': {
        const orchestrator = this.agents.get('orchestrator');
        const dispatch = this._station('dispatch');
        if (orchestrator && dispatch) {
          dispatch.active = true;
          this._sendAgentToWork(orchestrator, dispatch, instant);
        }
        break;
      }
      case 'agent_started': {
        const owner = payload.agent_name || ev.owner;
        const dynamicAgent = this.agents.get(owner);
        const dynamicStation = this._homeStation(owner);
        if (!dynamicAgent || !dynamicStation) break;
        const invocationId = payload.invocation_id || payload.tool_use_id || payload.runtime_task_id || `${owner}:anonymous`;
        const active = this.activeCoordinatorAgents.get(owner) || new Set();
        active.add(invocationId);
        this.activeCoordinatorAgents.set(owner, active);
        this.runtimeJobs.set(invocationId, {
          invocationId,
          owner,
          title: payload.description || '执行子任务',
          workItemId: payload.work_item_id || '',
          status: 'running',
          currentTool: '',
          summary: '',
          artifactCount: 0,
          updatedAt: this.time,
        });
        dynamicStation.active = true;
        dynamicStation.failed = false;
        dynamicStation.waiting = false;
        if (dynamicStation.dim) dynamicStation.dim = false;
        this._sendAgentToWork(dynamicAgent, dynamicStation, instant);
        this._focusCam(dynamicStation);
        break;
      }
      case 'agent_completed': {
        const owner = payload.agent_name || ev.owner;
        const dynamicAgent = this.agents.get(owner);
        const dynamicStation = this._homeStation(owner);
        const invocationId = payload.invocation_id || payload.tool_use_id || payload.runtime_task_id || `${owner}:anonymous`;
        const active = this.activeCoordinatorAgents.get(owner);
        if (active) {
          active.delete(invocationId);
          if (!active.size) this.activeCoordinatorAgents.delete(owner);
        }
        const job = this.runtimeJobs.get(invocationId);
        if (job) {
          job.status = payload.is_error ? 'failed' : 'completed';
          job.summary = payload.summary || '';
          job.currentTool = '';
          job.updatedAt = this.time;
        }
        if (!dynamicAgent || !dynamicStation) break;
        const stillActive = (this.activeCoordinatorAgents.get(owner) || new Set()).size > 0
          || (this.activeCoordinatorTools.get(owner) || new Set()).size > 0;
        dynamicStation.failed = !!payload.is_error && !stillActive;
        dynamicStation.active = stillActive;
        dynamicAgent.progress = null;
        dynamicAgent.signStepId = null;
        dynamicAgent.setState(stillActive ? 'work' : (payload.is_error ? 'blocked' : 'done'));
        this._focusCam(dynamicStation);
        break;
      }
      case 'work_item_upsert': {
        const orchestrator = this.agents.get('orchestrator');
        const dispatch = this._station('dispatch');
        if (orchestrator && dispatch && payload.status === 'in_progress') {
          dispatch.active = true;
          orchestrator.bubble = { text: String(payload.active_form || payload.title || '调度任务').slice(0, 30), until: this.time + 4 };
          this._sendAgentToWork(orchestrator, dispatch, instant);
        }
        break;
      }
      case 'tool_activity': {
        const owner = ev.owner || payload.agent_name || 'orchestrator';
        const toolId = payload.tool_use_id || `${payload.invocation_id || owner}:${payload.tool_name || 'tool'}`;
        const toolSet = this.activeCoordinatorTools.get(owner) || new Set();
        if (payload.phase === 'completed') toolSet.delete(toolId);
        else if (!payload.inferred) toolSet.add(toolId);
        if (toolSet.size) this.activeCoordinatorTools.set(owner, toolSet);
        else this.activeCoordinatorTools.delete(owner);

        const invocationId = payload.invocation_id || '';
        const job = invocationId ? this.runtimeJobs.get(invocationId) : null;
        if (job) {
          job.currentTool = payload.phase === 'completed' ? '' : (payload.tool_name || 'tool');
          if (payload.is_error) job.status = 'failed';
          job.updatedAt = this.time;
        }
        const toolAgent = this.agents.get(owner);
        const toolStation = this._homeStation(owner);
        if (toolAgent && toolStation && payload.phase !== 'completed') {
          toolStation.active = true;
          toolStation.failed = !!payload.is_error;
          this._sendAgentToWork(toolAgent, toolStation, instant);
        } else if (toolAgent && toolStation && payload.phase === 'completed') {
          const stillActive = (this.activeCoordinatorAgents.get(owner) || new Set()).size > 0
            || (this.activeCoordinatorTools.get(owner) || new Set()).size > 0;
          toolStation.active = stillActive;
          if (!stillActive && toolAgent.state === 'work') toolAgent.setState(payload.is_error ? 'blocked' : 'idle');
        }
        break;
      }
      case 'handoff': {
        const fromSt = payload.from_station
          ? this._station(payload.from_station)
          : this._homeStation(payload.from_owner);
        const toSt = payload.to_station
          ? this._station(payload.to_station)
          : this._homeStation(payload.to_owner);
        if (fromSt && toSt && fromSt.id !== toSt.id) {
          this._recordHandoff(fromSt, toSt, instant, {
            kind: payload.kind || 'delivery',
            label: payload.label || payload.description || payload.summary || '',
            invocationId: payload.invocation_id || '',
            workItemId: payload.work_item_id || '',
            isError: !!payload.is_error,
          });
          this._focusCam(toSt);
        }
        break;
      }
      case 'artifact_created': {
        const producer = payload.producer_owner || ev.owner;
        const fromSt = this._homeStation(producer);
        const toSt = this._homeStation(payload.delivery_to || 'orchestrator');
        if (fromSt && fromSt.tray) fromSt.tray.count += 1;
        if (fromSt && toSt && fromSt.id !== toSt.id && this.traceMode === 'runtime') {
          this._recordHandoff(fromSt, toSt, instant, {
            kind: 'artifact',
            label: payload.name || '研究产物',
          });
        }
        const activeIds = this.activeCoordinatorAgents.get(producer) || new Set();
        for (const invocationId of activeIds) {
          const job = this.runtimeJobs.get(invocationId);
          if (job) job.artifactCount = (job.artifactCount || 0) + 1;
        }
        break;
      }
      case 'step_started': {
        if (!step) return;
        step.status = 'running';
        if (station) {
          station.active = true; station.failed = false; station.waiting = false;
          if (station.dim) station.dim = false; // 计划复用但被强制重跑：撤掉蒙层
          const li = PROCESSOR_LIGHT[step.stepId];
          if (station.subLights && li != null) station.subLights[li] = 2;
          if (step.stepId === 'processor_digest') station.digestProgress = 0;
        }
        if (agent && station) {
          agent.progress = null;
          agent.signStepId = null;
          this._sendAgentToWork(agent, station, instant);
        }
        if (station) this._focusCam(station);
        break;
      }
      case 'step_progress': {
        if (agent) agent.progress = { done: payload.done || 0, total: payload.total || 0 };
        if (station && step.stepId === 'processor_digest' && payload.total > 0) {
          station.digestProgress = clamp(payload.done / payload.total, 0, 1);
        }
        break;
      }
      case 'step_waiting_llm': {
        if (!step) return;
        step.status = 'waiting';
        if (station) { station.active = true; station.waiting = true; if (station.dim) station.dim = false; }
        if (agent && station) {
          this._placeAgent(agent, station, instant, () => {
            agent.setState('wait_llm');
            agent.signStepId = step.stepId;
          });
        }
        if (station) this._focusCam(station);
        break;
      }
      case 'step_completed': {
        if (!step) return;
        step.status = payload.degraded ? 'degraded' : 'done';
        if (agent) { agent.progress = null; agent.signStepId = null; }
        let stillBusy = false;
        if (station) {
          station.waiting = false;
          const li = PROCESSOR_LIGHT[step.stepId];
          if (station.subLights && li != null) station.subLights[li] = 1;
          if (step.stepId === 'processor_digest') station.digestProgress = null;
          // 站内还有其他进行中的步骤时保持活跃（如 digest 与 RAG 并行）。
          // 小人也必须继续工作，不能因其中一个子步骤先完成就提前欢呼。
          stillBusy = this._stationSteps(station.id).some((x) => x.status === 'running' || x.status === 'waiting');
          if (!stillBusy) station.active = false;
        }
        if (payload.degraded && station && !instant) {
          this._floaty(station.x, station.y - 140, '降级完成', this.pal.warning);
        }
        // instant 只关闭过场，不得阻止语义收尾；历史恢复/跳尾后不能让角色
        // 永久停在 work。实时播放保留短暂 done 欢呼，快照直接归 idle。
        if (agent) agent.setState(stillBusy ? 'work' : (instant ? 'idle' : 'done'));
        // 交接留痕：文件 token 沿贝塞尔飞向下一站，飞过之处渐进显形一条
        // 常驻轨迹；instant/reduced 场景不飞行但轨迹与托盘照常落账，
        // 保证回放跳尾、快照恢复后“流程图”完整一致。
        if (station && this.traceMode !== 'runtime') {
          const nextSt = this._nextStationAfter(step);
          if (nextSt && nextSt.id !== station.id) {
            this._recordHandoff(station, nextSt, instant, { kind: 'legacy', label: step.title });
          }
        }
        break;
      }
      case 'step_failed': {
        if (!step) return;
        step.status = 'failed';
        if (station) { station.failed = true; station.active = true; station.waiting = false; }
        if (agent) { agent.setState('blocked'); agent.progress = null; agent.signStepId = null; }
        if (station) this._focusCam(station);
        break;
      }
      case 'step_skipped': {
        if (!step) return;
        step.status = 'skipped';
        if (agent) { agent.progress = null; agent.signStepId = null; }
        if (station) {
          station.waiting = false;
          const li = PROCESSOR_LIGHT[step.stepId];
          if (station.subLights && li != null) station.subLights[li] = 1;
          const stSteps = this._stationSteps(station.id);
          const allSkipped = stSteps.every((x) => x.status === 'skipped');
          const stillBusy = stSteps.some((x) => x.status === 'running' || x.status === 'waiting');
          if (!stillBusy) station.active = false;
          if (allSkipped) {
            station.dim = true;
            if (agent && agent.curStationId === station.id) {
              // 整站复用：在途则改写到站回调（走完再睡），否则就地安置入睡
              if (agent.route) agent.route.onArrive = () => agent.setState('sleep');
              else this._placeAgent(agent, station, true, () => agent.setState('sleep'));
            }
          } else if (!instant) {
            this._floaty(station.x, station.y - 140, '跳过', this.pal.weak);
            if (agent && agent.state === 'wait_llm') agent.setState('idle');
          } else if (agent && agent.state === 'wait_llm') {
            agent.setState('idle');
          }
        }
        break;
      }
      case 'backflow': {
        // 红色缺口 token 从 from_step 站点逆向飞往 to_owner 的主站点，
        // 并留下一条反向弯曲的红色虚线回流轨迹（与正向轨迹区分）
        const fromStep = this.stepMap.get(payload.from_step);
        const fromSt = fromStep ? this._station(fromStep.stationId) : null;
        const toAgent = this.agents.get(payload.to_owner);
        const toSt = toAgent ? this._homeStation(payload.to_owner) : null;
        const reason = String(payload.reason || '').slice(0, 40);
        if (fromSt && toSt) {
          this._recordBackflow(fromSt, toSt, instant, () => {
            if (toAgent) {
              // 抬头接住：短促起跳 + 头顶原因气泡
              toAgent.setState('done'); toAgent.stateT = 0.55;
              toAgent.bubble = { text: reason || '上游补证请求', until: this.time + 4 };
            }
          });
        } else if (toAgent && !instant) {
          toAgent.bubble = { text: reason || '上游补证请求', until: this.time + 4 };
        }
        if (toSt) this._focusCam(toSt);
        break;
      }
      case 'state_refreshed': {
        if (payload.layer_statuses) this.layerStatuses = { ...this.layerStatuses, ...payload.layer_statuses };
        if (payload.reusable) this.reusable = { ...this.reusable, ...payload.reusable };
        if (payload.milestone_states) this._applyMilestoneStates(payload.milestone_states);
        break;
      }
      case 'run_completed': {
        this.runStatus = payload.status || 'completed';
        this.activeCoordinatorAgents.clear();
        this.activeCoordinatorTools.clear();
        for (const job of this.runtimeJobs.values()) {
          if (job.status === 'running') job.status = this.runStatus === 'cancelled' ? 'cancelled' : 'completed';
          job.currentTool = '';
          job.updatedAt = this.time;
        }
        for (const st of this.layout.stations) { st.active = false; st.waiting = false; }
        // 终态必须清掉所有在途路线与工作标记；否则旧 onArrive 回调会在跳尾后
        // 再次把角色改回 work，造成“运行已结束但小人仍忙碌”。
        for (const a of this.agents.values()) {
          a.route = null;
          a.progress = null;
          a.signStepId = null;
          if (a.state !== 'sleep') a.setState('idle');
        }
        const deliverSt = this._station('deliver') || this.layout.stations[this.layout.stations.length - 1];
        if (this.runStatus === 'completed') {
          this.flagState = 'full';
          this.veilTarget = 0;
          if (!instant) {
            // reduced-motion 只关闭彩带与运动过程，静态完成状态仍必须可见。
            if (!this.reduced) this._spawnConfetti(deliverSt.x, deliverSt.y - 80);
            for (const a of this.agents.values()) {
              if (a.state !== 'sleep') { a.setState('done'); a.cheerUntil = this.time + 3; }
            }
          }
          this._focusCam(deliverSt);
        } else if (this.runStatus === 'partial') {
          this.flagState = 'half';
          this.veilTarget = 0;
          if (!instant) this._floaty(deliverSt.x, deliverSt.y - 150, '部分交付 · 带缺口', this.pal.warning);
          this._focusCam(deliverSt);
        } else {
          // failed / cancelled：灰色收场
          this.veilTarget = 0.32;
        }
        if (instant) this.veil = this.veilTarget;
        break;
      }
      case 'run_error': {
        // 诊断事件不是终态；是否灰幕只由后续 run_completed 决定。
        break;
      }
      default:
        break; // step_log / artifact_created 等纯信息事件不驱动舞台
    }
  },

  /**
   * 把小人安置到站点后执行回调（统一处理三种情形）：
   * instant/reduced → 瞬移；距离近 → 原地就位；否则走廊步行前往。
   * 关键点：瞬移必须清掉在途 route，否则旧路线会在下一帧把小人拽回去。
   */
  _placeAgent(agent, station, instant, then) {
    agent.curStationId = station.id;
    const far = Math.hypot(agent.x - station.workX, agent.y - station.workY) > 6;
    if (instant || this.reduced || !far) {
      agent.route = null;
      agent.x = station.workX; agent.y = station.workY;
      agent.facing = 1;
      then();
    } else {
      agent.walkTo(this._routeToStation(agent, station), then);
    }
  },

  /** 让小人走到站点并进入工作姿态（instant 时瞬移）。 */
  _sendAgentToWork(agent, station, instant) {
    this._placeAgent(agent, station, instant, () => {
      agent.facing = station.x >= agent.x ? 1 : -1;
      agent.setState('work');
    });
  },

  /** 取站点对象。 */
  _station(id) { return this.layout ? this.layout.stations.find((s) => s.id === id) : null; },

  /** 某站点上的全部步骤运行时对象。 */
  _stationSteps(stationId) {
    return this.stepOrder.map((id) => this.stepMap.get(id)).filter((x) => x && x.stationId === stationId);
  },

  /** audit 快照只更新交付就绪灯，不制造执行动画或虚构交接顺序。 */
  _applyMilestoneStates(states) {
    for (const [stepId, milestone] of Object.entries(states || {})) {
      const step = this.stepMap.get(stepId);
      if (!step) continue;
      step.status = milestone.run_status === 'completed' ? 'done' : (milestone.run_status || step.status);
      step.readinessStatus = milestone.readiness_status || '';
      const st = this._station(step.stationId);
      if (!st) continue;
      const li = PROCESSOR_LIGHT[stepId];
      if (st.subLights && li != null) {
        if (step.status === 'done' || step.status === 'skipped') st.subLights[li] = 1;
        else if (step.status === 'failed') st.subLights[li] = 3;
        else st.subLights[li] = 0;
      }
      const stationSteps = this._stationSteps(st.id);
      st.dim = stationSteps.length > 0 && stationSteps.every((item) => item.status === 'skipped');
      if (step.status === 'failed') st.failed = true;
    }
  },

  /**
   * 交接目标：市场支线固定汇入估值室；其余按计划顺序找下一个
   * 未终结步骤的站点；全部终结则飞向交付台。
   */
  _nextStationAfter(step) {
    if (step.stepId === 'market_context_update') return this._station('valuation') || this._station('deliver');
    const idx = this.stepOrder.indexOf(step.stepId);
    for (let i = idx + 1; i < this.stepOrder.length; i++) {
      const nx = this.stepMap.get(this.stepOrder[i]);
      if (!nx) continue;
      if (nx.status === 'pending' || nx.status === 'running' || nx.status === 'waiting') {
        return this._station(nx.stationId);
      }
    }
    return this._station('deliver');
  },

  /* ---------------------------------------------------------
   * 效果生成：流转轨迹系统
   *
   * 数据结构（this.traces 的元素）：
   *   {
   *     kind: 'file' | 'gap',      // 正向交接 / 回流
   *     key: 'from>to>kind',       // 同对合并键
   *     from, to,                  // 站点 id（仅用于调试/去重）
   *     p0, p1, cp1, cp2,          // 三次贝塞尔四点（世界坐标）
   *     path: Path2D | null,       // 缓存的曲线几何（只构建一次）
   *     arrow: {x,y,ang},          // 目标端箭头位姿
   *     mid: {x,y},                // 中点（挂 ×N / 回流签）
   *     c0, c1,                    // 源→目标渐变端色
   *     reveal, revealTarget,      // 显形进度 0..1（随 token 飞行推进）
   *     width,                     // 线宽（同对重复 +1，上限 5）
   *     count,                     // 交接次数（×N 徽标）
   *   }
   *
   * 设计要点：
   *   - 几何（path/arrow/mid）构建一次并缓存；每帧只推进 reveal 与全局
   *     dashPhase，绝不每帧 new Path2D，满足性能约束；
   *   - reveal 让轨迹“随 token 飞过逐段显形”，token 落地后整条常驻；
   *   - reduced/instant 时 revealTarget 直接置 1（瞬间显形、无流动）。
   * --------------------------------------------------------- */

  /**
   * 记录一次正向交接：合并同对轨迹或新建，并按需发射飞行 token。
   * 幂等要点：同 (from→to) 只保留一条轨迹，重复交接仅加粗 + 计数，
   * 因此 replay 大量事件涌入也不会叠出一堆重合线。
   */
  _recordHandoff(fromSt, toSt, instant, metadata = {}) {
    const key = `${fromSt.id}>${toSt.id}>file`;
    let tr = this.traceKeys.get(key);
    const delivery = {
      kind: metadata.kind || 'delivery',
      label: String(metadata.label || '').slice(0, 80),
      invocationId: metadata.invocationId || '',
      workItemId: metadata.workItemId || '',
      isError: !!metadata.isError,
    };
    if (tr) {
      // 同对重复：加粗一档（上限 5）并累加计数，具体任务保存在 deliveries 中供悬浮查看。
      tr.count += 1;
      tr.width = Math.min(5, tr.width + 1);
      tr.revealTarget = 1;
      tr.deliveries.push(delivery);
      if (instant || this.reduced) tr.reveal = 1;
    } else {
      tr = this._buildTrace('file', fromSt, toSt, instant);
      tr.deliveries = [delivery];
      this.traces.push(tr);
      this.traceKeys.set(key, tr);
    }
    if (toSt.tray) toSt.tray.count += 1;
    if (!instant && !this.reduced) {
      this._spawnToken('file', fromSt, toSt, null, tr, delivery.label);
    }
  },

  /**
   * 记录一次回流：红色反向弯曲虚线轨迹 + 逆向六边形 token。
   * onArrive 在 token 落到上游站点时触发（抬头接住动画）。
   */
  _recordBackflow(fromSt, toSt, instant, onArrive) {
    const key = `${fromSt.id}>${toSt.id}>gap`;
    let tr = this.traceKeys.get(key);
    if (tr) {
      tr.count += 1;
      tr.width = Math.min(5, tr.width + 1);
      tr.revealTarget = 1;
      if (instant || this.reduced) tr.reveal = 1;
    } else {
      tr = this._buildTrace('gap', fromSt, toSt, instant);
      this.traces.push(tr);
      this.traceKeys.set(key, tr);
    }
    if (!instant && !this.reduced) {
      this._spawnToken('gap', fromSt, toSt, onArrive, tr);
    } else if (onArrive) {
      onArrive();
    }
  },

  /**
   * 构建一条轨迹的几何与样式（只在首次交接时调用一次）。
   * 弧度方向：正向轨迹向上凸；回流轨迹向下凸（反向弯曲），
   * 从而与并存的正向线错开、不重叠。
   */
  _buildTrace(kind, fromSt, toSt, instant) {
    const p0 = { x: fromSt.x, y: fromSt.y - 46 };
    const p1 = { x: toSt.x, y: toSt.y - 46 };
    const dist = Math.hypot(p1.x - p0.x, p1.y - p0.y);
    const lift = clamp(dist * 0.28, 70, 190);
    // gap（回流）反向弯曲：控制点下压；file 正向上抬
    const dir = kind === 'gap' ? 1 : -1;
    const baseY = kind === 'gap' ? Math.max(p0.y, p1.y) : Math.min(p0.y, p1.y);
    const cp1 = { x: lerp(p0.x, p1.x, 0.3), y: baseY + dir * lift };
    const cp2 = { x: lerp(p0.x, p1.x, 0.7), y: baseY + dir * lift };
    // 目标端箭头：取贝塞尔 t≈0.94 与终点的切向
    const near = this._bezier(p0, cp1, cp2, p1, 0.9);
    const arrow = { x: p1.x, y: p1.y, ang: Math.atan2(p1.y - near.y, p1.x - near.x) };
    const mid = this._bezier(p0, cp1, cp2, p1, 0.5);
    const c0 = kind === 'gap' ? this.pal.critical : this.pal.role(fromSt.owner);
    const c1 = kind === 'gap' ? this.pal.critical : this.pal.role(toSt.owner);
    const full = instant || this.reduced;
    return {
      kind, key: `${fromSt.id}>${toSt.id}>${kind}`, from: fromSt.id, to: toSt.id,
      p0, p1, cp1, cp2, path: null, arrow, mid, c0, c1,
      reveal: full ? 1 : 0, revealTarget: 1,
      width: 3, count: 1,
    };
  },

  /**
   * 生成一枚飞行 token（附着到一条轨迹 tr，飞行时推进 tr.reveal）。
   * 控制点直接取轨迹的 cp1/cp2，保证 token 恰好沿轨迹飞行、留痕严丝合缝。
   */
  _spawnToken(kind, fromSt, toSt, onArrive, trace, label = '') {
    this.tokens.push({
      kind,
      label: String(label || '').slice(0, 24),
      p0: trace ? trace.p0 : { x: fromSt.x, y: fromSt.y - 46 },
      p1: trace ? trace.p1 : { x: toSt.x, y: toSt.y - 46 },
      cp1: trace ? trace.cp1 : { x: lerp(fromSt.x, toSt.x, 0.3), y: Math.min(fromSt.y, toSt.y) - 120 },
      cp2: trace ? trace.cp2 : { x: lerp(fromSt.x, toSt.x, 0.7), y: Math.min(fromSt.y, toSt.y) - 120 },
      t: 0, dur: kind === 'gap' ? 1.35 : 1.2,
      onArrive: onArrive || null,
      trace: trace || null,
      bounce: 0,
    });
  },

  /** 交付台彩带：60-80 个彩色小矩形，重力 + 旋转，2.5s 衰减。 */
  _spawnConfetti(x, y) {
    const colors = Object.values(this.pal.roles).concat([this.pal.seq2, this.pal.good, this.pal.warning]);
    const n = 60 + Math.floor(Math.random() * 20);
    for (let i = 0; i < n; i++) {
      const ang = -Math.PI / 2 + (Math.random() - 0.5) * 1.4;
      const sp = 220 + Math.random() * 260;
      this.confetti.push({
        x, y,
        vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp,
        rot: Math.random() * Math.PI * 2,
        vr: (Math.random() - 0.5) * 10,
        w: 4 + Math.random() * 5, h: 3 + Math.random() * 4,
        color: colors[i % colors.length],
        life: 1, decay: 1 / 2.5,
      });
    }
  },

  /** 生成一条上飘的文字提示。 */
  _floaty(x, y, text, color) {
    this.floaties.push({ x, y, text, color, life: 1 });
  },

  /* ---------------------------------------------------------
   * 相机
   * --------------------------------------------------------- */

  /**
   * 让全景恰好装进画布：用于初始与双击复位。
   * 以“内容包围盒”（站点 + 走廊）为基准而非整张世界，避免四周留白
   * 把小人挤小。放大后的舞台若超出画布，允许 fit 到约 0.85 再由用户缩放；
   * 同时设一个缩放下限，保证小人屏显高度不至于过小。
   */
  _fitCamera() {
    if (!this.layout || !this.cssW) return;
    const L = this.layout;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const eat = (x, y) => {
      if (x < minX) minX = x; if (x > maxX) maxX = x;
      if (y < minY) minY = y; if (y > maxY) maxY = y;
    };
    // 站点包围盒：上含悬挂站名牌，下含小人站位与收件托盘
    for (const s of L.stations) { eat(s.x - 96, s.y - 150); eat(s.x + 96, s.y + 110); }
    for (const p of L.corridor.pts) eat(p.x, p.y);
    const margin = 40;
    const bw = (maxX - minX) + margin * 2, bh = (maxY - minY) + margin * 2;
    // 缩放下限 0.62：小人世界身高约 84px，×0.62 ≈ 52px；配合放大后仍清晰。
    // 上限 1.4：小场景（行业 4 站）时不至于把小人怼得过大。
    this.cam.zoom = clamp(Math.min(this.cssW / bw, this.cssH / bh), 0.62, 1.4);
    this.cam.x = this.cam.tx = (minX + maxX) / 2;
    this.cam.y = this.cam.ty = (minY + maxY) / 2;
  },

  /** 自动轻推：把活跃站点设为镜头目标（用户操作后 10s 内不打扰）。 */
  _focusCam(station) {
    if (!station) return;
    if (performance.now() < this.cam.userLockUntil) return;
    this.cam.tx = station.x;
    this.cam.ty = station.y - 30;
  },

  /** 公开接口：镜头对准某角色主站点（同时解除用户锁定）。 */
  focusStation(owner) {
    const st = this._homeStation(owner);
    if (st) {
      this.cam.userLockUntil = 0;
      this.cam.tx = st.x; this.cam.ty = st.y - 30;
    }
  },

  /** 屏幕坐标 → 世界坐标。 */
  _toWorld(px, py) {
    return {
      x: (px - this.cssW / 2) / this.cam.zoom + this.cam.x,
      y: (py - this.cssH / 2) / this.cam.zoom + this.cam.y,
    };
  },

  /* ---------------------------------------------------------
   * 指针交互：拖拽平移 / 滚轮缩放 / 悬浮 / 点击 / 双击复位
   * --------------------------------------------------------- */

  _bindPointer() {
    const cv = this.canvas;

    cv.addEventListener('pointerdown', (e) => {
      cv.setPointerCapture(e.pointerId);
      const r = cv.getBoundingClientRect();
      this.pointer = {
        down: true, moved: false,
        sx: e.clientX - r.left, sy: e.clientY - r.top,
        camX: this.cam.x, camY: this.cam.y,
        lastX: e.clientX - r.left, lastY: e.clientY - r.top,
      };
    });

    cv.addEventListener('pointermove', (e) => {
      const r = cv.getBoundingClientRect();
      const px = e.clientX - r.left, py = e.clientY - r.top;
      if (this.pointer.down) {
        const dx = px - this.pointer.sx, dy = py - this.pointer.sy;
        if (Math.hypot(dx, dy) > 4) this.pointer.moved = true;
        if (this.pointer.moved) {
          // 拖拽平移：位移按当前缩放折算回世界坐标，并锁定自动跟随 10s
          this.cam.x = this.pointer.camX - dx / this.cam.zoom;
          this.cam.y = this.pointer.camY - dy / this.cam.zoom;
          this.cam.tx = this.cam.x; this.cam.ty = this.cam.y;
          this.cam.userLockUntil = performance.now() + 10000;
          cv.style.cursor = 'grabbing';
        }
      } else {
        this._updateHover(px, py);
      }
      this.pointer.lastX = px; this.pointer.lastY = py;
    });

    cv.addEventListener('pointerup', (e) => {
      const wasDrag = this.pointer.moved;
      this.pointer.down = false;
      cv.style.cursor = this.hoverStationId || this.hoverSign ? 'pointer' : 'default';
      if (wasDrag) return;
      // 点击：优先命中“等待 Claude”牌子，其次站点
      const r = cv.getBoundingClientRect();
      const w = this._toWorld(e.clientX - r.left, e.clientY - r.top);
      const signStep = this._hitSign(w);
      if (signStep) { if (this.cbSignClick) this.cbSignClick(signStep); return; }
      const st = this._hitStation(w);
      if (st && this.cbStationClick) this.cbStationClick({ stationId: st.id, owner: st.owner });
    });

    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      if (!this.layout) return;
      const r = cv.getBoundingClientRect();
      const px = e.clientX - r.left, py = e.clientY - r.top;
      const before = this._toWorld(px, py);
      // 指数缩放：手感均匀；限制 0.5~2.0（放大后允许缩得更远看全局、也能凑近看五官）
      const factor = Math.exp(-e.deltaY * 0.0012);
      this.cam.zoom = clamp(this.cam.zoom * factor, 0.5, 2.0);
      // 保持光标下的世界点不动：反解新的相机中心
      const after = this._toWorld(px, py);
      this.cam.x += before.x - after.x;
      this.cam.y += before.y - after.y;
      this.cam.tx = this.cam.x; this.cam.ty = this.cam.y;
      this.cam.userLockUntil = performance.now() + 10000;
    }, { passive: false });

    cv.addEventListener('dblclick', () => {
      // 双击复位：回全景并立即恢复自动跟随
      this.cam.userLockUntil = 0;
      this._fitCamera();
    });

    cv.addEventListener('pointerleave', () => { this.hoverStationId = null; this.hoverSign = false; });
  },

  /** 更新悬浮目标与光标样式。 */
  _updateHover(px, py) {
    if (!this.layout) { this.hoverStationId = null; return; }
    const w = this._toWorld(px, py);
    this.hoverSign = !!this._hitSign(w);
    const st = this._hitStation(w);
    this.hoverStationId = st ? st.id : null;
    this.canvas.style.cursor = (st || this.hoverSign) ? 'pointer' : 'default';
  },

  /** 站点命中测试：以站台为中心的宽松矩形（覆盖放大后的桌台与站名牌）。 */
  _hitStation(w) {
    if (!this.layout) return null;
    for (const s of this.layout.stations) {
      if (w.x > s.x - 90 && w.x < s.x + 90 && w.y > s.y - 140 && w.y < s.y + 40) return s;
    }
    return null;
  },

  /** “等待 Claude 分析”牌子命中测试（几何与 sprites.signHitRect 同源）。 */
  _hitSign(w) {
    for (const a of this.agents.values()) {
      if (a.state === 'wait_llm' && a.signRect) {
        const r = a.signRect;
        if (w.x > r.x && w.x < r.x + r.w && w.y > r.y && w.y < r.y + r.h) return a.signStepId;
      }
    }
    return null;
  },

  /* ---------------------------------------------------------
   * 主循环
   * --------------------------------------------------------- */

  _loop(ts) {
    if (!this.running) return;
    requestAnimationFrame((t) => this._loop(t));
    if (this.paused) return;

    // dt 夹取到 50ms：后台切换回来时不产生巨帧，动画不会瞬移穿模
    const dt = clamp((ts - this.lastFrame) / 1000, 0, 0.05);
    this.lastFrame = ts;
    this.time += dt;

    this._update(dt);
    this._render();
  },

  _update(dt) {
    // 相机缓动跟随：指数趋近，帧率无关
    const k = this.reduced ? 1 : 1 - Math.exp(-dt * 3.2);
    if (performance.now() >= this.cam.userLockUntil) {
      this.cam.x = lerp(this.cam.x, this.cam.tx, k);
      this.cam.y = lerp(this.cam.y, this.cam.ty, k);
    }

    for (const a of this.agents.values()) a.update(dt, this.time);

    // 流动短划线相位：约 24px/s（reduced 时不流动）
    if (!this.reduced) this.dashPhase += dt * 24;

    // token 飞行：飞行进度同步推进所附轨迹的显形（reveal）
    for (const tk of this.tokens) {
      tk.t += dt / tk.dur;
      if (tk.trace && tk.trace.reveal < 1) {
        // 让轨迹显形略领先 token 头部一点，视觉上像“笔尖在画线”
        tk.trace.reveal = clamp(Math.max(tk.trace.reveal, ease.outCubic(clamp(tk.t, 0, 1))), 0, 1);
      }
      if (tk.t >= 1 && !tk.done) {
        tk.done = true; tk.bounce = 0.001;
        if (tk.trace) tk.trace.reveal = 1;
        if (tk.onArrive) tk.onArrive();
      }
      if (tk.done) tk.bounce += dt;
    }
    this.tokens = this.tokens.filter((tk) => !tk.done || tk.bounce < 0.5);

    // 轨迹 reveal 兜底缓动：instant/reduced 或无 token 附着时平滑补满
    for (const tr of this.traces) {
      if (tr.reveal < tr.revealTarget) {
        tr.reveal = clamp(tr.reveal + dt * 2.2, 0, tr.revealTarget);
      }
    }

    // 彩带：重力 + 空气阻尼 + 旋转
    for (const p of this.confetti) {
      p.vy += 560 * dt;
      p.vx *= (1 - 0.9 * dt);
      p.x += p.vx * dt; p.y += p.vy * dt;
      p.rot += p.vr * dt;
      p.life -= p.decay * dt;
    }
    this.confetti = this.confetti.filter((p) => p.life > 0);

    // 上飘文字
    for (const f of this.floaties) { f.y -= 18 * dt; f.life -= dt / 2.2; }
    this.floaties = this.floaties.filter((f) => f.life > 0);

    // 灰幕过渡
    this.veil = lerp(this.veil, this.veilTarget, 1 - Math.exp(-dt * 4));
  },

  /* ---------------------------------------------------------
   * 渲染
   * --------------------------------------------------------- */

  _render() {
    const ctx = this.ctx;
    if (!ctx) return;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.cssW, this.cssH);

    // 背板（画布外底色）
    ctx.fillStyle = this.pal.stageWall;
    ctx.fillRect(0, 0, this.cssW, this.cssH);

    if (!this.layout) { this._renderIdleSplash(ctx); return; }
    if (this.bgDirty) this._renderStaticBg();

    // —— 进入世界坐标系 —— //
    ctx.save();
    ctx.translate(this.cssW / 2, this.cssH / 2);
    ctx.scale(this.cam.zoom, this.cam.zoom);
    ctx.translate(-this.cam.x, -this.cam.y);

    // 静态背景整幅贴上（离屏层仅含干净地板与极淡站台底板）
    ctx.drawImage(this.bgCanvas, 0, 0, this.layout.worldW, this.layout.worldH);

    // 流转轨迹层：绘制顺序介于地板与站点之间（地板 < 轨迹 < 站点 < 小人 < 飞行物）
    this._drawTraces(ctx);

    // 动态站点（道具动画、灯、蒙层）——按 y 排序保证前后遮挡自然
    const time = this.reduced ? 0 : this.time;
    const stations = [...this.layout.stations].sort((a, b) => a.y - b.y);
    for (const s of stations) {
      s.color = this.pal.role(s.owner);
      drawStation(ctx, s, this.pal, time);
      // 收件托盘：落地文件的堆叠（画在站台之后、小人之前）
      drawInboxTray(ctx, s, this.pal);
      // 失败站点：红色警示描边（尺寸随放大后的桌台）
      if (s.failed) {
        ctx.strokeStyle = withAlpha(this.pal.critical, 0.65);
        ctx.lineWidth = 2.5;
        roundRectPath(ctx, s.x - 82, s.y - 96, 164, 116, 12);
        ctx.stroke();
      }
      // 等待 LLM 的站点：暖色呼吸光圈
      if (s.waiting && !this.reduced) {
        const rr = 92 + Math.sin(this.time * 2.2) * 7;
        ctx.strokeStyle = withAlpha(this.pal.warning, 0.4);
        ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.ellipse(s.x, s.y - 10, rr, rr * 0.4, 0, 0, Math.PI * 2); ctx.stroke();
      }
    }

    // 交付旗（完成 / 半旗）
    const deliverSt = this._station('deliver');
    if (deliverSt && this.flagState === 'half') {
      drawHalfMast(ctx, deliverSt.x + 58, deliverSt.y - 30, this.pal, this.pal.warning, time);
    }

    // 小人（y 排序），wait_llm 者顺带记录牌子命中区
    const agents = [...this.agents.values()].sort((a, b) => a.y - b.y);
    for (const a of agents) {
      a.color = this.pal.role(a.owner);
      drawAgent(ctx, a, this.pal, this.time);
      // 牌子命中区：与 sprites.signHitRect 同一几何来源，杜绝错位
      a.signRect = a.state === 'wait_llm' ? signHitRect(a) : null;
      // 回流原因等临时气泡（放在放大后的头顶上方）
      if (a.bubble) this._drawSpeech(ctx, a.x, a.y - 150, a.bubble.text);
    }
    if (this.traceMode === 'runtime') this._drawRuntimeJobCards(ctx);

    // 飞行 token
    for (const tk of this.tokens) {
      const t = clamp(tk.t, 0, 1);
      const et = ease.outCubic(t);
      const pos = this._bezier(tk.p0, tk.cp1, tk.cp2, tk.p1, et);
      // 落地小弹跳：done 后在终点做一次快速压缩回弹
      let scale = 1;
      if (tk.done) scale = 1 + Math.sin(clamp(tk.bounce * 8, 0, Math.PI)) * 0.25;
      if (tk.kind === 'file') drawFileToken(ctx, pos.x, pos.y, scale, Math.sin(t * Math.PI * 2) * 0.12, this.pal);
      else drawGapToken(ctx, pos.x, pos.y, scale, this.pal);
      if (tk.label) {
        const label = tk.label.length > 18 ? `${tk.label.slice(0, 17)}…` : tk.label;
        ctx.font = '10px system-ui';
        const width = Math.min(150, ctx.measureText(label).width + 12);
        roundRectPath(ctx, pos.x - width / 2, pos.y - 30, width, 18, 7);
        ctx.fillStyle = withAlpha(this.pal.card, 0.94); ctx.fill();
        ctx.strokeStyle = withAlpha(this.pal.border, 0.9); ctx.lineWidth = 1; ctx.stroke();
        ctx.fillStyle = this.pal.fg; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(label, pos.x, pos.y - 21);
      }
    }

    // 彩带与上飘文字
    for (const p of this.confetti) drawConfettiPiece(ctx, p);
    for (const f of this.floaties) {
      ctx.globalAlpha = clamp(f.life, 0, 1);
      ctx.fillStyle = f.color;
      ctx.font = 'bold 12px system-ui';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(f.text, f.x, f.y);
      ctx.globalAlpha = 1;
    }

    ctx.restore();

    // —— 屏幕坐标层：悬浮 tooltip 与灰幕 —— //
    if (this.hoverStationId) this._drawTooltip(ctx);
    if (this.veil > 0.01) {
      ctx.fillStyle = withAlpha(this.pal.dark ? '#000000' : '#52514e', this.veil);
      ctx.fillRect(0, 0, this.cssW, this.cssH);
    }
  },

  /** 三次贝塞尔取点。 */
  _bezier(p0, c1, c2, p1, t) {
    const u = 1 - t;
    return {
      x: u * u * u * p0.x + 3 * u * u * t * c1.x + 3 * u * t * t * c2.x + t * t * t * p1.x,
      y: u * u * u * p0.y + 3 * u * u * t * c1.y + 3 * u * t * t * c2.y + t * t * t * p1.y,
    };
  },

  /**
   * 绘制流转轨迹层。
   *
   * 每条轨迹：
   *   1. 沿贝塞尔按 reveal 比例采样若干点，构建一次 Path2D 并缓存到
   *      tr._geom（reveal 变化时才重建，稳定后帧内零构建）；
   *   2. 用源→目标渐变描边，圆帽，整体 alpha≈0.5；
   *   3. 叠加一层流动短划线（lineDashOffset = -dashPhase），表达数据在流动；
   *   4. 目标端画箭头；同对重复交接在中点挂 ×N 徽标；回流在中点挂“⚠ 回流”。
   *
   * 性能：Path2D 仅在 reveal 推进时重建（有限次），流动仅改 lineDashOffset。
   */
  _drawTraces(ctx) {
    if (!this.traces.length) return;
    const reduced = this.reduced;
    for (const tr of this.traces) {
      if (tr.reveal <= 0.001) continue;

      // —— 几何缓存：仅在 reveal 明显变化时重采样重建 Path2D —— //
      if (!tr._geom || Math.abs(tr._geom.reveal - tr.reveal) > 0.012) {
        const steps = 24;
        const upto = Math.max(1, Math.round(steps * tr.reveal));
        const path = new Path2D();
        let head = null;
        for (let i = 0; i <= upto; i++) {
          const t = (i / steps);
          const pt = this._bezier(tr.p0, tr.cp1, tr.cp2, tr.p1, t);
          if (i === 0) path.moveTo(pt.x, pt.y); else path.lineTo(pt.x, pt.y);
          head = pt;
        }
        // 显形头部的切向（画“正在延伸”的笔尖箭头）
        const prev = this._bezier(tr.p0, tr.cp1, tr.cp2, tr.p1, Math.max(0, (upto - 1) / steps));
        tr._geom = { reveal: tr.reveal, path, head, headAng: Math.atan2(head.y - prev.y, head.x - prev.x) };
      }
      const geom = tr._geom;

      // —— 渐变描边（源色 → 目标色），整体半透明 —— //
      const grad = ctx.createLinearGradient(tr.p0.x, tr.p0.y, tr.p1.x, tr.p1.y);
      grad.addColorStop(0, withAlpha(tr.c0, 0.5));
      grad.addColorStop(1, withAlpha(tr.c1, 0.5));

      ctx.save();
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      // 底层：回流用红色虚线基底，正向用实线
      if (tr.kind === 'gap') {
        ctx.setLineDash([2, 10]);
        ctx.lineDashOffset = 0;
        ctx.strokeStyle = withAlpha(this.pal.critical, 0.5);
        ctx.lineWidth = tr.width;
        ctx.stroke(geom.path);
        ctx.setLineDash([]);
      } else {
        ctx.strokeStyle = grad;
        ctx.lineWidth = tr.width;
        ctx.stroke(geom.path);
      }
      // 流动短划线叠层：正向轨迹的“数据流动”光带（reduced 时不流动）
      if (tr.kind === 'file') {
        ctx.setLineDash([10, 14]);
        ctx.lineDashOffset = reduced ? 0 : -this.dashPhase;
        ctx.strokeStyle = withAlpha(shade(tr.c1, 0.2), 0.7);
        ctx.lineWidth = Math.max(1.5, tr.width - 1.2);
        ctx.stroke(geom.path);
        ctx.setLineDash([]);
      }
      ctx.restore();

      // —— 目标端箭头：轨迹显形完成后画在终点，否则画在延伸头部 —— //
      const arr = tr.reveal >= 0.995 ? tr.arrow : { x: geom.head.x, y: geom.head.y, ang: geom.headAng };
      this._drawTraceArrow(ctx, arr, tr.kind === 'gap' ? this.pal.critical : tr.c1);

      // —— 中点徽标：×N（重复交接）或“⚠ 回流” —— //
      if (tr.reveal >= 0.6) {
        if (tr.kind === 'gap') {
          this._drawTraceBadge(ctx, tr.mid.x, tr.mid.y, '⚠ 回流', this.pal.critical, true);
        } else {
          const latest = tr.deliveries && tr.deliveries.length ? tr.deliveries[tr.deliveries.length - 1] : null;
          const label = latest && latest.label ? String(latest.label) : '';
          const short = label.length > 16 ? `${label.slice(0, 15)}…` : label;
          const badge = tr.count > 1 ? `${short ? `${short} ` : ''}×${tr.count}` : short;
          if (badge) this._drawTraceBadge(ctx, tr.mid.x, tr.mid.y, badge, tr.c1, false);
        }
      }
    }
  },

  /** 轨迹目标端的小箭头（实心三角）。 */
  _drawTraceArrow(ctx, arr, color) {
    ctx.save();
    ctx.translate(arr.x, arr.y);
    ctx.rotate(arr.ang);
    ctx.fillStyle = withAlpha(color, 0.75);
    ctx.beginPath();
    ctx.moveTo(2, 0); ctx.lineTo(-9, -5.5); ctx.lineTo(-6, 0); ctx.lineTo(-9, 5.5);
    ctx.closePath(); ctx.fill();
    ctx.restore();
  },

  /** 轨迹中点徽标：白底圆角小签（×N 计数 / ⚠ 回流）。 */
  _drawTraceBadge(ctx, x, y, text, color, warn) {
    ctx.save();
    ctx.font = 'bold 11px system-ui';
    const w = ctx.measureText(text).width + (warn ? 14 : 12), h = 18;
    ctx.shadowColor = this.pal.dark ? 'rgba(0,0,0,.5)' : 'rgba(38,36,31,.2)';
    ctx.shadowBlur = 4; ctx.shadowOffsetY = 1;
    roundRectPath(ctx, x - w / 2, y - h / 2, w, h, 9);
    ctx.fillStyle = this.pal.card; ctx.fill();
    ctx.shadowColor = 'transparent';
    roundRectPath(ctx, x - w / 2, y - h / 2, w, h, 9);
    ctx.strokeStyle = withAlpha(color, 0.8); ctx.lineWidth = 1.2; ctx.stroke();
    ctx.fillStyle = warn ? color : this.pal.fg;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(text, x, y + 0.5);
    ctx.restore();
  },

  /** 空闲首屏：无 run 时的引导画面。 */
  _renderIdleSplash(ctx) {
    ctx.fillStyle = this.pal.mut;
    ctx.font = '14px system-ui';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('选择模式并开始一次运行，研究工坊即刻开工', this.cssW / 2, this.cssH / 2 - 10);
    ctx.fillStyle = this.pal.weak;
    ctx.font = '12px system-ui';
    ctx.fillText('公司研究 · 行业研究 · 演示 · 回放', this.cssW / 2, this.cssH / 2 + 14);
    // 三个静态小圆点作装饰
    const cols = [this.pal.role('information-collector'), this.pal.role('orchestrator'), this.pal.role('valuation-analyst')];
    cols.forEach((c, i) => {
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.arc(this.cssW / 2 - 24 + i * 24, this.cssH / 2 + 42, 5, 0, Math.PI * 2); ctx.fill();
    });
  },

  /** 在角色站点旁显示真实 Agent invocation 的任务与当前工具。 */
  _drawRuntimeJobCards(ctx) {
    const byOwner = new Map();
    for (const job of this.runtimeJobs.values()) {
      const list = byOwner.get(job.owner) || [];
      list.push(job);
      byOwner.set(job.owner, list);
    }
    for (const [owner, jobs] of byOwner.entries()) {
      const st = this._homeStation(owner);
      if (!st) continue;
      jobs.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
      const activeJobs = jobs.filter((job) => job.status === 'running');
      // 运行中最多并排展示 2 个；没有活动任务时只保留最近完成的一张结果卡，
      // 避免处理员连续执行 parse/digest/RAG 时把画板堆满历史卡片。
      const visible = activeJobs.length ? activeJobs.slice(0, 2) : jobs.slice(0, 1);
      visible.forEach((job, index) => {
        const w = 190, h = 34;
        const x = st.x + (st.x > this.layout.worldW * 0.68 ? -w - 72 : 72);
        const y = st.y - 126 - index * 39;
        roundRectPath(ctx, x, y, w, h, 8);
        ctx.fillStyle = withAlpha(this.pal.card, 0.96); ctx.fill();
        const statusColor = job.status === 'failed' ? this.pal.critical
          : (job.status === 'completed' ? this.pal.good : this.pal.role(owner));
        ctx.strokeStyle = withAlpha(statusColor, 0.75); ctx.lineWidth = job.status === 'running' ? 2 : 1; ctx.stroke();
        ctx.fillStyle = statusColor;
        ctx.beginPath(); ctx.arc(x + 10, y + 10, 4, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = this.pal.fg; ctx.font = 'bold 10.5px system-ui';
        ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
        const title = String(job.title || '执行子任务');
        ctx.fillText(title.length > 22 ? `${title.slice(0, 21)}…` : title, x + 19, y + 10, w - 26);
        ctx.fillStyle = this.pal.mut; ctx.font = '9.5px system-ui';
        const detail = job.currentTool
          ? `工具 ${job.currentTool}`
          : (job.status === 'completed' ? '结果已回传' : (job.status === 'failed' ? '执行失败' : 'Agent 工作中'));
        ctx.fillText(detail, x + 10, y + 25, w - 18);
      });
      if (activeJobs.length > visible.length) {
        const x = st.x + (st.x > this.layout.worldW * 0.68 ? -118 : 118);
        const y = st.y - 126 - visible.length * 39;
        this._drawSpeech(ctx, x, y, `+${activeJobs.length - visible.length} 个并发任务`);
      }
    }
  },

  /** 简易语音气泡（回流原因），世界坐标绘制，自动按文字宽度撑开。 */
  _drawSpeech(ctx, x, y, text) {
    ctx.save();
    ctx.font = '10px system-ui';
    const tw = Math.min(ctx.measureText(text).width, 220);
    const w = tw + 16, h = 20;
    roundRectPath(ctx, x - w / 2, y - h, w, h, 6);
    ctx.fillStyle = this.pal.card; ctx.fill();
    ctx.strokeStyle = this.pal.critical; ctx.lineWidth = 1; ctx.stroke();
    ctx.fillStyle = this.pal.fg;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(text, x, y - h / 2, 210);
    ctx.restore();
  },

  /* ---------------------------------------------------------
   * 静态背景离屏渲染
   * 为什么离屏：干净地板与站台底板不随帧变化，预渲染成一张位图后
   * 主循环只需一次 drawImage。本版已删除树/地垫/走廊虚线/方向箭头/
   * 网点等一切纯装饰，只留：纯色地板 + 顶部墙沿 + 极淡站台底板 +
   * 市场雷达站的悬浮夹层板（干净浅色板 + 细边 + 淡投影）。
   * --------------------------------------------------------- */

  _renderStaticBg() {
    const L = this.layout;
    if (!L) return;
    const SC = this.BG_SCALE;
    if (!this.bgCanvas) this.bgCanvas = document.createElement('canvas');
    this.bgCanvas.width = L.worldW * SC;
    this.bgCanvas.height = L.worldH * SC;
    const b = this.bgCanvas.getContext('2d');
    b.setTransform(SC, 0, 0, SC, 0, 0);
    b.clearRect(0, 0, L.worldW, L.worldH);
    const pal = this.pal;

    // 地板：纯色底
    b.fillStyle = pal.stageFloor;
    b.fillRect(0, 0, L.worldW, L.worldH);
    // 顶部墙沿：一条更深的横带制造进深
    b.fillStyle = pal.stageWall;
    b.fillRect(0, 0, L.worldW, 34);
    b.fillStyle = withAlpha(pal.fg, 0.05);
    b.fillRect(0, 34, L.worldW, 3);

    // 极淡地板纹理：极低对比度的宽条纹，几乎不可见，只为避免大片死板纯色。
    // 透明度压到 ≈0.02，满足“降到几乎不可见的极淡纹理”。
    b.fillStyle = withAlpha(pal.fg, pal.dark ? 0.02 : 0.018);
    for (let gy = 90; gy < L.worldH; gy += 96) {
      b.fillRect(0, gy, L.worldW, 48);
    }

    // 市场夹层：干净浅色悬浮板（细边框 + 淡投影），不再画支路虚线
    if (L.mode === 'company') {
      const m = L.stations.find((s) => s.id === 'market');
      if (m) {
        const px = m.x - 150, py = m.y - 96, pw = 300, ph = 216;
        // 淡投影
        b.save();
        b.shadowColor = pal.dark ? 'rgba(0,0,0,.5)' : 'rgba(38,36,31,.16)';
        b.shadowBlur = 22; b.shadowOffsetY = 10;
        b.fillStyle = pal.dark ? shade(pal.stageWall, 0.1) : shade(pal.stageFloor, 0.35);
        roundRectPath(b, px, py, pw, ph, 20); b.fill();
        b.restore();
        // 细边框
        b.strokeStyle = withAlpha(pal.role('market-context-collector'), 0.4);
        b.lineWidth = 2;
        roundRectPath(b, px, py, pw, ph, 20); b.stroke();
        // 夹层标签
        b.fillStyle = pal.weak; b.font = '13px system-ui'; b.textAlign = 'left'; b.textBaseline = 'alphabetic';
        b.fillText('夹层 · 市场支线', px + 16, py + 24);
      }
    }

    // 每个站点脚下的极淡站台底板：角色色极低透明度圆角矩形，只作“工位领域”暗示
    for (const s of L.stations) {
      if (s.id === 'market') continue; // 市场站已有夹层板，不再叠底板
      const c = pal.role(s.owner);
      b.fillStyle = withAlpha(c, pal.dark ? 0.07 : 0.05);
      roundRectPath(b, s.x - 88, s.y - 40, 176, 116, 18); b.fill();
    }

    this.bgDirty = false;
  },

  /* ---------------------------------------------------------
   * 悬浮 tooltip（屏幕坐标层）
   * --------------------------------------------------------- */

  _drawTooltip(ctx) {
    const st = this._station(this.hoverStationId);
    if (!st) return;
    const lines = [];
    lines.push({ text: `${st.label} · ${OWNER_META[st.owner] ? OWNER_META[st.owner].name : st.owner}`, bold: true });
    for (const layer of st.layers) {
      const status = this.layerStatuses[layer] || '未知';
      const reuse = this.reusable[layer] ? ' · 可复用' : '';
      lines.push({ text: `${LAYER_LABEL[layer] || layer}：${status}${reuse}` });
    }
    for (const x of this._stationSteps(st.id)) {
      lines.push({ text: `· ${x.title} — ${STEP_STATUS_TEXT[x.status] || x.status}` });
    }
    if (!lines.length) return;

    ctx.save();
    ctx.font = '11px system-ui';
    const w = Math.min(280, Math.max(...lines.map((l) => ctx.measureText(l.text).width)) + 20);
    const lh = 16, h = lines.length * lh + 12;
    // 位置跟随指针，靠边时自动翻转
    let x = this.pointer.lastX + 14, y = this.pointer.lastY + 14;
    if (x + w > this.cssW - 8) x = this.pointer.lastX - w - 10;
    if (y + h > this.cssH - 8) y = this.pointer.lastY - h - 10;
    roundRectPath(ctx, x, y, w, h, 8);
    ctx.fillStyle = this.pal.card; ctx.fill();
    ctx.strokeStyle = this.pal.border; ctx.lineWidth = 1; ctx.stroke();
    lines.forEach((l, i) => {
      ctx.fillStyle = l.bold ? this.pal.fg : this.pal.mut;
      ctx.font = l.bold ? 'bold 11px system-ui' : '11px system-ui';
      ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
      ctx.fillText(l.text, x + 10, y + 10 + i * lh, w - 20);
    });
    ctx.restore();
  },
};

export default game;
