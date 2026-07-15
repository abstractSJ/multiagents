/**
 * sprites.js — 舞台矢量绘制库（大比例 Q 版重制）
 *
 * 功能：
 *   以纯函数方式，用 Canvas 2D 矢量图形绘制“研究工坊”里的角色小人、
 *   站点道具、收件托盘、飞行 token、彩带粒子等元素。所有函数不持有状态，
 *   由 game.js 传入位置、调色板与动画时间后即时绘制。
 *
 * 本版设计取向：
 *   1. 小人为舞台绝对主角：世界坐标身高约 84px，头身比接近 2:5（头占
 *      身高约 40%），圆润胶囊身体 + 短腿小脚，主色即角色色；
 *   2. 描边不用纯黑，一律用“角色色加深 35%”的同色系深色细描边，
 *      深浅两套主题下都不会出现突兀的黑框；
 *   3. 每个角色一件专属头饰 + 一件手持道具，全部画在头部局部坐标系里，
 *      随头整体移动，任何状态下都不会跳动或穿模；
 *   4. 站点道具沿用旧版的几何拼装代码，通过外层 scale(1.9) 整体放大，
 *      保证与放大后的小人比例协调，同时避免重写十套道具几何。
 */

/* ============================================================
 * 一、缓动与数学工具
 * 为什么手写：全站零依赖，动画节奏需要可控且可预测的缓动曲线。
 * ============================================================ */

/** 线性插值：在 a、b 之间按比例 t(0..1) 取值。 */
export function lerp(a, b, t) { return a + (b - a) * t; }

/** 数值夹取到 [lo, hi] 区间。 */
export function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

/** 缓动函数集合：入场、出场、回弹等常用曲线。 */
export const ease = {
  /** 二次 in-out：慢-快-慢，用于镜头与呼吸等自然过渡。 */
  inOutQuad: (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2),
  /** 三次 out：起步快、收尾缓，用于 token 飞行落位。 */
  outCubic: (t) => 1 - Math.pow(1 - t, 3),
  /** 回弹 out：收尾略微过冲再回弹，用于跳跃/落地弹跳。 */
  outBack: (t) => {
    const c1 = 1.70158, c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  },
  /** 正弦往复：产出 -1..1 的平滑摆动，用于呼吸、晃动。 */
  sine: (t) => Math.sin(t * Math.PI * 2),
};

/**
 * 兼容性圆角矩形路径。
 * 为什么保留自绘：部分环境 ctx.roundRect 缺失或半径过大时行为不一，
 * 这里统一夹取半径并手绘四段圆弧，行为可预测。
 */
export function roundRectPath(ctx, x, y, w, h, r) {
  const rr = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

/** 便捷：填充一个圆角矩形。 */
function fillRR(ctx, x, y, w, h, r, color) {
  roundRectPath(ctx, x, y, w, h, r);
  ctx.fillStyle = color;
  ctx.fill();
}

/** 便捷：描边一个圆角矩形。 */
function strokeRR(ctx, x, y, w, h, r, color, lw) {
  roundRectPath(ctx, x, y, w, h, r);
  ctx.strokeStyle = color;
  ctx.lineWidth = lw || 1;
  ctx.stroke();
}

/**
 * 在 #rrggbb 颜色上叠加透明度，返回 rgba 字符串。
 * 用于半透明光圈、投影、复用蒙层等，避免依赖外部颜色库。
 */
export function withAlpha(hex, a) {
  if (typeof hex !== 'string') return `rgba(128,128,128,${a})`;
  let h = hex.trim();
  if (h[0] === '#') h = h.slice(1);
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  if (h.length !== 6) return `rgba(128,128,128,${a})`;
  const n = parseInt(h, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/**
 * 把颜色向白/黑混合，得到更浅或更深的同色系颜色。
 * amt>0 混白（提亮），amt<0 混黑（压暗）。角色描边统一取 shade(color,-0.35)。
 */
export function shade(hex, amt) {
  let h = (hex || '#888888').trim();
  if (h[0] === '#') h = h.slice(1);
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const n = parseInt(h, 16);
  let r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  const mix = amt > 0 ? 255 : 0, k = Math.abs(amt);
  r = Math.round(lerp(r, mix, k));
  g = Math.round(lerp(g, mix, k));
  b = Math.round(lerp(b, mix, k));
  return `rgb(${r},${g},${b})`;
}

/* ============================================================
 * 二、角色几何常量
 * 集中定义身体各段尺寸：drawAgent 与 game.js 的命中区计算共用一套
 * 数字（通过 signHitRect 导出），避免“牌子画在 A 处、点击区在 B 处”。
 * ============================================================ */

const AG = {
  LEG_H: 10,      // 腿高（髋到脚底）
  BODY_W: 30,     // 身体胶囊宽
  BODY_H: 36,     // 身体胶囊高
  HEAD_R: 17,     // 头半径（直径 34 ≈ 身高 84 的 40%，Q 版大头比例）
  CHIN: 5,        // 下巴与身体的重叠量，让头“坐”在身体上
};

/** 脚底 y → 头心 y 的固定偏移（不含呼吸位移）。 */
function headCyOf(feetY) {
  return feetY - AG.LEG_H - AG.BODY_H - AG.HEAD_R + AG.CHIN;
}

/**
 * “等待 Claude 分析”举牌的世界坐标命中矩形。
 * game.js 的点击检测直接调用本函数，与绘制端共享同一几何来源。
 */
export function signHitRect(a) {
  const headTop = headCyOf(a.y) - AG.HEAD_R;
  const cy = headTop - 16 - 34;          // 名牌上方再抬 34px 是牌面中心
  return { x: a.x - 78, y: cy - 17, w: 156, h: 34 };
}

/* ============================================================
 * 三、角色小人绘制
 * agent 对象约定字段（由 game.js 维护，sprites 只读）：
 *   x, y / color / name / owner / state / stateT / walkPhase /
 *   facing / blink / progress {done,total} / prop / reduced / phase
 * ============================================================ */

/**
 * 绘制单个角色小人（含名牌、进度药丸与状态装饰）。
 * @param ctx  Canvas 2D 上下文
 * @param a    agent 对象（见上文字段约定）
 * @param pal  调色板对象（随主题切换）
 * @param time 全局累计时间（秒），用于呼吸/眨眼等周期动画
 */
export function drawAgent(ctx, a, pal, time) {
  const reduced = a.reduced;
  const st = a.stateT;
  const sleeping = a.state === 'sleep';

  // —— 状态驱动的整体位移 / 倾斜 / 压扁拉伸 ——
  let bob = 0;          // 竖直位移（负=上浮）
  let tilt = 0;         // 身体倾斜弧度
  let sx = 1, sy = 1;   // squash & stretch 系数（以脚底为锚缩放）

  if (!reduced) {
    if (a.state === 'idle') {
      bob = Math.sin(time * 2 + a.phase) * 2.4;
    } else if (a.state === 'walk') {
      bob = Math.abs(Math.sin(a.walkPhase * 2)) * -3;
      tilt = Math.sin(a.walkPhase) * 0.05 * a.facing;
    } else if (a.state === 'work') {
      bob = Math.sin(time * 3 + a.phase) * 1.2;
    } else if (a.state === 'wait_llm') {
      tilt = Math.sin(time * 1.6 + a.phase) * 0.05;
    } else if (a.state === 'done') {
      // 两次抛物线跳跃 + squash&stretch：
      // 地面相位压扁（蓄力/落地），滞空相位拉长（升腾感）。
      if (st < 1.2) {
        const p = (st % 0.6) / 0.6;
        const air = Math.sin(p * Math.PI);        // 0 地面 → 1 最高点
        bob = -air * 20;
        sy = lerp(0.95, 1.05, air);
        sx = lerp(1.05, 0.95, air);
      } else {
        bob = Math.sin(time * 3) * 1.6;
      }
    } else if (sleeping) {
      tilt = 0.16 * a.facing;                     // 倚靠站台的坐姿倾斜
      bob = Math.sin(time * 1.1 + a.phase) * 0.8;
    } else if (a.state === 'blocked') {
      tilt = Math.sin(time * 8) * 0.035;          // 气恼的小幅晃动
    }
  }

  const cx = a.x;
  // 坐姿整体下沉：睡觉时臀部落地，身高视觉变矮
  const feetY = a.y + bob + (sleeping ? 6 : 0);
  const bodyBottom = feetY - AG.LEG_H + (sleeping ? 6 : 0);
  const bodyTop = bodyBottom - AG.BODY_H;
  const headCy = bodyTop - AG.HEAD_R + AG.CHIN;
  const outline = shade(a.color, -0.35);          // 同色系深描边

  ctx.save();

  // —— 脚下柔和椭圆投影：随跳起高度缩小淡化 —— //
  if (!reduced) {
    const shScale = clamp(1 - (-Math.min(bob, 0)) / 46, 0.5, 1);
    ctx.save();
    ctx.globalAlpha = (pal.dark ? 0.34 : 0.2) * shScale;
    ctx.fillStyle = pal.dark ? '#000000' : '#26241f';
    ctx.beginPath();
    ctx.ellipse(cx, a.y + 3, 19 * shScale, 5 * shScale, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  // squash&stretch 以脚底为锚：地面接触点不动，符合卡通物理直觉
  ctx.translate(cx, feetY);
  ctx.scale(sx, sy);
  ctx.translate(-cx, -feetY);

  // 倾斜以身体中心为轴
  ctx.translate(cx, (bodyTop + bodyBottom) / 2);
  ctx.rotate(tilt);
  ctx.translate(-cx, -(bodyTop + bodyBottom) / 2);

  // —— 腿与脚 —— //
  drawLegs(ctx, a, cx, bodyBottom, feetY, outline, reduced, sleeping);

  // —— 身体：圆角胶囊 + 腹部浅色面板 + 同色系描边 —— //
  const bw = AG.BODY_W, bh = AG.BODY_H;
  fillRR(ctx, cx - bw / 2, bodyTop, bw, bh, 14, a.color);
  // 腹部/围裙面板：同色系浅一档，界定“工装”气质
  fillRR(ctx, cx - bw / 2 + 5, bodyTop + bh * 0.42, bw - 10, bh * 0.5, 8, withAlpha(shade(a.color, 0.5), 0.92));
  // 左上高光：一条窄弧光提示体积
  ctx.save();
  roundRectPath(ctx, cx - bw / 2, bodyTop, bw, bh, 14);
  ctx.clip();
  ctx.fillStyle = withAlpha('#ffffff', 0.22);
  ctx.beginPath(); ctx.ellipse(cx - bw / 2 + 6, bodyTop + 8, 5, 11, 0.35, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
  strokeRR(ctx, cx - bw / 2, bodyTop, bw, bh, 14, outline, 2);

  // —— 手臂 + 手持道具 —— //
  drawArmsAndProp(ctx, a, cx, bodyTop, bodyBottom, bw, pal, time, reduced, headCy);

  // —— 头部：脸、五官、头饰 —— //
  drawHead(ctx, a, cx, headCy, AG.HEAD_R, pal, time, reduced, outline);

  ctx.restore();

  // —— 状态装饰（气泡/牌子/!/✓/Zzz/汗滴）绘制在最上层，不随倾斜旋转 —— //
  drawStateDecor(ctx, a, cx, feetY, headCy, AG.HEAD_R, pal, time, reduced);

  // —— 名牌 + 迷你进度药丸：头顶常驻，可达性补偿通道 —— //
  const showProg = a.state === 'work' && a.progress && a.progress.total > 0;
  const headTop = headCy - AG.HEAD_R;
  // 有进度药丸时名牌整体上抬，给药丸让出头饰上方的空间
  const tagCy = headTop - 16 - (showProg ? 14 : 0);
  drawNameTag(ctx, a.name, cx, tagCy, a.color, pal, showProg ? a.progress : null);
}

/**
 * 绘制双腿与小脚。
 * walk 态两腿以 walkPhase 反相摆动；sleep 态双腿向前平伸成坐姿。
 */
function drawLegs(ctx, a, cx, hipY, feetY, outline, reduced, sleeping) {
  const legColor = shade(a.color, -0.24);
  if (sleeping) {
    // 坐姿：两条腿向朝向方向平伸，脚尖微翘
    const f = a.facing || 1;
    for (const s of [-1, 1]) {
      fillRR(ctx, cx + s * 5 - 3, hipY - 4, 7 + 6, 7, 3.5, legColor);
      fillRR(ctx, cx + s * 5 + f * 9 - 3, hipY - 5, 8, 6, 3, legColor);
    }
    return;
  }
  const swing = (a.state === 'walk' && !reduced) ? Math.sin(a.walkPhase * 2) * 6 : 0;
  for (const s of [-1, 1]) {
    const hipX = cx + s * 7;
    const footDx = s * swing * 0.9;
    // 大腿：短圆柱
    fillRR(ctx, hipX - 3.5, hipY - 2, 7, AG.LEG_H, 3.5, legColor);
    // 脚：小椭圆鞋，行走时前后错动
    ctx.fillStyle = shade(a.color, -0.4);
    ctx.beginPath();
    ctx.ellipse(hipX + footDx, feetY - 2, 5.6, 3, 0, 0, Math.PI * 2);
    ctx.fill();
  }
}

/**
 * 绘制手臂与手持道具。
 * work 态双手在身前键盘位交替敲打（节律感来自左右手相位差 π/2）；
 * wait_llm 一手扶举牌杆；done 双手上扬；blocked 叉腰；sleep 手放腿上。
 */
function drawArmsAndProp(ctx, a, cx, bodyTop, bodyBottom, bw, pal, time, reduced, headCy) {
  const armColor = shade(a.color, -0.2);
  const shoulderY = bodyTop + 10;
  const st = a.stateT;
  let lHand = { x: cx - bw / 2 - 4, y: shoulderY + 13 };
  let rHand = { x: cx + bw / 2 + 4, y: shoulderY + 13 };

  if (a.state === 'work' && !reduced) {
    // 键盘敲打：左右手交替下压，落点在腹前
    const tapL = Math.abs(Math.sin(time * 7 + 0)) * 6;
    const tapR = Math.abs(Math.sin(time * 7 + Math.PI / 2)) * 6;
    lHand = { x: cx - 9, y: bodyBottom - 8 - tapL };
    rHand = { x: cx + 9, y: bodyBottom - 8 - tapR };
  } else if (a.state === 'walk' && !reduced) {
    const sw = Math.sin(a.walkPhase * 2) * 5;
    lHand = { x: cx - bw / 2 - 4, y: shoulderY + 13 + sw };
    rHand = { x: cx + bw / 2 + 4, y: shoulderY + 13 - sw };
  } else if (a.state === 'done' && st < 1.4 && !reduced) {
    lHand = { x: cx - bw / 2 - 8, y: shoulderY - 12 };
    rHand = { x: cx + bw / 2 + 8, y: shoulderY - 12 };
  } else if (a.state === 'blocked') {
    lHand = { x: cx - bw / 2 + 3, y: shoulderY + 16 };
    rHand = { x: cx + bw / 2 - 3, y: shoulderY + 16 };
  } else if (a.state === 'sleep') {
    lHand = { x: cx - 8, y: bodyBottom - 3 };
    rHand = { x: cx + 8, y: bodyBottom - 3 };
  } else if (a.state === 'wait_llm') {
    // 右手上举扶牌杆（牌面画在 drawStateDecor，杆底接到这只手）
    rHand = { x: cx + 13, y: headCy - AG.HEAD_R - 4 };
  }

  ctx.strokeStyle = armColor;
  ctx.lineWidth = 6.5;
  ctx.lineCap = 'round';
  ctx.beginPath();
  ctx.moveTo(cx - bw / 2 + 4, shoulderY); ctx.lineTo(lHand.x, lHand.y);
  ctx.moveTo(cx + bw / 2 - 4, shoulderY); ctx.lineTo(rHand.x, rHand.y);
  ctx.stroke();
  // 小手：肤色圆点收尾，避免手臂末端生硬
  ctx.fillStyle = pal.faceFill;
  for (const h of [lHand, rHand]) {
    ctx.beginPath(); ctx.arc(h.x, h.y, 3.4, 0, Math.PI * 2); ctx.fill();
  }

  // 手持道具：仅 idle/walk 呈现（work 时双手在敲打，道具视为放在桌面）
  if (a.prop && (a.state === 'idle' || a.state === 'walk')) {
    ctx.save();
    ctx.translate(rHand.x, rHand.y);
    ctx.scale(1.7, 1.7);
    drawHeldProp(ctx, a.prop, 0, 0, a.facing, pal, a.color);
    ctx.restore();
  }
}

/** 绘制角色手持道具：不同角色拿不同小物件，强化辨识度。 */
function drawHeldProp(ctx, prop, x, y, facing, pal, color) {
  ctx.save();
  ctx.translate(x, y);
  if (prop === 'flag') {
    ctx.strokeStyle = pal.weak; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(0, -10); ctx.lineTo(0, 6); ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.moveTo(0, -10); ctx.lineTo(9 * facing, -7); ctx.lineTo(0, -4); ctx.closePath(); ctx.fill();
  } else if (prop === 'folder') {
    fillRR(ctx, -5, -4, 12, 9, 1.5, shade(color, 0.15));
    ctx.fillStyle = withAlpha('#ffffff', 0.7); ctx.fillRect(-5, -4, 12, 2);
  } else if (prop === 'gear') {
    ctx.fillStyle = shade(color, 0.2);
    for (let i = 0; i < 6; i++) { const an = i / 6 * Math.PI * 2; ctx.fillRect(Math.cos(an) * 4 - 1.5, Math.sin(an) * 4 - 1.5, 3, 3); }
    ctx.beginPath(); ctx.arc(0, 0, 3, 0, Math.PI * 2); ctx.fill();
  } else if (prop === 'calc') {
    fillRR(ctx, -5, -6, 10, 12, 1.5, shade(color, 0.2));
    ctx.fillStyle = pal.good; ctx.fillRect(-3.5, -4.5, 7, 3);
    ctx.fillStyle = withAlpha('#ffffff', 0.6);
    for (let r = 0; r < 2; r++) for (let c = 0; c < 3; c++) ctx.fillRect(-3.5 + c * 3, 0 + r * 3, 1.6, 1.6);
  } else if (prop === 'scale') {
    ctx.strokeStyle = shade(color, 0.1); ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(-6, -3); ctx.lineTo(6, -3); ctx.moveTo(0, -3); ctx.lineTo(0, 5); ctx.stroke();
    ctx.beginPath(); ctx.arc(-6, 0, 2.4, 0, Math.PI); ctx.arc(6, 0, 2.4, 0, Math.PI); ctx.stroke();
  } else if (prop === 'radar') {
    ctx.strokeStyle = shade(color, 0.15); ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.moveTo(0, 5); ctx.lineTo(0, -8); ctx.stroke();
    ctx.beginPath(); ctx.arc(0, -8, 2, 0, Math.PI * 2); ctx.fillStyle = pal.critical; ctx.fill();
  } else if (prop === 'map') {
    fillRR(ctx, -6, -4, 12, 8, 1.5, shade(color, 0.25));
    ctx.strokeStyle = withAlpha('#ffffff', 0.55); ctx.lineWidth = 0.8;
    ctx.beginPath(); ctx.moveTo(-4, 0); ctx.lineTo(4, -2); ctx.stroke();
  } else if (prop === 'scope') {
    fillRR(ctx, -2, -7, 5, 12, 2, shade(color, 0.15));
    ctx.fillStyle = pal.seq2; ctx.beginPath(); ctx.arc(0.5, -7, 2.6, 0, Math.PI * 2); ctx.fill();
  }
  ctx.restore();
}

/* ------------------------------------------------------------
 * 头部：脸、大眼、腮红、状态嘴型、专属头饰
 * ------------------------------------------------------------ */

/**
 * 绘制头部。
 * 五官布局随 facing 微移形成“转头看路”的方向感；
 * 头饰在最后绘制，永远压在头发/脸之上，且全部使用头局部坐标 —— 头动饰动，
 * 不存在独立动画，从根上杜绝穿模。
 */
function drawHead(ctx, a, cx, cy, r, pal, time, reduced, outline) {
  const off = a.facing * 1.6;   // 五官朝向偏移

  // 脸：浅肤色大圆 + 同色系柔和描边
  ctx.fillStyle = pal.faceFill;
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill();
  ctx.strokeStyle = withAlpha(shade(a.color, -0.35), 0.55);
  ctx.lineWidth = 1.8;
  ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();

  // 眼睛：白色大眼 + 深色瞳仁 + 高光点；blink/sleep 时收成弧线
  const eyeDx = 6, eyeY = cy - 1;
  const closed = (a.state === 'sleep') || (!reduced && a.blink > 0.6);
  for (const s of [-1, 1]) {
    const ex = cx + s * eyeDx + off;
    if (closed) {
      ctx.strokeStyle = pal.eye; ctx.lineWidth = 1.8; ctx.lineCap = 'round';
      ctx.beginPath(); ctx.arc(ex, eyeY, 3.4, Math.PI * 0.15, Math.PI * 0.85); ctx.stroke();
    } else {
      ctx.fillStyle = '#ffffff';
      ctx.beginPath(); ctx.arc(ex, eyeY, 4.6, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = withAlpha(shade(a.color, -0.35), 0.3); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(ex, eyeY, 4.6, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = pal.eye;
      ctx.beginPath(); ctx.arc(ex + off * 0.8, eyeY + 0.4, 2.3, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#ffffff';
      ctx.beginPath(); ctx.arc(ex + off * 0.8 - 0.8, eyeY - 0.6, 0.95, 0, Math.PI * 2); ctx.fill();
    }
  }

  // 腮红两枚：固定暖粉，不随角色色走，保证亲和力一致
  ctx.fillStyle = withAlpha('#ef8f8f', 0.4);
  for (const s of [-1, 1]) {
    ctx.beginPath(); ctx.ellipse(cx + s * 8.5 + off, cy + 4.5, 2.9, 1.9, 0, 0, Math.PI * 2); ctx.fill();
  }

  // 嘴：按状态切换表情
  drawMouth(ctx, a, cx + off, cy + 7.2, pal);

  // 专属头饰（最后画，覆盖在头顶）
  drawHeadwear(ctx, a, cx, cy, r, pal, off);
}

/** 状态嘴型：work 专注抿嘴 / done 开口笑 / blocked 波浪嘴 / sleep 小 o / 其余浅笑。 */
function drawMouth(ctx, a, mx, my, pal) {
  ctx.strokeStyle = pal.eye;
  ctx.lineWidth = 1.6;
  ctx.lineCap = 'round';
  if (a.state === 'done') {
    // 开口笑：填充半圆嘴 + 小舌头
    ctx.fillStyle = shade(pal.eye, 0.12);
    ctx.beginPath(); ctx.arc(mx, my - 1, 3.6, 0, Math.PI); ctx.closePath(); ctx.fill();
    ctx.fillStyle = '#e98080';
    ctx.beginPath(); ctx.arc(mx, my + 1.4, 1.7, 0, Math.PI); ctx.fill();
  } else if (a.state === 'blocked') {
    // 波浪嘴：为难/受阻情绪
    ctx.beginPath();
    ctx.moveTo(mx - 4.5, my);
    ctx.quadraticCurveTo(mx - 2.2, my - 2.4, mx, my);
    ctx.quadraticCurveTo(mx + 2.2, my + 2.4, mx + 4.5, my);
    ctx.stroke();
  } else if (a.state === 'sleep') {
    // 熟睡小 o
    ctx.beginPath(); ctx.arc(mx, my + 0.5, 1.8, 0, Math.PI * 2); ctx.stroke();
  } else if (a.state === 'work' || a.state === 'wait_llm') {
    // 专注抿嘴：短平线
    ctx.beginPath(); ctx.moveTo(mx - 2.8, my); ctx.lineTo(mx + 2.8, my); ctx.stroke();
  } else {
    // 默认浅笑
    ctx.beginPath(); ctx.arc(mx, my - 1.2, 3.2, Math.PI * 0.2, Math.PI * 0.8); ctx.stroke();
  }
}

/**
 * 角色专属头饰。
 * 设计约束：全部几何相对头心 (cx,cy) 定位，不含独立动画项，
 * 保证任何状态（含跳跃/倾斜）下头饰与头刚性同步。
 */
function drawHeadwear(ctx, a, cx, cy, r, pal, off) {
  const c = a.color;
  const dark = shade(c, -0.35);
  const lite = shade(c, 0.45);
  const f = a.facing || 1;
  const eyeY = cy - 1;

  switch (a.owner) {
    case 'orchestrator': {
      // 金色贝雷帽：偏一侧的软塌椭圆 + 顶部小蒂；耳麦：耳罩 + 话筒杆
      ctx.save();
      ctx.translate(cx - 2, cy - r + 3);
      ctx.rotate(-0.14);
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.ellipse(0, 0, 14.5, 6.4, 0, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = dark; ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.ellipse(0, 0, 14.5, 6.4, 0, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = dark;
      ctx.beginPath(); ctx.arc(3, -5.5, 1.8, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
      // 耳麦：右耳耳罩 + 弧形话筒
      ctx.fillStyle = dark;
      ctx.beginPath(); ctx.arc(cx + r - 2, cy + 1, 3.4, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = dark; ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.moveTo(cx + r - 2, cy + 4); ctx.quadraticCurveTo(cx + 9, cy + 10, cx + 4 + off, cy + 9); ctx.stroke();
      ctx.fillStyle = pal.critical;
      ctx.beginPath(); ctx.arc(cx + 4 + off, cy + 9, 1.5, 0, Math.PI * 2); ctx.fill();
      break;
    }
    case 'information-collector': {
      // 蓝色鸭舌帽：上半头盔形帽冠 + 朝向侧的帽檐 + 顶扣
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.arc(cx, cy - 2.5, r - 0.5, Math.PI, Math.PI * 2); ctx.closePath(); ctx.fill();
      ctx.strokeStyle = dark; ctx.lineWidth = 1.6;
      ctx.beginPath(); ctx.arc(cx, cy - 2.5, r - 0.5, Math.PI, Math.PI * 2); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(cx - r + 0.5, cy - 2.5); ctx.lineTo(cx + r - 0.5, cy - 2.5); ctx.stroke();
      // 帽檐
      ctx.fillStyle = shade(c, -0.12);
      ctx.beginPath(); ctx.ellipse(cx + f * 13, cy - 4.5, 9.5, 3, f * 0.14, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = dark; ctx.lineWidth = 1.3;
      ctx.beginPath(); ctx.ellipse(cx + f * 13, cy - 4.5, 9.5, 3, f * 0.14, 0, Math.PI * 2); ctx.stroke();
      // 顶扣
      ctx.fillStyle = lite;
      ctx.beginPath(); ctx.arc(cx, cy - r - 1, 2, 0, Math.PI * 2); ctx.fill();
      break;
    }
    case 'information-processor': {
      // 青绿护目镜推在额头：宽绑带 + 双镜片 + 玻璃高光
      ctx.strokeStyle = withAlpha(dark, 0.9); ctx.lineWidth = 3.4;
      ctx.beginPath(); ctx.arc(cx, cy, r - 1.2, Math.PI * 1.12, Math.PI * 1.88); ctx.stroke();
      for (const s of [-1, 1]) {
        const gx = cx + s * 6, gy = cy - r + 5.5;
        ctx.fillStyle = withAlpha(lite, 0.95);
        ctx.beginPath(); ctx.arc(gx, gy, 4.6, 0, Math.PI * 2); ctx.fill();
        ctx.strokeStyle = dark; ctx.lineWidth = 1.7;
        ctx.beginPath(); ctx.arc(gx, gy, 4.6, 0, Math.PI * 2); ctx.stroke();
        ctx.fillStyle = withAlpha('#ffffff', 0.75);
        ctx.beginPath(); ctx.arc(gx - 1.4, gy - 1.4, 1.3, 0, Math.PI * 2); ctx.fill();
      }
      break;
    }
    case 'financial-analyst': {
      // 紫框圆眼镜：戴在眼睛上 + 鼻梁桥 + 两侧镜腿
      ctx.strokeStyle = c; ctx.lineWidth = 2;
      for (const s of [-1, 1]) {
        ctx.beginPath(); ctx.arc(cx + s * 6 + off, eyeY, 5.8, 0, Math.PI * 2); ctx.stroke();
      }
      ctx.beginPath(); ctx.moveTo(cx - 0.2 + off, eyeY - 1.5); ctx.quadraticCurveTo(cx + off, eyeY - 3, cx + 0.2 + off, eyeY - 1.5); ctx.stroke();
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      ctx.moveTo(cx - 11.8 + off, eyeY - 1); ctx.lineTo(cx - r + 1, eyeY - 2.5);
      ctx.moveTo(cx + 11.8 + off, eyeY - 1); ctx.lineTo(cx + r - 1, eyeY - 2.5);
      ctx.stroke();
      break;
    }
    case 'valuation-analyst': {
      // 橙色遮阳帽檐：环头绑带 + 朝向侧宽檐；算珠发卡：三粒小珠
      ctx.strokeStyle = dark; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.arc(cx, cy - 1, r - 0.8, Math.PI * 1.08, Math.PI * 1.92); ctx.stroke();
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.ellipse(cx + f * 11, cy - r + 6, 11, 3.6, f * 0.18, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = dark; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.ellipse(cx + f * 11, cy - r + 6, 11, 3.6, f * 0.18, 0, Math.PI * 2); ctx.stroke();
      // 算珠发卡：反侧三粒渐变小珠串在细杆上
      ctx.strokeStyle = dark; ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.moveTo(cx - f * 6, cy - r + 1.5); ctx.lineTo(cx - f * 13, cy - r + 5); ctx.stroke();
      const beads = [lite, shade(c, -0.1), lite];
      beads.forEach((bc, i) => {
        const t = (i + 0.5) / 3;
        ctx.fillStyle = bc;
        ctx.beginPath(); ctx.arc(lerp(cx - f * 6, cx - f * 13, t), lerp(cy - r + 1.5, cy - r + 5, t), 1.7, 0, Math.PI * 2); ctx.fill();
      });
      break;
    }
    case 'market-context-collector': {
      // 红色头戴耳机：头带 + 双耳罩 + 小天线（天线定在头带正上，不摆动）
      ctx.strokeStyle = dark; ctx.lineWidth = 3.4;
      ctx.beginPath(); ctx.arc(cx, cy + 1, r + 1.5, Math.PI * 1.06, Math.PI * 1.94); ctx.stroke();
      for (const s of [-1, 1]) {
        fillRR(ctx, cx + s * (r - 0.5) - 3, cy - 4, 6, 10, 3, c);
        strokeRR(ctx, cx + s * (r - 0.5) - 3, cy - 4, 6, 10, 3, dark, 1.5);
      }
      ctx.strokeStyle = dark; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(cx + 6, cy - r - 2.5); ctx.lineTo(cx + 9, cy - r - 9); ctx.stroke();
      ctx.fillStyle = pal.critical;
      ctx.beginPath(); ctx.arc(cx + 9, cy - r - 9, 2, 0, Math.PI * 2); ctx.fill();
      break;
    }
    case 'industry-info-collector': {
      // 绿色探险帽：宽帽檐 + 圆顶帽冠 + 深色帽带
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.ellipse(cx, cy - r + 6.5, 16.5, 4.2, 0, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = dark; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.ellipse(cx, cy - r + 6.5, 16.5, 4.2, 0, 0, Math.PI * 2); ctx.stroke();
      fillRR(ctx, cx - 9.5, cy - r - 5.5, 19, 12, 6, c);
      strokeRR(ctx, cx - 9.5, cy - r - 5.5, 19, 12, 6, dark, 1.5);
      ctx.fillStyle = dark;
      ctx.fillRect(cx - 9.5, cy - r + 2.2, 19, 2.6);
      break;
    }
    case 'industry-researcher': {
      // 粉色发带：宽弧带 + 侧蝴蝶结；单边眼镜：单圆镜 + 垂链
      ctx.strokeStyle = c; ctx.lineWidth = 4.2;
      ctx.beginPath(); ctx.arc(cx, cy + 0.5, r - 0.6, Math.PI * 1.12, Math.PI * 1.88); ctx.stroke();
      // 蝴蝶结：两片三角 + 中心结
      ctx.save();
      ctx.translate(cx - f * 10, cy - r + 3.5);
      ctx.fillStyle = shade(c, -0.08);
      ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(-5.5, -4); ctx.lineTo(-4.5, 1.5); ctx.closePath(); ctx.fill();
      ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(5, -4.5); ctx.lineTo(5.5, 1); ctx.closePath(); ctx.fill();
      ctx.fillStyle = dark;
      ctx.beginPath(); ctx.arc(0, 0, 1.9, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
      // 单边眼镜（朝向侧眼睛）
      ctx.strokeStyle = shade(c, -0.25); ctx.lineWidth = 1.8;
      ctx.beginPath(); ctx.arc(cx + f * 6 + off, eyeY, 5.6, 0, Math.PI * 2); ctx.stroke();
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx + f * 6 + off, eyeY + 5.6); ctx.quadraticCurveTo(cx + f * 9 + off, eyeY + 10, cx + f * 7 + off, eyeY + 12); ctx.stroke();
      break;
    }
    default: {
      // 兜底：小天线球（未知角色仍有辨识点）
      ctx.strokeStyle = c; ctx.lineWidth = 1.6; ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(cx, cy - r + 1); ctx.lineTo(cx + 3, cy - r - 6); ctx.stroke();
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.arc(cx + 3, cy - r - 6, 2.2, 0, Math.PI * 2); ctx.fill();
    }
  }
}

/* ------------------------------------------------------------
 * 状态装饰物
 * ------------------------------------------------------------ */

/**
 * 绘制状态装饰：
 *   work → “…”思考泡 + 周期性汗滴；wait_llm → 大举牌 + 呼吸光圈；
 *   blocked → 红 ! 弹跳；done → ✓ 粒子；sleep → 逐字放大的 Zzz。
 * 名牌/进度药丸不在此处（见 drawAgent 尾部），保证层级清晰。
 */
function drawStateDecor(ctx, a, cx, feetY, headCy, r, pal, time, reduced) {
  const headTop = headCy - r;
  const topY = headTop - 16 - 22;    // 名牌上方的装饰锚点

  if (a.state === 'work') {
    if (!(a.progress && a.progress.total > 0)) {
      // “…”三点循环：无量化进度时表达“正在忙”
      drawBubble(ctx, cx, topY, 34, 18, pal);
      const n = reduced ? 3 : (Math.floor(time * 3) % 3) + 1;
      ctx.fillStyle = pal.mut;
      for (let i = 0; i < 3; i++) {
        ctx.globalAlpha = i < n ? 1 : 0.25;
        ctx.beginPath(); ctx.arc(cx - 7 + i * 7, topY, 2, 0, Math.PI * 2); ctx.fill();
      }
      ctx.globalAlpha = 1;
    }
    // 汗滴：每 2.6s 从额角滑落一颗，强化“卖力干活”
    if (!reduced) {
      const t = (time + a.phase) % 2.6;
      if (t < 0.9) {
        const p = t / 0.9;
        ctx.globalAlpha = 1 - p;
        ctx.fillStyle = '#7db6e8';
        ctx.beginPath();
        const dx = cx + (a.facing || 1) * (r - 3);
        const dy = headCy - r + 6 + p * 12;
        ctx.moveTo(dx, dy - 3.2);
        ctx.quadraticCurveTo(dx + 2.6, dy + 1.2, dx, dy + 2.6);
        ctx.quadraticCurveTo(dx - 2.6, dy + 1.2, dx, dy - 3.2);
        ctx.fill();
        ctx.globalAlpha = 1;
      }
    }
  } else if (a.state === 'wait_llm') {
    // 呼吸光圈：站位脚下泛开的等待暗示
    if (!reduced) {
      const gr = 30 + Math.sin(time * 2) * 4;
      ctx.strokeStyle = withAlpha(pal.warning, 0.45); ctx.lineWidth = 2.5;
      ctx.beginPath(); ctx.ellipse(cx, a.y - 2, gr, gr * 0.4, 0, 0, Math.PI * 2); ctx.stroke();
    }
    // 大举牌：几何与 signHitRect 同源；绕杆底轻摆表达“耐心等待”
    const rect = signHitRect(a);
    const signCx = rect.x + rect.w / 2, signCy = rect.y + rect.h / 2;
    const poleBaseX = cx + 13, poleBaseY = headTop - 4;
    const sway = reduced ? 0 : Math.sin(time * 1.5 + a.phase) * 0.045;
    ctx.save();
    ctx.translate(poleBaseX, poleBaseY);
    ctx.rotate(sway);
    ctx.translate(-poleBaseX, -poleBaseY);
    // 牌杆
    ctx.strokeStyle = pal.weak; ctx.lineWidth = 2.5; ctx.lineCap = 'round';
    ctx.beginPath(); ctx.moveTo(signCx, signCy + rect.h / 2); ctx.lineTo(poleBaseX, poleBaseY); ctx.stroke();
    // 牌面：白底 + 警示色边 + 可读大字
    fillRR(ctx, rect.x, rect.y, rect.w, rect.h, 8, pal.card);
    strokeRR(ctx, rect.x, rect.y, rect.w, rect.h, 8, pal.warning, 2);
    ctx.fillStyle = pal.fg;
    ctx.font = 'bold 13px system-ui';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('等待 Claude 分析', signCx, signCy + 0.5);
    ctx.restore();
  } else if (a.state === 'blocked') {
    // 红色 “!” 弹跳 + 白色描底保证深色主题可读
    const by = topY - (reduced ? 0 : Math.abs(Math.sin(time * 5)) * 7);
    ctx.font = 'bold 24px system-ui'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.lineWidth = 4; ctx.strokeStyle = withAlpha(pal.dark ? '#000000' : '#ffffff', 0.7);
    ctx.strokeText('!', cx, by);
    ctx.fillStyle = pal.critical;
    ctx.fillText('!', cx, by);
  } else if (a.state === 'done') {
    // 绿色 ✓ 粒子：从头顶向四周飘散
    if (!reduced && a.stateT < 1.6) {
      for (let i = 0; i < 6; i++) {
        const seed = (i * 97) % 100 / 100;
        const life = (a.stateT + seed) % 1.6 / 1.6;
        const ang = seed * Math.PI * 2;
        ctx.globalAlpha = 1 - life;
        ctx.fillStyle = pal.good; ctx.font = 'bold 13px system-ui'; ctx.textAlign = 'center';
        ctx.fillText('✓', cx + Math.cos(ang) * life * 26, topY - life * 30);
      }
      ctx.globalAlpha = 1;
    } else {
      ctx.fillStyle = pal.good; ctx.font = 'bold 15px system-ui'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('✓', cx, topY);
    }
  } else if (a.state === 'sleep') {
    // Zzz：逐字放大、向右上漂浮、渐隐
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillStyle = pal.weak;
    for (let i = 0; i < 3; i++) {
      const t2 = reduced ? (i * 0.33) : ((time * 0.55 + i * 0.38) % 1.3);
      ctx.globalAlpha = reduced ? 0.7 : clamp(1 - t2 / 1.3, 0, 1);
      ctx.font = `bold ${11 + i * 4}px system-ui`;
      ctx.fillText('Z', cx + 12 + i * 8 + (reduced ? 0 : t2 * 8), headTop - 4 - i * 9 - (reduced ? 0 : t2 * 16));
    }
    ctx.globalAlpha = 1;
  }
}

/** 绘制一个白底圆角对话气泡（含小尾巴）。 */
function drawBubble(ctx, cx, cy, w, h, pal) {
  fillRR(ctx, cx - w / 2, cy - h / 2, w, h, 7, pal.card);
  strokeRR(ctx, cx - w / 2, cy - h / 2, w, h, 7, pal.border, 1);
  ctx.fillStyle = pal.card;
  ctx.beginPath();
  ctx.moveTo(cx - 3, cy + h / 2 - 1); ctx.lineTo(cx + 3, cy + h / 2 - 1); ctx.lineTo(cx, cy + h / 2 + 5); ctx.closePath();
  ctx.fill();
}

/**
 * 绘制头顶名牌（白底圆角药丸签）+ 可选迷你进度药丸。
 * 名牌：左侧角色色圆点 + 墨色角色名 + 细边框 + 微投影；
 * 进度药丸：名牌下方一枚小胶囊，进度填充为底、done/total 文字居中，
 * 用“底色填充比例”代替独立进度条，避免小尺寸下两层元素挤在一起。
 * @param progress {done,total} 或 null
 */
export function drawNameTag(ctx, name, cx, cy, color, pal, progress) {
  ctx.save();
  ctx.font = 'bold 11px system-ui';
  const tw = ctx.measureText(name).width;
  const w = tw + 26, h = 20;

  // 微投影：深色主题用黑色低透明度，浅色主题用墨色低透明度，
  // 两种主题下都能把白底药丸从背景里衬出来
  ctx.save();
  ctx.shadowColor = pal.dark ? 'rgba(0,0,0,.55)' : 'rgba(38,36,31,.25)';
  ctx.shadowBlur = 5;
  ctx.shadowOffsetY = 1.5;
  fillRR(ctx, cx - w / 2, cy - h / 2, w, h, 10, pal.card);
  ctx.restore();
  strokeRR(ctx, cx - w / 2, cy - h / 2, w, h, 10, pal.border, 1);

  // 左侧角色色圆点
  ctx.fillStyle = color;
  ctx.beginPath(); ctx.arc(cx - w / 2 + 10, cy, 4, 0, Math.PI * 2); ctx.fill();
  // 角色名：墨色，永远保证对比度
  ctx.fillStyle = pal.fg;
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(name, cx - w / 2 + 18, cy + 0.5);

  // 迷你进度药丸
  if (progress && progress.total > 0) {
    const pw = 52, ph = 13, py = cy + h / 2 + 8;
    const frac = clamp(progress.done / progress.total, 0, 1);
    fillRR(ctx, cx - pw / 2, py - ph / 2, pw, ph, 6.5, pal.card);
    // 进度填充：截取左侧 frac 比例区域着色
    ctx.save();
    roundRectPath(ctx, cx - pw / 2, py - ph / 2, pw, ph, 6.5);
    ctx.clip();
    ctx.fillStyle = withAlpha(pal.seq3, 0.3);
    ctx.fillRect(cx - pw / 2, py - ph / 2, pw * frac, ph);
    ctx.restore();
    strokeRR(ctx, cx - pw / 2, py - ph / 2, pw, ph, 6.5, pal.border, 1);
    ctx.fillStyle = pal.fg;
    ctx.font = 'bold 9px system-ui';
    ctx.textAlign = 'center';
    ctx.fillText(`${progress.done}/${progress.total}`, cx, py + 0.5);
  }
  ctx.restore();
}

/* ============================================================
 * 四、站点绘制
 * station 对象约定字段：
 *   x, y / color / kind / label / active / dim / subLights[4] /
 *   digestProgress / tray {count}
 * 放大策略：道具几何沿用旧版局部坐标，外层统一 scale(PROP_SCALE)。
 * 这样一处常量即可整体调比例，且线宽随之放大保持视觉密度一致。
 * ============================================================ */

const PROP_SCALE = 1.9;

/**
 * 绘制站点（台面 + 投影 + 悬挂站名牌 + 专属道具 + 复用蒙层）。
 * @param ctx  上下文
 * @param s    station 对象
 * @param pal  调色板
 * @param time 累计时间
 */
export function drawStation(ctx, s, pal, time) {
  ctx.save();

  // 台面投影：地面软椭圆
  ctx.fillStyle = pal.dark ? 'rgba(0,0,0,.5)' : 'rgba(38,36,31,.14)';
  ctx.globalAlpha = 0.55;
  ctx.beginPath(); ctx.ellipse(s.x, s.y + 8, 88, 15, 0, 0, Math.PI * 2); ctx.fill();
  ctx.globalAlpha = 1;

  // 活跃呼吸光圈
  if (s.active) {
    const pr = 98 + Math.sin(time * 2) * 6;
    ctx.strokeStyle = withAlpha(s.color, 0.35); ctx.lineWidth = 3;
    ctx.beginPath(); ctx.ellipse(s.x, s.y + 4, pr, pr * 0.36, 0, 0, Math.PI * 2); ctx.stroke();
  }

  // 台面底座：矮台 + 角色色描边 + 台沿高光
  const deskFill = pal.dark ? shade('#1e1e1c', 0.06) : shade('#f3f2ee', -0.05);
  fillRR(ctx, s.x - 76, s.y - 34, 152, 40, 10, deskFill);
  ctx.fillStyle = withAlpha(s.color, pal.dark ? 0.3 : 0.22);
  fillRR(ctx, s.x - 76, s.y - 34, 152, 9, 5, withAlpha(s.color, pal.dark ? 0.3 : 0.22));
  strokeRR(ctx, s.x - 76, s.y - 34, 152, 40, 10, withAlpha(shade(s.color, -0.1), 0.55), 2);

  // 站点专属道具：平移到台面顶沿后整体放大
  ctx.save();
  ctx.translate(s.x, s.y - 34);
  ctx.scale(PROP_SCALE, PROP_SCALE);
  switch (s.kind) {
    case 'orchestrator': drawPropOrchestrator(ctx, s, pal, time); break;
    case 'collector': drawPropCollector(ctx, s, pal, time); break;
    case 'processor': drawPropProcessor(ctx, s, pal, time); break;
    case 'financial': drawPropFinancial(ctx, s, pal, time); break;
    case 'market': drawPropMarket(ctx, s, pal, time); break;
    case 'valuation': drawPropValuation(ctx, s, pal, time); break;
    case 'deliver': drawPropDeliver(ctx, s, pal, time); break;
    case 'industry_map': drawPropIndustryMap(ctx, s, pal, time); break;
    case 'gate': drawPropGate(ctx, s, pal, time); break;
    case 'industry_research': drawPropIndustryResearch(ctx, s, pal, time); break;
    default: break;
  }
  ctx.restore();

  // 悬挂站名牌：置于道具上方，避开站前的小人与名牌
  drawStationSign(ctx, s, pal);

  // 复用置灰：半透明蒙层 + 斜盖“复用”印章（随站台等比放大）
  if (s.dim) {
    fillRR(ctx, s.x - 88, s.y - 116, 176, 134, 12, withAlpha(pal.page, 0.5));
    ctx.save();
    ctx.translate(s.x + 42, s.y - 86); ctx.rotate(-0.18);
    strokeRR(ctx, -34, -14, 68, 28, 6, pal.good, 2.6);
    ctx.fillStyle = pal.good; ctx.font = 'bold 17px system-ui'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('复用', 0, 1);
    ctx.restore();
  }

  ctx.restore();
}

/** 悬挂式站名牌：白底药丸 + 角色色点 + 站名，随站台比例放大后仍居道具上方。 */
function drawStationSign(ctx, s, pal) {
  const label = s.label || '';
  if (!label) return;
  ctx.save();
  ctx.font = 'bold 12.5px system-ui';
  const tw = ctx.measureText(label).width;
  const w = tw + 28, h = 22, cy = s.y - 126;
  ctx.save();
  ctx.shadowColor = pal.dark ? 'rgba(0,0,0,.5)' : 'rgba(38,36,31,.2)';
  ctx.shadowBlur = 4; ctx.shadowOffsetY = 1.5;
  fillRR(ctx, s.x - w / 2, cy - h / 2, w, h, 11, pal.card);
  ctx.restore();
  strokeRR(ctx, s.x - w / 2, cy - h / 2, w, h, 11, withAlpha(s.color, 0.55), 1.5);
  ctx.fillStyle = s.color;
  ctx.beginPath(); ctx.arc(s.x - w / 2 + 11, cy, 4, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = pal.fg;
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(label, s.x - w / 2 + 19, cy + 0.5);
  ctx.restore();
}

/**
 * 收件托盘：站台前缘左侧的小托盘，落地的文件逐张堆叠。
 * 最多显示 4 张（超过挂 ×N 计数徽标）。卡片偏转角用索引伪随机固定，
 * 避免每帧随机导致的抖动。
 * @param s station 对象（读 s.tray.count）
 */
export function drawInboxTray(ctx, s, pal) {
  const count = s.tray ? s.tray.count : 0;
  if (count <= 0) return;
  const tx = s.x - 62, ty = s.y + 22;
  ctx.save();

  // 托盘盘体：角色色深调的扁平盘
  fillRR(ctx, tx - 26, ty, 52, 9, 3.5, withAlpha(shade(s.color, -0.2), pal.dark ? 0.55 : 0.4));
  strokeRR(ctx, tx - 26, ty, 52, 9, 3.5, withAlpha(shade(s.color, -0.35), 0.5), 1);

  // 堆叠文档卡：从盘底向上，每张固定的轻微偏转
  const visible = Math.min(count, 4);
  for (let i = 0; i < visible; i++) {
    const rot = (((i * 53) % 9) - 4) * 0.022;   // 确定性偏转：-0.088..0.088 rad
    const cy = ty - 4 - i * 5;
    ctx.save();
    ctx.translate(tx, cy);
    ctx.rotate(rot);
    fillRR(ctx, -13, -8, 26, 16, 2, pal.card);
    strokeRR(ctx, -13, -8, 26, 16, 2, pal.border, 1);
    // 折角
    ctx.fillStyle = pal.inset;
    ctx.beginPath(); ctx.moveTo(6, -8); ctx.lineTo(13, -8); ctx.lineTo(13, -1); ctx.closePath(); ctx.fill();
    // 文本线
    ctx.strokeStyle = withAlpha(s.color, 0.55); ctx.lineWidth = 1;
    for (let l = 0; l < 2; l++) {
      ctx.beginPath(); ctx.moveTo(-9, -2 + l * 4); ctx.lineTo(5, -2 + l * 4); ctx.stroke();
    }
    ctx.restore();
  }

  // 超出 4 张：×N 计数徽标
  if (count > 4) {
    const text = `×${count}`;
    ctx.font = 'bold 10px system-ui';
    const bw = ctx.measureText(text).width + 10;
    fillRR(ctx, tx + 18, ty - 26, bw, 15, 7.5, pal.card);
    strokeRR(ctx, tx + 18, ty - 26, bw, 15, 7.5, pal.border, 1);
    ctx.fillStyle = pal.fg; ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ctx.fillText(text, tx + 23, ty - 18);
  }
  ctx.restore();
}

/* ------------------------------------------------------------
 * 站点专属道具（局部坐标：原点 = 台面顶沿中心，向上为负）
 * 几何沿用旧版，由 drawStation 外层 scale 统一放大。
 * ------------------------------------------------------------ */

/** 调度台：指挥屏 + 旗子。 */
function drawPropOrchestrator(ctx, s, pal, time) {
  fillRR(ctx, -22, -34, 34, 24, 3, shade(s.color, -0.2));
  fillRR(ctx, -19, -31, 28, 18, 2, withAlpha(pal.seq2, 0.85));
  ctx.strokeStyle = withAlpha('#ffffff', 0.85); ctx.lineWidth = 1.2;
  ctx.beginPath();
  for (let i = 0; i <= 6; i++) {
    const x = -18 + i * 4.4;
    const y = -22 + Math.sin(time * 2 + i) * 4;
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.strokeStyle = pal.weak; ctx.lineWidth = 1.6;
  ctx.beginPath(); ctx.moveTo(18, -34); ctx.lineTo(18, -2); ctx.stroke();
  const wave = Math.sin(time * 3) * 2;
  ctx.fillStyle = s.color;
  ctx.beginPath(); ctx.moveTo(18, -34); ctx.quadraticCurveTo(26, -32 + wave, 30, -30); ctx.lineTo(30, -22 + wave); ctx.quadraticCurveTo(26, -24, 18, -26); ctx.closePath(); ctx.fill();
}

/** 资料采集站：档案架 + 一摞 PDF 箱。 */
function drawPropCollector(ctx, s, pal, time) {
  fillRR(ctx, -26, -34, 22, 30, 2, shade(s.color, -0.15));
  ctx.fillStyle = withAlpha('#ffffff', 0.25);
  for (let i = 0; i < 3; i++) ctx.fillRect(-24, -31 + i * 9, 18, 6);
  const boxColors = [shade(s.color, 0.1), shade(s.color, 0.25), shade(s.color, 0.0)];
  const stacks = [[6, -12], [20, -12], [13, -22]];
  stacks.forEach((p, i) => {
    fillRR(ctx, p[0] - 7, p[1], 14, 12, 2, boxColors[i % 3]);
    ctx.fillStyle = withAlpha('#ffffff', 0.7); ctx.font = '6px system-ui'; ctx.textAlign = 'center';
    ctx.fillText('PDF', p[0], p[1] + 8);
  });
}

/** 解析车间：传送带 + 四子工位指示灯（digest 工位带进度条）。 */
function drawPropProcessor(ctx, s, pal, time) {
  fillRR(ctx, -34, -12, 68, 8, 3, shade(s.color, -0.2));
  ctx.fillStyle = withAlpha('#ffffff', 0.3);
  const off = (time * 20) % 8;
  for (let x = -34 + off; x < 34; x += 8) ctx.fillRect(x, -10, 3, 4);
  ctx.fillStyle = withAlpha(pal.card, 0.9);
  const bx = ((time * 16) % 60) - 30;
  fillRR(ctx, bx, -18, 8, 6, 1.5, pal.card);

  const labels = ['解析', 'digest', 'RAG', '比对'];
  const lights = s.subLights || [0, 0, 0, 0];
  const startX = -30, gap = 20;
  for (let i = 0; i < 4; i++) {
    const lx = startX + i * gap, ly = -32;
    let c = pal.weak;
    if (lights[i] === 1) c = pal.good;
    else if (lights[i] === 2) c = pal.warning;
    const glow = lights[i] === 2 ? 0.5 + Math.abs(Math.sin(time * 4)) * 0.5 : 1;
    ctx.globalAlpha = glow;
    ctx.fillStyle = c;
    ctx.beginPath(); ctx.arc(lx, ly, 3.2, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
    ctx.fillStyle = pal.mut; ctx.font = '6.5px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(labels[i], lx, ly + 10);
    if (i === 1 && s.digestProgress != null && lights[i] === 2) {
      fillRR(ctx, lx - 8, ly - 8, 16, 3, 1.5, pal.inset);
      fillRR(ctx, lx - 8, ly - 8, 16 * clamp(s.digestProgress, 0, 1), 3, 1.5, pal.seq3);
    }
  }
}

/** 财务分析室：白板 + 计算器 + 两块牌子（证据草稿 / 正式分析）。 */
function drawPropFinancial(ctx, s, pal, time) {
  fillRR(ctx, -30, -36, 40, 28, 2, pal.card);
  strokeRR(ctx, -30, -36, 40, 28, 2, shade(s.color, 0), 1.4);
  ctx.strokeStyle = s.color; ctx.lineWidth = 1.3;
  ctx.beginPath(); ctx.moveTo(-26, -14); ctx.lineTo(-18, -22); ctx.lineTo(-10, -18); ctx.lineTo(-2, -28); ctx.stroke();
  ctx.fillStyle = withAlpha(s.color, 0.5);
  for (let i = 0; i < 3; i++) ctx.fillRect(-26 + i * 6, -14 - i * 2, 3, 4 + i * 2);
  fillRR(ctx, 14, -20, 14, 18, 2, shade(s.color, -0.1));
  ctx.fillStyle = pal.good; ctx.fillRect(16, -18, 10, 4);
  ctx.fillStyle = withAlpha('#ffffff', 0.6);
  for (let r = 0; r < 3; r++) for (let c = 0; c < 3; c++) ctx.fillRect(16 + c * 3.3, -11 + r * 3.3, 2, 2);
  drawMiniSign(ctx, -20, -40, '证据草稿', pal, shade(s.color, 0.2));
  drawMiniSign(ctx, 8, -40, '正式分析', pal, s.color);
}

/** 绘制财务室上方的小牌子。 */
function drawMiniSign(ctx, x, y, text, pal, color) {
  ctx.font = '6.5px system-ui';
  const w = ctx.measureText(text).width + 8;
  fillRR(ctx, x - w / 2, y - 6, w, 10, 2, pal.card);
  strokeRR(ctx, x - w / 2, y - 6, w, 10, 2, color, 1);
  ctx.fillStyle = pal.fg; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(text, x, y - 0.5);
}

/** 市场雷达站：旋转雷达碟。 */
function drawPropMarket(ctx, s, pal, time) {
  ctx.strokeStyle = shade(s.color, -0.1); ctx.lineWidth = 2.4;
  ctx.beginPath(); ctx.moveTo(0, -4); ctx.lineTo(0, -20); ctx.stroke();
  ctx.save();
  ctx.translate(0, -22);
  ctx.strokeStyle = s.color; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.arc(0, 0, 12, Math.PI * 0.15, Math.PI * 0.85); ctx.stroke();
  ctx.beginPath(); ctx.arc(0, 0, 7, Math.PI * 0.15, Math.PI * 0.85); ctx.stroke();
  const ang = (time * 1.6) % (Math.PI * 2);
  ctx.save(); ctx.rotate(-Math.PI / 2);
  const grad = ctx.createLinearGradient(0, 0, Math.cos(ang) * 14, Math.sin(ang) * 14);
  grad.addColorStop(0, withAlpha(s.color, 0.8));
  grad.addColorStop(1, withAlpha(s.color, 0));
  ctx.strokeStyle = grad; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(Math.cos(ang) * 14, Math.sin(ang) * 14); ctx.stroke();
  ctx.restore();
  ctx.fillStyle = pal.critical;
  ctx.beginPath(); ctx.arc(Math.cos(time * 2) * 8, -Math.abs(Math.sin(time * 2)) * 6, 1.6, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}

/** 估值室：天平道具。 */
function drawPropValuation(ctx, s, pal, time) {
  ctx.strokeStyle = shade(s.color, -0.1); ctx.lineWidth = 2.6;
  ctx.beginPath(); ctx.moveTo(0, -4); ctx.lineTo(0, -30); ctx.stroke();
  const tilt = Math.sin(time * 1.5) * 0.12;
  ctx.save(); ctx.translate(0, -30); ctx.rotate(tilt);
  ctx.strokeStyle = s.color; ctx.lineWidth = 2.2;
  ctx.beginPath(); ctx.moveTo(-16, 0); ctx.lineTo(16, 0); ctx.stroke();
  for (const sx of [-16, 16]) {
    ctx.strokeStyle = withAlpha(s.color, 0.7); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx - 5, 8); ctx.moveTo(sx, 0); ctx.lineTo(sx + 5, 8); ctx.stroke();
    ctx.fillStyle = withAlpha(s.color, 0.85);
    ctx.beginPath(); ctx.ellipse(sx, 9, 6, 2.2, 0, 0, Math.PI * 2); ctx.fill();
  }
  ctx.restore();
  ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(0, -30, 2.4, 0, Math.PI * 2); ctx.fill();
}

/** 交付台：文件托盘 + 小旗。 */
function drawPropDeliver(ctx, s, pal, time) {
  fillRR(ctx, -22, -14, 44, 12, 3, shade(s.color, -0.1));
  fillRR(ctx, -18, -22, 36, 10, 2, pal.card);
  ctx.fillStyle = withAlpha(s.color, 0.4);
  for (let i = 0; i < 3; i++) ctx.fillRect(-14 + i * 11, -20, 8, 6);
  ctx.strokeStyle = pal.weak; ctx.lineWidth = 1.4;
  ctx.beginPath(); ctx.moveTo(0, -22); ctx.lineTo(0, -34); ctx.stroke();
  const wave = Math.sin(time * 3) * 1.6;
  ctx.fillStyle = s.color;
  ctx.beginPath(); ctx.moveTo(0, -34); ctx.lineTo(10, -32 + wave); ctx.lineTo(0, -29); ctx.closePath(); ctx.fill();
}

/** 行业地图桌：铺开的地图 + 图钉。 */
function drawPropIndustryMap(ctx, s, pal, time) {
  fillRR(ctx, -30, -24, 60, 20, 2, shade(pal.card, -0.02));
  strokeRR(ctx, -30, -24, 60, 20, 2, shade(s.color, 0), 1.2);
  ctx.strokeStyle = withAlpha(s.color, 0.5); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(-24, -8); ctx.lineTo(-10, -18); ctx.lineTo(6, -10); ctx.lineTo(22, -18); ctx.stroke();
  const pins = [[-10, -18], [6, -10], [22, -18]];
  pins.forEach((p, i) => {
    ctx.fillStyle = i === 0 ? pal.critical : s.color;
    ctx.beginPath(); ctx.arc(p[0], p[1], 2.2, 0, Math.PI * 2); ctx.fill();
  });
}

/** 验证闸门：发光 Gate。 */
function drawPropGate(ctx, s, pal, time) {
  const glow = 0.5 + Math.abs(Math.sin(time * 2)) * 0.5;
  fillRR(ctx, -22, -36, 8, 34, 2, shade(s.color, -0.1));
  fillRR(ctx, 14, -36, 8, 34, 2, shade(s.color, -0.1));
  fillRR(ctx, -24, -40, 48, 8, 2, s.color);
  ctx.globalAlpha = glow * 0.5;
  const grad = ctx.createLinearGradient(0, -36, 0, -2);
  grad.addColorStop(0, withAlpha(s.color, 0.7));
  grad.addColorStop(1, withAlpha(s.color, 0.05));
  ctx.fillStyle = grad; ctx.fillRect(-14, -36, 28, 34);
  ctx.globalAlpha = 1;
  ctx.strokeStyle = pal.card; ctx.lineWidth = 2; ctx.lineCap = 'round';
  ctx.beginPath(); ctx.moveTo(-6, -20); ctx.lineTo(-1, -14); ctx.lineTo(8, -26); ctx.stroke();
}

/** 行业研究室：书堆 + 望远镜。 */
function drawPropIndustryResearch(ctx, s, pal, time) {
  const books = [shade(s.color, 0.1), shade(s.color, -0.1), shade(s.color, 0.25)];
  books.forEach((c, i) => {
    fillRR(ctx, -28 + (i % 2) * 3, -12 - i * 7, 22, 6, 1.5, c);
  });
  ctx.strokeStyle = shade(s.color, -0.15); ctx.lineWidth = 1.6;
  ctx.beginPath(); ctx.moveTo(16, -4); ctx.lineTo(12, -18); ctx.moveTo(16, -4); ctx.lineTo(22, -16); ctx.stroke();
  ctx.save(); ctx.translate(16, -20); ctx.rotate(-0.5 + Math.sin(time * 1.2) * 0.08);
  fillRR(ctx, -3, -8, 6, 16, 2, s.color);
  ctx.fillStyle = pal.seq2; ctx.beginPath(); ctx.arc(0, -8, 3, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}

/* ============================================================
 * 五、飞行 token 与粒子
 * ============================================================ */

/**
 * 绘制交接文件 token：约 20px 高的白色折角文档卡。
 * 放大 + 阴影 + 折角让“文件在飞”在远景下也清晰可辨。
 */
export function drawFileToken(ctx, x, y, scale, rot, pal) {
  ctx.save();
  ctx.translate(x, y); ctx.rotate(rot || 0); ctx.scale(scale, scale);
  // 投影
  ctx.fillStyle = withAlpha('#000000', 0.16);
  fillRR(ctx, -9, -11, 21, 27, 3, withAlpha('#000000', 0.16));
  // 卡片本体
  fillRR(ctx, -10.5, -13, 21, 27, 3, pal.card);
  strokeRR(ctx, -10.5, -13, 21, 27, 3, pal.border, 1.2);
  // 折角
  ctx.fillStyle = pal.inset;
  ctx.beginPath(); ctx.moveTo(3.5, -13); ctx.lineTo(10.5, -13); ctx.lineTo(10.5, -6); ctx.closePath(); ctx.fill();
  ctx.strokeStyle = pal.border; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(3.5, -13); ctx.lineTo(10.5, -6); ctx.stroke();
  // 文本线
  ctx.strokeStyle = pal.weak; ctx.lineWidth = 1.2;
  for (let i = 0; i < 5; i++) {
    ctx.beginPath(); ctx.moveTo(-7, -5 + i * 4); ctx.lineTo(6, -5 + i * 4); ctx.stroke();
  }
  ctx.restore();
}

/** 绘制回流缺口 token：红色六边形，中间感叹号。 */
export function drawGapToken(ctx, x, y, scale, pal) {
  ctx.save();
  ctx.translate(x, y); ctx.scale(scale, scale);
  ctx.fillStyle = pal.critical;
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const an = Math.PI / 6 + i * Math.PI / 3;
    const px = Math.cos(an) * 13, py = Math.sin(an) * 13;
    i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
  }
  ctx.closePath(); ctx.fill();
  ctx.strokeStyle = withAlpha('#ffffff', 0.85); ctx.lineWidth = 1.5; ctx.stroke();
  ctx.fillStyle = '#ffffff'; ctx.font = 'bold 15px system-ui'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText('!', 0, 0.5);
  ctx.restore();
}

/**
 * 绘制彩带粒子。
 * 粒子由 game.js 维护（位置/旋转/颜色/生命），此处只负责一帧渲染。
 */
export function drawConfettiPiece(ctx, p) {
  ctx.save();
  ctx.globalAlpha = clamp(p.life, 0, 1);
  ctx.translate(p.x, p.y); ctx.rotate(p.rot);
  ctx.fillStyle = p.color;
  ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
  ctx.restore();
}

/** 绘制半旗（partial 收场）：旗降到杆中部，比例随舞台放大。 */
export function drawHalfMast(ctx, x, y, pal, color, time) {
  ctx.strokeStyle = pal.weak; ctx.lineWidth = 3;
  ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x, y - 70); ctx.stroke();
  const wave = Math.sin(time * 2) * 3;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(x, y - 38); ctx.quadraticCurveTo(x + 18, y - 35 + wave, x + 27, y - 32);
  ctx.lineTo(x + 27, y - 17 + wave); ctx.quadraticCurveTo(x + 18, y - 20, x, y - 23);
  ctx.closePath(); ctx.fill();
}
