/* SPDX-License-Identifier: MIT
   Copyright (c) 2026 EasonShu
   =========================================================================
   DayPilot · 前端逻辑（无构建步骤，浏览器直接加载本文件）

   与后端 outputs/server.py 的契约（实现见其中的 DashboardHandler）：
     /api/status/<切片>     读接口，按页面按需拉取：overview / accounts /
                            schedules / notifications / recent / tasks /
                            stats / admin
     /api/auth/*            会话、登录、注册、退出
     /api/accounts/*        凭据导入、停用、归档、恢复、删除
     /api/admin/users/*     管理员用户管理
     /api/crypto/key        取传输密钥，请求体加解密与签名见下方 crypto 一节
     /api/logs、/api/log    运行日志列表与原文

   注意：接口清单不在本文件重复维护，改动请以 server.py 为准。
   ====================================================================== */

const state = {
  data: null,
  timer: null,
  authenticated: false,
  username: "",
  role: "",
  status: "",
  activeAccount: null,
  panel: "",
  lastSignature: "",
  groupsSig: "",      // 账号结构指纹：只有它变了才重播入场动画
  todayKeys: {},      // 上一轮各账号的今日状态：用来判断「刚变化」并闪一下
  closeTimer: null,   // 抽屉延迟收起，等退场动画走完
  modalTimer: null,   // 导入弹窗同理
  signature: "",      // 数据内容指纹：用来判断服务端是不是真的有新结果（不含 generated_at）
  inflight: null,     // 正在飞的 /api/status 请求，撞车时直接复用
  watchTimer: null,   // 触发签到后的「盯变化」轮询句柄
  watching: false,    // 是否正在盯变化（顶栏状态点据此呼吸）
  panelDirty: false,  // 抽屉里的表单被用户动过 —— 静默刷新就别重建它，免得把填了一半的内容冲掉
};

/* 页面数据的自动同步节奏。
   以前是 5 分钟一次，而且触发签到后只补拉两次固定延迟 ——
   单账号跑完要十几秒，晚了就只能整页刷新才看得到新结果。
   现在：前台 20 秒一轮；后台标签页不轮询；回到前台立刻补一轮；
   触发签到后改成「盯着数据变化轮询」，一变就停。 */
const LIVE_INTERVAL_MS = 20 * 1000;
const WATCH_TIMEOUT_MS = 90 * 1000;
const WATCH_INTERVAL_MS = 2500;

/* 必须等一帧再加动画 class：同一帧里「显示 + 加 class」，浏览器
   看不到初始态，transition 直接不触发，表现出来就是「没有动画」。 */
const nextFrame = (fn) => window.requestAnimationFrame(() => window.requestAnimationFrame(fn));

const $ = (selector) => document.querySelector(selector);

const PRODUCTS = [
  { key: "WorkBuddy", short: "W", desc: "桌面客户端签到" },
  { key: "TRAE", short: "T", desc: "IDE 每日签到" },
];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function toastTone(message, explicitTone = "auto") {
  if (explicitTone && explicitTone !== "auto") return explicitTone;
  const text = String(message || "");
  if (/失败|错误|超时|拒绝|异常|无权限|删除失败|导入失败|保存失败|测试失败/.test(text)) return "danger";
  if (/警告|注意|尚未|没有需要|未发现|需审核|等待|需要管理员|请先登录/.test(text)) return "warning";
  if (/正在|读取|刷新|启动|提交中|保存中|上传|替换|发送/.test(text)) return "loading";
  if (/成功|已登录|已刷新|已导入|已保存|已发送|已复制|已启用|已停用|已归档|已删除|已恢复|已替换|已启动|已更新|已提交/.test(text)) return "success";
  return "info";
}

function toastTitle(tone) {
  return {
    success: "操作成功",
    danger: "操作失败",
    warning: "需要注意",
    loading: "处理中",
    info: "提示",
  }[tone] || "提示";
}

function toastIcon(tone) {
  if (tone === "success") return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m20 6-11 11-5-5"/></svg>';
  if (tone === "danger") return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8v5"/><path d="M12 17h.01"/><path d="M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>';
  if (tone === "warning") return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 4.3 2.6 18a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0z"/></svg>';
  if (tone === "loading") return '<span class="toast-spinner" aria-hidden="true"></span>';
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16v-4"/><path d="M12 8h.01"/><circle cx="12" cy="12" r="10"/></svg>';
}

function toastRoot() {
  let root = $("#toast");
  if (!root) {
    root = document.createElement("div");
    root.id = "toast";
    document.body.appendChild(root);
  }
  root.className = "toast-viewport";
  root.setAttribute("aria-live", "polite");
  root.setAttribute("aria-atomic", "false");
  return root;
}

function closeToast(item) {
  if (!item || item.classList.contains("is-leaving")) return;
  window.clearTimeout(item.toastTimer);
  item.classList.add("is-leaving");
  window.setTimeout(() => item.remove(), 180);
}

function showToast(message, tone = "auto", options = {}) {
  if (tone && typeof tone === "object") {
    options = tone;
    tone = options.tone || "auto";
  }
  const root = toastRoot();
  const finalTone = toastTone(message, tone);
  const item = document.createElement("div");
  item.className = `toast-item toast-${finalTone}`;
  item.setAttribute("role", finalTone === "danger" ? "alert" : "status");
  item.innerHTML = `
    <span class="toast-icon">${toastIcon(finalTone)}</span>
    <span class="toast-content">
      <strong>${escapeHtml(options.title || toastTitle(finalTone))}</strong>
      <span>${escapeHtml(message)}</span>
    </span>
    <button type="button" class="toast-close" aria-label="关闭通知" title="关闭通知">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
    </button>
    <span class="toast-timer" aria-hidden="true"></span>
  `;
  item.querySelector(".toast-close")?.addEventListener("click", () => closeToast(item));
  root.prepend(item);
  [...root.querySelectorAll(".toast-item")].slice(4).forEach((node) => closeToast(node));
  const duration = Number(options.duration || (finalTone === "danger" ? 5600 : finalTone === "loading" ? 3000 : 3800));
  item.style.setProperty("--dur", `${duration}ms`);
  item.toastDeadline = Date.now() + duration;
  // 悬停暂停自动消失，移开继续倒计时（视觉进度条与 JS 计时同步暂停/恢复）
  const pauseToast = () => {
    window.clearTimeout(item.toastTimer);
    item.toastRemaining = Math.max(0, item.toastDeadline - Date.now());
    item.classList.add("is-paused");
  };
  const resumeToast = () => {
    const remaining = Number.isFinite(item.toastRemaining) ? item.toastRemaining : duration;
    item.classList.remove("is-paused");
    if (item.classList.contains("is-leaving")) return;
    item.toastDeadline = Date.now() + remaining;
    item.toastRemaining = null;
    item.toastTimer = window.setTimeout(() => closeToast(item), remaining);
  };
  item.addEventListener("mouseenter", pauseToast);
  item.addEventListener("mouseleave", resumeToast);
  item.toastTimer = window.setTimeout(() => closeToast(item), duration);
}

window.DayPilotToast = showToast;
window.DailyHubToast = showToast;

/* 剪贴板：localhost 算安全上下文，但面板也可能从局域网 IP 打开（http 非安全），
   那里 navigator.clipboard 是 undefined —— 必须留 execCommand 兜底。 */
function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const helper = document.createElement("textarea");
    helper.value = text;
    helper.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.appendChild(helper);
    helper.select();
    try {
      document.execCommand("copy") ? resolve() : reject(new Error("execCommand 返回 false"));
    } catch (error) {
      reject(error);
    } finally {
      helper.remove();
    }
  });
}

function setLoginMessage(message, type = "") {
  const target = $("#loginMessage");
  if (!target) return;
  target.textContent = message || "";
  target.className = "login-message" + (type === "danger" ? " is-danger"
    : type === "success" ? " is-success"
    : type === "warning" ? " is-warning"
    : type === "muted" ? " is-muted"
    : "");
  target.hidden = !target.textContent;
}

function resetPasswordToggles(scope = document) {
  scope.querySelectorAll("[data-toggle-password]").forEach((button) => {
    const control = button.closest(".auth-control");
    const input = control?.querySelector("input");
    if (input) input.type = "password";
    button.classList.remove("is-active");
    button.setAttribute("aria-label", "显示密码");
    button.setAttribute("title", "显示密码");
  });
}

function togglePasswordVisibility(button) {
  const control = button.closest(".auth-control");
  const input = control?.querySelector("input");
  if (!input) return;
  const visible = input.type === "password";
  input.type = visible ? "text" : "password";
  button.classList.toggle("is-active", visible);
  button.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
  button.setAttribute("title", visible ? "隐藏密码" : "显示密码");
  input.focus();
}

/* ---- 登录态 ---------------------------------------------------------- */

/* 底栏已移除：正常态不再显示「xx 已登录」这类噪音。
   连不上服务时才提示，直接写到顶栏副标题（状态点会同步变灰）。 */
function showServiceError(text) {
  const meta = $("#headerMeta");
  if (meta) meta.textContent = text;
}

function setLocked(locked, message) {
  document.body.classList.toggle("locked", locked);
  closeDrawer();
  closeUpload();
  if (!locked) return;
  state.authenticated = false;
  state.username = "";
  state.role = "";
  state.status = "";
  state.data = null;
  const panel = $("#console");
  if (panel) panel.hidden = true;
  setLiveDot("offline");
  stopLiveMonitor();
  const card = document.querySelector("[data-login-card]");
  if (card) card.classList.remove("is-success", "is-leaving");
  const loginForm = $("#loginForm");
  if (loginForm) {
    loginForm.hidden = false;
    loginForm.reset();
    setLoginButtonState(loginForm.querySelector("button[type='submit']"), "idle");
  }
  const registerForm = $("#registerForm");
  if (registerForm) {
    registerForm.hidden = true;
    registerForm.reset();
  }
  resetPasswordToggles(document);
  const showRegister = $("#showRegister");
  if (showRegister) showRegister.hidden = false;
  setLoginMessage(message);
  window.setTimeout(() => $("#loginForm input[name='username']")?.focus(), 80);
}

function unlock(username, role = "", status = "approved") {
  state.authenticated = true;
  state.username = username || "";
  state.role = role || "";
  state.status = status || "";
  document.body.classList.remove("locked");
  // #console 是靠 hidden 属性控制显隐的，只摘掉 body.locked 不够 —— 漏掉这步整页会是空白。
  const panel = $("#console");
  if (panel) panel.hidden = false;
  startLiveMonitor();
}

/* ---- 实时同步 -------------------------------------------------------- */

/* 数据指纹：覆盖界面上真正会显示出来的每一个字段。
   刻意**不含** generated_at / 请求时间这类每次都在变的东西 ——
   带上就等于「永远在变」，下面那套「内容没变就不重画」的优化会彻底失效。

   为什么要这么全：指纹一旦漏掉某个字段，那个字段的更新就永远不会反映到页面上；
   多算一点（拼字符串）远比漏算安全。 */
function dataSignature(data) {
  if (!data) return "";
  const s = data.summary || {};
  const join = (rows, pick) => (rows || []).map(pick).join(",");
  return [
    "s", s.total_accounts, s.signed_today, s.attention, s.pending, s.total_credit_today, s.longest_streak,
    "a", join(data.accounts, (a) => [
      a.product, a.name, a.display_name, a.account_id, a.account_name,
      a.enabled, a.credential_exists, a.health_severity, a.health_label,
      a.days_left, a.refresh_days_left,
      a.latest ? [a.latest.result, a.latest.time, a.latest.label, a.latest.credit, a.latest.streak_days].join(":") : "-",
    ].join(":")),
    "r", join((data.recent || []).slice(0, 12), (r) => [r.product, r.account, r.time, r.raw_result].join(":")),
    "z", join(data.archived, (a) => [a.product, a.name].join(":")),
    "u", join(data.admin?.users, (u) => [u.username, u.status, u.role].join(":")),
    "ua", join(data.admin?.accounts, (a) => [a.owner_username, a.product, a.name, a.enabled, a.health_severity, a.latest?.time].join(":")),
    "un", join(data.admin?.notifications, (n) => [n.owner_username, n.product, n.enabled, n.channel, n.on, n.has_key, n.has_url].join(":")),
    "p", join(data.schedules, (x) => [x.product, x.enabled, x.poll_enabled, x.daily_time, (x.poll_times || []).join("+")].join(":")),
    "n", join(data.notifications, (x) => [x.product, x.enabled, x.channel, x.on].join(":")),
    "t", data.scheduler_active, data.next_run_at,
  ].join("|");
}


function setSyncing(on) {
  state.watching = !!on;
  const dot = $("#liveDot");
  if (!dot) return;
  if (on) dot.classList.add("is-syncing");
  else dot.classList.remove("is-syncing");
}

function stopWatching() {
  window.clearTimeout(state.watchTimer);
  state.watchTimer = null;
  setSyncing(false);
}

/* 静默拉一次数据；数据真的变了才顺手把「当前这一屏」也更新掉。
   抽屉开着的场景最要紧：新结果到了抽屉里还停在旧值，就等于没刷新。
   没变的时候连抽屉都不碰 —— 少一次无谓的 DOM 重建。 */
async function syncData() {
  const changed = await loadStatus({ silent: true });
  if (changed) refreshActivePanel();
  return changed;
}

function startLiveMonitor() {
  window.clearInterval(state.timer);
  state.timer = window.setInterval(() => {
    // 后台标签页不轮询：白耗服务端，回来时补一轮就行。
    if (document.visibilityState === "hidden") return;
    syncData().catch(() => {});
  }, LIVE_INTERVAL_MS);
}

function stopLiveMonitor() {
  window.clearInterval(state.timer);
  state.timer = null;
  stopWatching();
}

/* 触发签到之后用的轮询：不是「等固定几秒」，而是「盯着指纹变没变」。
   变了立刻收工并停表；最多盯 90 秒。这样跑得快的不用干等，跑得慢的也不会漏。 */
function watchForUpdate() {
  const before = state.signature;
  const deadline = Date.now() + WATCH_TIMEOUT_MS;
  stopWatching();
  setSyncing(true);
  const tick = async () => {
    try {
      await syncData();
    } catch (error) {
      // 单次失败不中止，下一轮继续
    }
    if (state.signature !== before || Date.now() > deadline) {
      stopWatching();
      return;
    }
    state.watchTimer = window.setTimeout(tick, WATCH_INTERVAL_MS);
  };
  state.watchTimer = window.setTimeout(tick, 1200);
}

// 回到前台 / 重新获得焦点时立刻补一轮：用户切回来看到的就是最新的。
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.authenticated) syncData().catch(() => {});
});
window.addEventListener("focus", () => {
  if (state.authenticated) syncData().catch(() => {});
});

/* ---- 网络 ------------------------------------------------------------ */

/* ---- 传输层加解密（纯 WebCrypto，无第三方依赖） -------------------------
   服务端不支持(旧版)或引导失败时自动退回明文，保证兼容。 */
let cryptoCtx = {}; // { secret: Uint8Array|null, ready:bool }

async function ensureCryptoKey() {
  if ("secret" in cryptoCtx) return cryptoCtx;
  // 引导密钥：任何上下文都拉取（http 下也由纯 JS 镜像算法完成加解密，故无需检查 crypto.subtle）。
  try {
    const res = await fetch("/api/crypto/key", { cache: "no-store", credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    const raw = b64ToBuf(data.key || "");
    cryptoCtx = { secret: raw.length ? raw : null, ready: !!raw.length };
  } catch (error) {
    cryptoCtx = { secret: null, ready: false };
  }
  return cryptoCtx;
}

function b64ToBuf(b64) {
  const bin = atob(b64.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}
function concatBytes(...arrs) {
  const len = arrs.reduce((a, x) => a + x.length, 0);
  const out = new Uint8Array(len);
  let o = 0;
  for (const a of arrs) { out.set(a, o); o += a.length; }
  return out;
}
function consttimeEq(a, b) {
  const A = new Uint8Array(a), B = new Uint8Array(b);
  if (A.length !== B.length) return false;
  let diff = 0;
  for (let i = 0; i < A.length; i++) diff |= A[i] ^ B[i];
  return diff === 0;
}
// 是否具备 WebCrypto（HTTPS/localhost 才存在）。http://IP 访问时无 crypto.subtle，
// 改用下方纯 JS 镜像算法（与服务端 hashlib 逐字节一致），保证 IP 访问也能加解密。
const HAVE_SUBTLE = !!(globalThis.crypto && globalThis.crypto.subtle);
// SHA-256 常量 K 与初始 H
const JS_SHA_K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2,
]);
const JS_SHA_H = new Uint32Array([
  0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19,
]);
function jsRor(x, n) {
  return ((x >>> n) | (x << (32 - n))) >>> 0;
}
function jsSha256(msg) {
  const pad = (64 - ((msg.length + 9) % 64)) % 64;
  const m = new Uint8Array(msg.length + 1 + 8 + pad);
  m.set(msg);
  m[msg.length] = 0x80;
  const dv = new DataView(m.buffer);
  // 64 位比特长度；本项目消息远小于 2^32 字节，故高位恒为 0，仅写低 32 位
  dv.setUint32(m.length - 8, 0);
  dv.setUint32(m.length - 4, msg.length * 8 >>> 0);
  const h = new Uint32Array(JS_SHA_H);
  const w = new Uint32Array(64);
  for (let off = 0; off < m.length; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = (jsRor(w[i - 15], 7) ^ jsRor(w[i - 15], 18) ^ (w[i - 15] >>> 3)) >>> 0;
      const s1 = (jsRor(w[i - 2], 17) ^ jsRor(w[i - 2], 19) ^ (w[i - 2] >>> 10)) >>> 0;
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (let i = 0; i < 64; i++) {
      const S1 = (jsRor(e, 6) ^ jsRor(e, 11) ^ jsRor(e, 25)) >>> 0;
      const ch = ((e & f) ^ (~e & g)) >>> 0;
      const t1 = (hh + S1 + ch + JS_SHA_K[i] + w[i]) >>> 0;
      const S0 = (jsRor(a, 2) ^ jsRor(a, 13) ^ jsRor(a, 22)) >>> 0;
      const ma = ((a & b) ^ (a & c) ^ (b & c)) >>> 0;
      const t2 = (S0 + ma) >>> 0;
      hh = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0; h[2] = (h[2] + c) >>> 0;
    h[3] = (h[3] + d) >>> 0; h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0;
    h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
  }
  const out = new Uint8Array(32);
  const odv = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) odv.setUint32(i * 4, h[i]);
  return out;
}
function jsHmac256(key, msg) {
  let k = new Uint8Array(64).fill(0);
  if (key.length > 64) key = jsSha256(key);
  k.set(key);
  const ipad = new Uint8Array(64), opad = new Uint8Array(64);
  for (let i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5c; }
  return jsSha256(concatBytes(opad, jsSha256(concatBytes(ipad, msg))));
}
// PBKDF2-HMAC-SHA256、dklen=32、iterations=1（与服务端一致）：即 HMAC(key=password, salt || INT32_BE(1))
function jsPbkdf2One(password, salt) {
  return jsHmac256(password, concatBytes(salt, new Uint8Array([0, 0, 0, 1])));
}
// 与服务端 _msg_key 一致：pbkdf2_hmac(sha256, label+"\x00"+secret, nonce, 1, 32)
// 有 WebCrypto 用原生，否则用纯 JS 镜像算法。
async function deriveKey(label, nonce) {
  const pwd = new Uint8Array(1 + 1 + cryptoCtx.secret.length);
  pwd[0] = label.charCodeAt(0); pwd[1] = 0; pwd.set(cryptoCtx.secret, 2);
  if (HAVE_SUBTLE) {
    const base = await crypto.subtle.importKey("raw", pwd, { name: "PBKDF2" }, false, ["deriveBits"]);
    const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: nonce, iterations: 1 }, base, 256);
    return new Uint8Array(bits);
  }
  return jsPbkdf2One(pwd, nonce);
}
async function hmacBytes(keyBuf, data) {
  if (HAVE_SUBTLE) {
    const k = await crypto.subtle.importKey("raw", keyBuf, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    return new Uint8Array(await crypto.subtle.sign("HMAC", k, data));
  }
  return jsHmac256(keyBuf, data);
}
// 与服务端 _keystream 一致：HMAC-SHA256(key, counter_be32) 拼接，截取所需长度
async function keystream(keyBuf, length) {
  const enc = new TextEncoder(), view = new DataView(new ArrayBuffer(4));
  let counter = 0, acc = new Uint8Array(0);
  while (acc.length < length) {
    view.setUint32(0, counter, false);
    acc = concatBytes(acc, await hmacBytes(keyBuf, new Uint8Array(view.buffer)));
    counter += 1;
  }
  return acc.subarray(0, length);
}
async function aesEncryptString(text, aad) {
  if (!cryptoCtx.ready) return null;
  const enc = new TextEncoder();
  const nonce = crypto.getRandomValues(new Uint8Array(16));
  const raw = enc.encode(text);
  const ks = await keystream(await deriveKey("k", nonce), raw.length);
  const ct = raw.map((b, i) => b ^ ks[i]);
  const tag = await hmacBytes(await deriveKey("a", nonce), concatBytes(nonce, ct, enc.encode(aad)));
  return JSON.stringify({ v: 2, n: bufToB64(nonce), c: bufToB64(ct), t: bufToB64(tag) });
}
async function aesDecryptEnvelope(text, aad) {
  if (!cryptoCtx.ready) return null;
  const enc = new TextEncoder();
  let env;
  try { env = JSON.parse(text); } catch (error) { return null; }
  if (!(env && env.v === 2 && env.n && env.c && env.t)) return null;
  const nonce = b64ToBuf(env.n), ct = b64ToBuf(env.c), tag = b64ToBuf(env.t);
  const expect = await hmacBytes(await deriveKey("a", nonce), concatBytes(nonce, ct, enc.encode(aad)));
  if (!consttimeEq(expect, tag)) return null;
  const ks = await keystream(await deriveKey("k", nonce), ct.length);
  return new TextDecoder().decode(ct.map((b, i) => b ^ ks[i]));
}

// 统一网络层：GET/POST 都走这里，支持 signal/缓存参数，透明加解密。
async function decryptedFetch(url, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const path = url.split("?")[0];
  const aad = `${method} ${path}`;
  const ctx = await ensureCryptoKey();
  const headers = Object.assign({}, opts.headers || {});
  let body = opts.body;
  if (opts.json !== undefined) {
    headers["Content-Type"] = "application/json";
    if (ctx.ready) {
      headers["X-Crypto"] = "1";
      body = await aesEncryptString(JSON.stringify(opts.json), aad);
    } else {
      body = JSON.stringify(opts.json);
    }
  } else if (ctx.ready) {
    headers["X-Crypto"] = "1";
  }
  const fetchOpts = Object.assign({}, opts, { method, headers, credentials: "same-origin" });
  if (body !== undefined) fetchOpts.body = body;
  const response = await fetch(url, fetchOpts);
  const text = await response.text();
  let data;
  if (ctx.ready && text) {
    try {
      data = JSON.parse(await aesDecryptEnvelope(text, aad));
    } catch (error) {
      data = JSON.parse(text || "{}");
    }
  } else {
    data = JSON.parse(text || "{}");
  }
  return { ok: response.ok, status: response.status, text, data };
}

async function apiPost(url, payload) {
  const res = await decryptedFetch(url, {
    method: "POST",
    json: payload instanceof FormData ? undefined : (payload || {}),
    body: payload instanceof FormData ? payload : undefined,
  });
  if (!res.ok) {
    if (res.status === 401) setLocked(true, "登录已过期，请重新登录。");
    throw new Error(res.data.error || `HTTP ${res.status}`);
  }
  return res.data;
}

/* ---- 时间工具 -------------------------------------------------------- */

function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  const hh = String(date.getHours()).padStart(2, "0");
  const mi = String(date.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

function formatClock(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function formatStamp(value) {
  const date = value ? new Date(value) : new Date();
  if (Number.isNaN(date.getTime())) return "-";
  return `${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function statusClass(severity) {
  return ["success", "warning", "danger", "neutral"].includes(severity) ? severity : "neutral";
}

function capital(name) {
  const text = String(name || "?");
  return text.slice(0, 1).toUpperCase();
}

/* 状态 → 配色映射 */
const SEVERITY_TONE = {
  success: { pill: "bg-emerald-50 text-emerald-700 border-emerald-200", dot: "bg-emerald-500", cell: "bg-emerald-50", text: "text-emerald-700" },
  warning: { pill: "bg-amber-50 text-amber-700 border-amber-200", dot: "bg-amber-500", cell: "bg-amber-50", text: "text-amber-700" },
  danger:  { pill: "bg-rose-50 text-rose-700 border-rose-200", dot: "bg-rose-500", cell: "bg-rose-50", text: "text-rose-700" },
  neutral: { pill: "bg-slate-100 text-slate-600 border-slate-200", dot: "bg-slate-400", cell: "bg-slate-50", text: "text-slate-600" },
};
const tone = (sev) => SEVERITY_TONE[statusClass(sev)] || SEVERITY_TONE.neutral;

/* =========================================================================
   账号状态判定（卡片上最该看清的东西）
   ====================================================================== */

function todayState(item) {
  if (item.enabled === false) {
    return { key: "off", severity: "neutral", label: "已停用", short: "停用", note: "不参与自动签到" };
  }
  const latest = item.latest || {};
  if (latest.needs_attention || latest.severity === "danger") {
    return {
      key: "error", severity: "danger", label: "执行异常", short: "异常",
      note: latest.note || latest.label || "需要处理", time: latest.time,
    };
  }
  if (latest.signed_today) {
    return {
      key: "done", severity: "success", label: "今日已签", short: "已签",
      note: latest.credit ? `本次 +${latest.credit}` : (latest.note || "签到成功"),
      time: latest.time,
    };
  }
  if (latest.time) {
    return {
      key: "wait", severity: "warning", label: "今日未签", short: "待签",
      note: latest.note || "等待下次执行", time: latest.time,
    };
  }
  return { key: "none", severity: "neutral", label: "暂无记录", short: "无记录", note: "等待首次执行", time: null };
}

function credentialState(item) {
  if (item.credential_exists === false) {
    return { severity: "warning", short: "未发现", label: "未发现凭据", detail: "需要重新导出并上传" };
  }
  const days = item.days_left;
  if (days == null) {
    return { severity: "neutral", short: "—", label: "未记录有效期", detail: "凭据文件已就位" };
  }
  const rounded = Math.max(0, Math.round(days));
  let severity = "success";
  if (days < 0 || days <= 3) severity = "danger";
  else if (days <= 10) severity = "warning";
  const label = days < 0 ? "凭据已过期" : `凭据剩余 ${rounded} 天`;
  const detail = item.expires_at ? `过期时间 ${formatDateTime(item.expires_at)}` : "未读到过期时间";
  return { severity, short: days < 0 ? "已过期" : `${rounded} 天`, label, detail };
}

function accountHistory(item, limit = 12) {
  if (item.owner_username && item.owner_username !== state.username) {
    return (item.recent || []).slice(0, limit);
  }
  return (state.data?.recent || [])
    .filter((row) => row.account === item.name && row.product === item.product)
    .slice(0, limit);
}

/* spark：把最近 12 次结果画成一排色块。空槽用极淡底色而非灰块。 */
function renderSpark(item) {
  const rows = accountHistory(item, 12);
  const slot = 12;
  // --i 供 CSS 做「从左往右依次长出来」的错位延迟
  const blanks = (count, offset = 0) =>
    Array.from({ length: count }, (_, i) => `<i class="empty-slot" style="--i:${offset + i}"></i>`).join("");
  if (!rows.length) {
    return `
      <div class="spark-row">
        <div class="spark-head"><span>近 12 次</span><em>暂无数据</em></div>
        <div class="spark">${blanks(slot)}</div>
        <div class="spark-foot"><span>PAST</span><span>NOW</span></div>
      </div>
    `;
  }
  const chrono = [...rows].reverse();
  const pad = Math.max(0, slot - chrono.length);
  const cells = chrono
    .map((row, i) => `<i class="${statusClass(row.severity)}" style="--i:${pad + i}" title="${escapeAttr(`${formatDateTime(row.time)} · ${row.label}`)}"></i>`)
    .join("");
  const okCount = rows.filter((row) => row.severity === "success").length;
  return `
    <div class="spark-row">
      <div class="spark-head"><span>近 ${rows.length} 次</span><em>成功 ${okCount} / ${rows.length}</em></div>
      <div class="spark">${blanks(pad)}${cells}</div>
      <div class="spark-foot"><span>PAST</span><span>NOW</span></div>
    </div>
  `;
}

function pathFileName(value) {
  const text = String(value || "").replaceAll("\\", "/");
  return text.split("/").filter(Boolean).pop() || "";
}

function compactMiddle(value, limit = 16) {
  const text = String(value ?? "");
  if (text.length <= limit) return text;
  const head = Math.max(4, Math.ceil((limit - 1) * 0.56));
  const tail = Math.max(3, limit - head - 1);
  return `${text.slice(0, head)}…${text.slice(-tail)}`;
}

function daysShort(value, empty = "未记录") {
  if (value == null || value === "") return empty;
  const days = Math.round(Number(value));
  if (Number.isNaN(days)) return empty;
  return days < 0 ? "已过期" : `${Math.max(0, days)} 天`;
}

function accountRuntimeLabel(item) {
  if (item.credential_exists === false) return "仅日志";
  return item.enabled === false ? "已停用" : "已启用";
}

function credentialExpiryShort(item) {
  if (item.days_left == null) return "未记录";
  if (Number(item.days_left) < 0) return "已过期";
  return daysShort(item.days_left);
}

/* 账号 ID / accountName 都来自凭据文件本身：
     TRAE      account.userId（16 位）+ account.accountName
     WorkBuddy account.uid（36 位 UUID）+ JWT 里的 nickname
   老凭据里可能一个都没有 —— 那就老实写「未记录」，别编一个出来。 */
function accountIdentity(item) {
  return {
    id: String(item.account_id || ""),
    name: String(item.account_name || ""),
  };
}

function accountInfoGrid(item) {
  const credentialPath = item.credential_path || item.disabled_path || "";
  const identity = accountIdentity(item);
  const rows = [
    { label: "账号", value: item.name || "-", title: item.name || "", mono: true },
    {
      label: "accountName",
      value: identity.name || "未记录",
      title: identity.name ? `accountName：${identity.name}` : "凭据里没有 accountName",
      muted: !identity.name,
    },
    {
      label: "账号 ID",
      value: identity.id ? compactMiddle(identity.id, 18) : "未记录",
      title: identity.id ? `账号 ID：${identity.id}` : "凭据里没有账号 ID",
      mono: true,
      muted: !identity.id,
    },
    { label: "运行", value: accountRuntimeLabel(item) },
    { label: "凭据", value: pathFileName(credentialPath) || "未发现", title: credentialPath || "未发现凭据文件", mono: true },
    { label: "日志", value: pathFileName(item.log_path) || "暂无", title: item.log_path || "暂无日志文件", mono: true },
    { label: "到期", value: credentialExpiryShort(item), title: item.expires_at ? formatDateTime(item.expires_at) : "未记录有效期" },
    { label: "刷新", value: daysShort(item.refresh_days_left), title: item.refresh_expires_at ? formatDateTime(item.refresh_expires_at) : "未记录刷新有效期" },
  ];

  return `
    <dl class="account-info-grid mb-4">
      ${rows.map((row) => `
        <div title="${escapeAttr(row.title || row.value)}">
          <dt>${escapeHtml(row.label)}</dt>
          <dd class="${[row.mono ? "font-mono" : "", row.muted ? "is-muted" : ""].filter(Boolean).join(" ")}">${escapeHtml(row.value)}</dd>
        </div>
      `).join("")}
    </dl>
  `;
}

function accountInfoChips(item) {
  const latest = item.latest || {};
  const chips = [
    latest.result ? { text: latest.result, severity: latest.severity || "neutral", title: "最近原始结果" } : null,
    latest.credit != null ? { text: `+${latest.credit} 分`, severity: "success", title: "本次积分" } : null,
    latest.streak_days != null ? { text: `连续 ${latest.streak_days} 天`, severity: "neutral", title: "连续签到" } : null,
    latest.total_credits != null ? { text: `总分 ${latest.total_credits}`, severity: "neutral", title: "累计积分" } : null,
    item.ahaDeviceId ? { text: `设备 ${compactMiddle(item.ahaDeviceId, 10)}`, severity: "neutral", title: item.ahaDeviceId, mono: true } : null,
  ].filter(Boolean);

  if (!chips.length) return "";
  return `
    <div class="account-chip-row mb-4">
      ${chips.map((chip) => `
        <span class="${tone(chip.severity).pill} ${chip.mono ? "font-mono" : ""}" title="${escapeAttr(chip.title || chip.text)}">${escapeHtml(chip.text)}</span>
      `).join("")}
    </div>
  `;
}

/* ---- 卡片（新设计） -------------------------------------------------- */

function renderAccountCard(item, index = 0) {
  const today = todayState(item);
  const cred = credentialState(item);
  const disabled = item.enabled === false;
  const serial = String(index + 1).padStart(2, "0");
  const displayName = String(item.display_name || item.name || "未命名账号");
  const title = displayName || item.name;
  const subTitle = item.name && item.name !== displayName
    ? item.name
    : (item.credential_exists === false ? "仅日志记录" : "凭据已接入");
  const isTrae = item.product === "TRAE";
  const t = tone(today.severity);
  const c = tone(cred.severity);
  const brandBg = isTrae ? "bg-slate-100 text-slate-700 border-slate-200" : "bg-blue-50 text-blue-700 border-blue-100";

  return `
    <article style="--i:${Math.min(index, 7)}"
             class="account-card group flex flex-col h-full bg-white border border-slate-200/80 rounded-2xl cursor-pointer
                    hover:border-slate-300 hover:shadow-xl hover:shadow-slate-200/50 hover:-translate-y-0.5
                    focus:outline-none focus:ring-4 focus:ring-blue-500/10 focus:border-blue-300${disabled ? " opacity-60" : ""}"
             role="button" tabindex="0"
             data-account="${escapeAttr(item.name)}" data-product="${escapeAttr(item.product)}"
             aria-label="${escapeAttr(`${item.product} 第 ${index + 1} 个账号 ${title}，${today.label}，点击配置`)}">
      <div class="p-5 flex-1 flex flex-col">
        <!-- 头部：图标 + 标题 + 状态徽章 -->
        <header class="flex items-start justify-between gap-3 mb-4">
          <div class="flex items-center gap-2.5 min-w-0">
            ${productIconHtml(item.product, "md")}
            <div class="min-w-0">
              <div class="flex items-center gap-1.5 min-w-0">
                <span class="account-serial">#${escapeHtml(serial)}</span>
                <h3 class="font-semibold text-slate-900 truncate text-[15px] leading-tight">${escapeHtml(title)}</h3>
              </div>
              <p class="text-[11px] text-slate-500 mt-0.5 truncate">
                <span class="inline-block px-1 py-px ${brandBg} rounded text-[10px] font-semibold border mr-1.5 align-middle">${escapeHtml(item.product)}</span>
                ${escapeHtml(subTitle)}
              </p>
            </div>
          </div>
          <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-semibold border flex-none ${t.pill}">
            <span class="w-1.5 h-1.5 rounded-full ${t.dot}"></span>${escapeHtml(today.label)}
          </span>
        </header>

        <!-- 3 指标格 -->
        <div class="grid grid-cols-3 gap-1.5 mb-4">
          <div class="px-2.5 py-2 rounded-lg ${t.cell}">
            <div class="text-[10px] text-slate-500 font-medium">今日</div>
            <div class="text-[15px] font-semibold mt-0.5 ${t.text}">${escapeHtml(today.short)}</div>
          </div>
          <div class="px-2.5 py-2 rounded-lg ${c.cell}">
            <div class="text-[10px] text-slate-500 font-medium">凭据</div>
            <div class="text-[15px] font-semibold mt-0.5 ${c.text}">${escapeHtml(cred.short)}</div>
          </div>
          <div class="px-2.5 py-2 rounded-lg bg-slate-50">
            <div class="text-[10px] text-slate-500 font-medium">最近</div>
            <div class="text-[15px] font-semibold mt-0.5 text-slate-700">${escapeHtml(today.time ? formatClock(today.time) : "—")}</div>
          </div>
        </div>

        ${accountInfoGrid(item)}
        ${accountInfoChips(item)}
        ${renderSpark(item)}

        <!-- 监控状态：mt-auto 让同一行的卡片底部对齐，高度不齐也整齐 -->
        <div class="mt-auto pt-4 border-t border-slate-100">
          <p class="text-[12px] text-slate-500 leading-relaxed line-clamp-2 mb-2">${escapeHtml(today.note)}</p>
          <div class="flex items-center justify-between text-[11px]">
            <span class="text-slate-400">${escapeHtml(today.time ? formatDateTime(today.time) : "暂无记录")}</span>
            <span class="text-blue-600 font-medium inline-flex items-center gap-0.5 group-hover:gap-1.5 transition-all">
              配置详情
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
                <path d="M9 18l6-6-6-6"/>
              </svg>
            </span>
          </div>
        </div>
      </div>
    </article>
  `;
}

/* ---- 分组（新设计） -------------------------------------------------- */

function renderGroup(product) {
  const accounts = (state.data?.accounts || []).filter((item) => item.product === product.key);
  const stats = (state.data?.products || []).find((item) => item.product === product.key) || {};
  const isTrae = product.key === "TRAE";
  const signed = stats.signed_today || 0;
  const pending = stats.pending || 0;
  const attention = stats.attention || 0;
  const total = stats.total || accounts.length;
  const brandSolid = isTrae ? "bg-slate-800" : "bg-blue-600";

  return `
    <section data-product="${escapeAttr(product.key)}" class="space-y-4">
      <header style="--i:0" class="group-header rounded-2xl border border-slate-200/80 bg-white shadow-sm shadow-slate-900/[0.02]">
        <div class="flex items-center justify-between gap-4 px-5 py-3.5">
          <div class="flex items-center gap-3.5 min-w-0">
            ${productIconHtml(product.key)}
            <div class="min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <h2 class="text-[15px] font-semibold text-slate-900 tracking-tight">${escapeHtml(product.key)}</h2>
                <span class="px-1.5 py-px ${brandSolid} text-white rounded text-[11px] font-semibold leading-relaxed">${total}</span>
                <span class="text-[11px] text-slate-400 font-medium">账号</span>
              </div>
              <p class="text-xs text-slate-500 mt-0.5 truncate">${escapeHtml(product.desc)}</p>
            </div>
          </div>
          <div class="flex items-center gap-3 flex-none">
            <div class="hidden md:flex items-center gap-1 text-xs">
              <span class="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-emerald-50 text-emerald-700 border border-emerald-100 font-semibold">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>${signed}
              </span>
              ${pending ? `<span class="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-amber-50 text-amber-700 border border-amber-100 font-semibold">
                <span class="w-1.5 h-1.5 rounded-full bg-amber-500"></span>${pending}
              </span>` : ""}
              ${attention ? `<span class="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-rose-50 text-rose-700 border border-rose-100 font-semibold">
                <span class="w-1.5 h-1.5 rounded-full bg-rose-500"></span>${attention}
              </span>` : ""}
            </div>
            ${accounts.length ? `<button class="group-run h-8 px-3 ${brandSolid} hover:opacity-90 hover:shadow-md text-white rounded-lg text-xs font-medium transition flex items-center gap-1.5" type="button" data-run="${escapeAttr(product.key)}">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z"/></svg>
              立即签到
            </button>` : ""}
          </div>
        </div>
      </header>
      ${isTrae ? `
      <!-- TRAE 机器码绑定提醒：一机多号只会有第一个签到成功，避免用户困惑。 -->
      <div class="rounded-xl border border-amber-200/70 bg-amber-50/80 text-amber-800 px-3.5 py-2.5 text-xs leading-relaxed flex items-start gap-2">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-amber-500 flex-none mt-0.5">
          <path d="M6 10h0"/><path d="M10.5 10h0"/><path d="M15 14h0"/><path d="M18 6l3 3-2 1 1 3-3 3-2-2"/><path d="M11 21H6.5a2.5 2.5 0 0 1 0-5h.5a2.5 2.5 0 0 1 .5 0"/><path d="M13 7H8a2 2 0 0 1 0-4h1"/><path d="M10 12a2 2 0 0 0 0 4h8a2 2 0 0 1 0 4H15"/>
        </svg>
        <span><b class="font-semibold">TRAE 账号已绑定机器码</b>——同一台电脑（同一机器码）托管多个账号时，每天只有其中<em class="not-italic font-semibold">第一个</em>账号能签到成功，其余会轮空。如需多号，请把账号分布到不同机器上。</span>
      </div>` : `
      <!-- WorkBuddy 多账号说明：与 TRAE 不同，同一台电脑的多账号可分别签到。 -->
      <div class="rounded-xl border border-emerald-200/70 bg-emerald-50/80 text-emerald-800 px-3.5 py-2.5 text-xs leading-relaxed flex items-start gap-2">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-emerald-500 flex-none mt-0.5">
          <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
        </svg>
        <span><b class="font-semibold">WorkBuddy 支持多账号</b>——与 TRAE 不同，WorkBuddy<em class="not-italic font-semibold">不绑定机器码</em>，同一台电脑上托管的多个账号可各自签到成功，互不影响。</span>
      </div>`}
      <!-- 列数自适应：固定 3 列时「1 个账号 + 导入卡」会空出一整列，
           auto-fit 会把空轨道折掉，卡片自动铺满且宽度有上限。 -->
      <div class="grid grid-cols-[repeat(auto-fit,minmax(300px,1fr))] gap-4">
        ${accounts.map((item, index) => renderAccountCard(item, index)).join("")}
        <button type="button" data-add="${escapeAttr(product.key)}" style="--i:${Math.min(accounts.length, 7)}"
                class="add-card h-full min-h-[260px] border border-dashed border-slate-300 bg-slate-50/40 rounded-2xl text-slate-500
                       hover:border-blue-400 hover:bg-blue-50/50 hover:text-blue-600 transition flex flex-col items-center justify-center gap-2 group">
          <span class="w-12 h-12 rounded-xl bg-white border border-slate-200 group-hover:border-blue-300 group-hover:bg-blue-50 group-hover:text-blue-600 grid place-items-center text-2xl font-light transition">+</span>
          <span class="text-sm font-medium">导入 ${escapeHtml(product.key)} 账号</span>
          <span class="text-[11px] text-slate-400">上传凭据 JSON 文件</span>
        </button>
      </div>
    </section>
  `;
}

function renderManagedAccountsPanel(data) {
  const accounts = data?.admin?.accounts || [];
  const products = PRODUCTS.map((product) => {
    const rows = accounts.filter((item) => item.product === product.key);
    return {
      product: product.key,
      managed: rows.length,
      signed_today: rows.filter((item) => item.latest?.signed_today).length,
      attention: rows.filter((item) => statusClass(item.health_severity || item.latest?.severity) === "danger").length,
    };
  });
  const enabledCount = accounts.filter((item) => item.enabled !== false).length;
  const attentionCount = accounts.filter((item) => statusClass(item.health_severity || item.latest?.severity) === "danger").length;
  const signedToday = products.reduce((sum, item) => sum + Number(item.signed_today || 0), 0);
  const ownerCount = new Set(accounts.map((item) => item.owner_id || item.owner_username).filter(Boolean)).size;

  return `
    <section class="setting-summary managed-summary">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="setting-live-dot ${accounts.length ? "is-on" : "is-off"}"></span>
            <h3>托管账号</h3>
          </div>
          <p>${accounts.length ? "管理员总览所有用户的 WorkBuddy 与 TRAE 托管账号，可查看归属、状态并维护账号。" : "还没有任何用户导入账号。"}</p>
        </div>
        <span class="setting-status-pill ${accounts.length ? "info" : "neutral"}">${accounts.length} ACCOUNTS</span>
      </div>
      <div class="setting-stat-grid">
        ${settingStat("启用账号", `${enabledCount}/${accounts.length}`, enabledCount ? "success" : "neutral")}
        ${settingStat("今日已签", `${signedToday}`, signedToday ? "success" : "neutral")}
        ${settingStat("所属用户", `${ownerCount}`, ownerCount ? "info" : "neutral")}
        ${settingStat("需要关注", `${attentionCount}`, attentionCount ? "danger" : "neutral")}
      </div>
    </section>
    <div class="managed-product-list">
      ${PRODUCTS.map((product) => {
        const rows = accounts.filter((item) => item.product === product.key);
        const stats = products.find((item) => item.product === product.key) || {};
        const managed = stats.managed ?? rows.length;
        return `
          <section class="managed-product-section">
            <header class="managed-product-head">
              <div class="flex items-center gap-3 min-w-0">
                ${productIconHtml(product.key)}
                <div class="min-w-0">
                  <h3>${escapeHtml(product.key)}</h3>
                  <p>${escapeHtml(product.desc)} · ${managed} 个账号</p>
                </div>
              </div>
              <span class="setting-status-pill neutral">${rows.length} 项</span>
            </header>
            <div class="managed-account-grid">
              ${rows.length ? rows.map((item, index) => renderManagedAccountRow(item, index)).join("") : `
                <div class="managed-empty-add" role="status">
                  <span>—</span>
                  <b>暂无 ${escapeHtml(product.key)} 账号</b>
                  <em>用户导入后会显示在这里</em>
                </div>
              `}
            </div>
          </section>
        `;
      }).join("")}
    </div>
  `;
}

function renderManagedAccountRow(item, index = 0) {
  const displayName = String(item.display_name || item.name || "未命名账号");
  const identity = accountIdentity(item);
  const latest = item.latest || {};
  const today = todayState(item);
  const cred = credentialState(item);
  const enabled = item.enabled !== false;
  const accountKey = item.name || "";
  const ownerId = item.owner_id || item.owner?.id || "";
  const ownerName = item.owner_username || item.owner?.username || "";
  const accountSub = [
    accountKey && accountKey !== displayName ? accountKey : "",
    identity.id ? `ID ${compactMiddle(identity.id, 18)}` : "",
    latest.time ? `最近 ${formatDateTime(latest.time)}` : "暂无执行记录",
  ].filter(Boolean).join(" · ");
  const statusTone = statusClass(item.health_severity || today.severity);
  const ownerAttr = ownerId ? ` data-owner-id="${escapeAttr(ownerId)}"` : "";
  return `
    <article class="managed-account-row" data-managed-account data-account="${escapeAttr(item.name)}"
             data-product="${escapeAttr(item.product)}"${ownerAttr} tabindex="0" style="--i:${Math.min(index, 10)}">
      <div class="managed-account-main">
        <span class="managed-account-index">${escapeHtml(String(index + 1).padStart(2, "0"))}</span>
        <div class="min-w-0">
          <div class="managed-account-title">
            <b>${escapeHtml(displayName)}</b>
            ${ownerName ? `<span class="managed-owner" title="归属 ${escapeHtml(ownerName)}"><i>${escapeHtml(capital(ownerName))}</i><b>${escapeHtml(ownerName)}</b></span>` : ""}
          </div>
          <p title="${escapeAttr(accountSub || item.name || "")}">${escapeHtml(accountSub || item.name || "未记录账号标识")}</p>
        </div>
      </div>
      <div class="managed-account-facts">
        <span class="admin-status ${enabled ? "success" : "neutral"}">${enabled ? "启用" : "停用"}</span>
        <span class="admin-status ${statusTone}" title="${escapeAttr(item.health_label || today.detail || today.label)}">${escapeHtml(item.health_label || today.label)}</span>
        <span class="managed-fact" title="${escapeAttr(cred.detail)}">${escapeHtml(credentialExpiryShort(item))}</span>
        ${latest.credit != null ? `<span class="admin-status success" title="今日签到获得积分">+${escapeHtml(latest.credit)} 分</span>` : ""}
        ${latest.total_credits != null ? `<span class="managed-fact" title="累计积分">总分 ${escapeHtml(latest.total_credits)}</span>` : ""}
      </div>
      <div class="managed-account-actions">
        <button type="button" class="admin-action neutral" data-action="toggle-account"
                data-product="${escapeAttr(item.product)}" data-name="${escapeAttr(item.name)}"
                data-next="${enabled ? "false" : "true"}"${ownerAttr}>${enabled ? "停用" : "启用"}</button>
        <button type="button" class="admin-action danger" data-action="delete-account"
                data-product="${escapeAttr(item.product)}" data-name="${escapeAttr(item.name)}"${ownerAttr}>删除</button>
      </div>
    </article>
  `;
}

function setLiveDot(kind) {
  const dot = $("#liveDot");
  if (!dot) return;
  // 移除旧的 tone class，添加新的
  dot.className = "inline-block w-1.5 h-1.5 rounded-full shadow-[0_0_0_3px_rgba(0,0,0,0.05)]";
  if (kind === "danger") dot.classList.add("bg-rose-500", "shadow-[0_0_0_3px_rgba(244,63,94,0.18)]");
  else if (kind === "warning") dot.classList.add("bg-amber-500", "shadow-[0_0_0_3px_rgba(245,158,11,0.18)]");
  // 断线时不呼吸 —— 呼吸代表「有东西在跑」，离线还呼吸是误导
  else if (kind === "offline") dot.classList.add("bg-slate-400", "is-quiet");
  else dot.classList.add("bg-emerald-500", "shadow-[0_0_0_3px_rgba(16,185,129,0.18)]");
  // 上面是整串覆盖 className，会把同步中的呼吸标记抹掉 —— 正在轮询就补回来。
  if (state.watching) dot.classList.add("is-syncing");
}

/* ---- 汇总渲染 -------------------------------------------------------- */

/* 顶栏那两行小字单独抽出来：内容没变时不走 render()，
   但「更新至 22:41」必须继续往前走 —— 否则看起来像页面卡死了。 */
function updateHeaderStamp(data) {
  const headerTime = $("#headerTime");
  if (headerTime) headerTime.textContent = formatStamp(data.generated_at);
  const meta = $("#headerMeta");
  if (meta) meta.textContent = `共 ${(data.summary || {}).total_accounts || 0} 个账号`;
}

function render(data) {
  state.data = data;
  state.authenticated = true;
  state.username = data.auth?.username || state.username;
  state.role = data.auth?.role || state.role;
  state.status = data.auth?.status || state.status;

  // 头部时间/状态
  updateHeaderStamp(data);
  setLiveDot((data.summary?.attention || 0) ? "danger" : (data.summary?.pending || 0) ? "warning" : "ok");

  /* 入场动画只在「账号结构变化」时播一次。
     每 5 分钟的静默刷新若也重播，整页会一直闪，反而显得没在刷新。 */
  const accounts = data.accounts || [];
  const structure = PRODUCTS
    .map((p) => p.key + ":" + accounts.filter((a) => a.product === p.key).map((a) => a.name + (a.enabled === false ? "-off" : "")).join(","))
    .join("|");
  const entering = structure !== state.groupsSig;
  state.groupsSig = structure;

  /* 和上一轮比对今日状态：变了的卡片闪一下，「实时」才看得出来 */
  const changed = [];
  const nextKeys = {};
  accounts.forEach((item) => {
    const key = `${item.product}/${item.name}`;
    const current = todayState(item).key;
    nextKeys[key] = current;
    if (state.todayKeys[key] && state.todayKeys[key] !== current) changed.push(key);
  });
  state.todayKeys = nextKeys;

  const groups = $("#groups");
  if (groups) {
    groups.className = `space-y-8 sm:space-y-10${entering ? " is-entering" : ""}`;
    groups.innerHTML = PRODUCTS.map(renderGroup).join("");
    if (changed.length) {
      groups.querySelectorAll("article[data-account]").forEach((card) => {
        if (!changed.includes(`${card.dataset.product}/${card.dataset.account}`)) return;
        card.classList.add("is-flash");
        window.setTimeout(() => card.classList.remove("is-flash"), 1600);
      });
    }
  }

  renderSchedules(data.schedules || [], data);
  renderNotifications(data);
  renderRecent(data.recent || []);
  renderArchived(data.archived || []);
  renderAdminUsers(data.admin?.users || []);
  renderSecurity(data);

  state.lastSignature = [
    data.summary?.signed_today,
    data.summary?.attention,
    data.summary?.pending,
  ].join("|");

  // 「盯变化」轮询靠它判断服务端到底有没有新结果
  state.signature = dataSignature(data);
}

/* 已归档账号清单。
   归档是软删除（凭据搬到 archive/），界面上没有这块清单，
   用户点一次「归档」就再也找不到人 —— 这正是之前踩的坑。

   现在只保留顶栏入口和顶栏抽屉，避免首页底部重复出现一块归档列表。 */
function archivedListHtml(rows) {
  return rows.map((row, i) => `
    <li class="archived-row flex items-center gap-3 px-4 sm:px-5 py-3.5" style="--i:${Math.min(i, 7)}">
      ${productIconHtml(row.product, "sm")}
      <div class="min-w-0 flex-1">
        <p class="text-sm font-medium text-slate-800 truncate">${escapeHtml(row.name)}</p>
        <p class="text-[11px] text-slate-500 mt-0.5">
          <span class="font-semibold">${escapeHtml(row.product)}</span> · 归档于 ${escapeHtml(row.archived_at || "未知时间")}
          <span class="text-slate-400 font-mono ml-1.5 hidden sm:inline">${escapeHtml(row.file || "")}</span>
        </p>
      </div>
      <button type="button"
              class="flex-none h-8 px-3 rounded-lg text-sm font-medium bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-100 transition disabled:opacity-60"
              data-action="restore-account" data-product="${escapeAttr(row.product)}" data-name="${escapeAttr(row.name)}">恢复</button>
    </li>
  `).join("");
}

function renderArchived(rows) {
  rows = rows || [];

  // 顶栏入口：归档数为 0 时整颗按钮收起来，不给没归档过的人添噪音。
  const entry = $("#openArchived");
  if (entry) entry.hidden = !rows.length;
  const count = $("#archivedCount");
  if (count) count.textContent = String(rows.length);

  // 抽屉宿主（顶栏点开的那份）
  const list = $("#archivedList");
  if (list) {
    list.innerHTML = rows.length
      ? `<ul class="divide-y divide-slate-100 -mx-4 sm:-mx-5">${archivedListHtml(rows)}</ul>`
      : `<p class="text-xs text-slate-500 py-8 text-center">还没有归档过账号</p>`;
  }

  // 首页底部不再展示归档清单，只保留顶部入口。
  const host = $("#archivedSection");
  if (host) {
    host.hidden = true;
    host.innerHTML = "";
  }
}

function renderSecurity(data) {
  const accountsButton = $("#openAccounts");
  if (accountsButton) accountsButton.hidden = data.auth?.role !== "admin";
  const adminButton = $("#openAdmin");
  if (adminButton) adminButton.hidden = data.auth?.role !== "admin";
  const adminNotificationsButton = $("#openAdminNotifications");
  if (adminNotificationsButton) adminNotificationsButton.hidden = data.auth?.role !== "admin";
  const avatar = $("#userAvatar");
  if (avatar) {
    avatar.textContent = data.auth?.role === "admin" ? "管" : capital(data.auth?.username || "U");
    avatar.title = data.auth?.username ? `${data.auth.username} · ${data.auth.role === "admin" ? "管理员" : "用户"}` : "";
  }
}

function userStatusMeta(status) {
  const map = {
    pending: { label: "待审核", cls: "warning" },
    approved: { label: "已通过", cls: "success" },
    rejected: { label: "已拒绝", cls: "danger" },
    disabled: { label: "已停用", cls: "neutral" },
  };
  return map[String(status || "").toLowerCase()] || { label: status || "未知", cls: "neutral" };
}

function adminUserActions(user) {
  const id = escapeAttr(user.id);
  const role = String(user.role || "user");
  const status = String(user.status || "pending");
  const buttons = [];
  if (status === "pending") {
    buttons.push(`<button type="button" class="admin-action success" data-admin-action="approve" data-user-id="${id}">通过</button>`);
    buttons.push(`<button type="button" class="admin-action danger" data-admin-action="reject" data-user-id="${id}">拒绝</button>`);
  } else if (status === "approved") {
    buttons.push(`<button type="button" class="admin-action neutral" data-admin-action="disable" data-user-id="${id}">停用</button>`);
  } else {
    buttons.push(`<button type="button" class="admin-action success" data-admin-action="activate" data-user-id="${id}">启用</button>`);
  }
  if (status === "approved") {
    buttons.push(role === "admin"
      ? `<button type="button" class="admin-action neutral" data-admin-action="make_user" data-user-id="${id}">设为用户</button>`
      : `<button type="button" class="admin-action" data-admin-action="make_admin" data-user-id="${id}">设为管理员</button>`);
  }
  if (String(user.username || "") !== String(state.username || "")) {
    buttons.push(`<button type="button" class="admin-action danger" data-admin-action="delete" data-user-id="${id}" data-user-name="${escapeAttr(user.username || "")}">删除</button>`);
  }
  return buttons.join("");
}

function adminSection(title, sub, count, body, toneName = "neutral") {
  return `
    <section class="admin-section">
      <header class="admin-section-head">
        <div>
          <h3>${escapeHtml(title)}</h3>
          <p>${escapeHtml(sub || "")}</p>
        </div>
        <span class="setting-status-pill ${toneName}">${escapeHtml(String(count))}</span>
      </header>
      ${body}
    </section>
  `;
}

function adminOwner(user) {
  return user?.username || user?.owner_username || "未知用户";
}

function adminOwnerPill(item) {
  const owner = item.owner || item;
  const meta = userStatusMeta(owner.status || item.owner_status);
  return `
    <span class="admin-owner-badge" title="所属用户：${escapeAttr(adminOwner(owner))}">
      <i>${escapeHtml(capital(adminOwner(owner)))}</i>
      <b>${escapeHtml(adminOwner(owner))}</b>
      <em class="${meta.cls}">${escapeHtml(meta.label)}</em>
    </span>
  `;
}

function renderAdminUserSection(rows) {
  if (!rows.length) {
    return adminSection("注册用户", "暂无注册用户。", "0 USERS", `<p class="admin-empty">暂无用户。</p>`);
  }
  const body = `
    <div class="admin-user-list">
      ${rows.map((user, index) => {
        const meta = userStatusMeta(user.status);
        return `
          <article class="admin-user-card" style="--i:${Math.min(index, 7)}">
            <div class="admin-user-main">
              <span class="admin-user-avatar">${escapeHtml(capital(user.username || "?"))}</span>
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <h3 class="admin-user-name">${escapeHtml(user.username || "-")}</h3>
                  <span class="admin-role ${user.role === "admin" ? "is-admin" : ""}">${user.role === "admin" ? "管理员" : "用户"}</span>
                  <span class="admin-status ${meta.cls}">${escapeHtml(meta.label)}</span>
                </div>
                <p class="admin-user-meta">
                  注册 ${escapeHtml(formatDateTime(user.created_at))}
                  ${user.last_login_at ? ` · 最近登录 ${escapeHtml(formatDateTime(user.last_login_at))}` : ""}
                </p>
              </div>
            </div>
            <div class="admin-user-actions">${adminUserActions(user)}</div>
          </article>`;
      }).join("")}
    </div>`;
  return adminSection("注册用户", "审核注册、停用账号、调整管理员权限。", `${rows.length} USERS`, body, "info");
}

function renderAdminAccountSection(rows) {
  rows = rows || [];
  if (!rows.length) {
    return adminSection("托管账号", "还没有任何用户导入签到账号。", "0 ACCOUNTS", `<p class="admin-empty">暂无托管账号。</p>`);
  }
  const body = `
    <div class="admin-owned-list">
      ${rows.map((item, index) => {
        const displayName = String(item.display_name || item.name || "未命名账号");
        const identity = accountIdentity(item);
        const health = statusClass(item.health_severity || item.latest?.severity);
        const enabled = item.enabled !== false;
        return `
          <article class="admin-owned-card" style="--i:${Math.min(index, 10)}">
            <div class="admin-owned-main">
              ${productIconHtml(item.product, "sm")}
              <div class="min-w-0">
                <div class="admin-owned-title">
                  <b>${escapeHtml(displayName)}</b>
                  ${adminOwnerPill(item)}
                </div>
                <p title="${escapeAttr(identity.id || item.name || "")}">
                  ${escapeHtml(item.product || "-")} · ${escapeHtml(item.name || "-")}
                  ${identity.id ? ` · ID ${escapeHtml(compactMiddle(identity.id, 18))}` : ""}
                </p>
              </div>
            </div>
            <div class="admin-owned-meta">
              <span class="admin-status ${health}">${escapeHtml(item.health_label || item.latest?.label || "未记录")}</span>
              <span class="admin-status ${enabled ? "success" : "neutral"}">${enabled ? "启用" : "停用"}</span>
            </div>
          </article>`;
      }).join("")}
    </div>`;
  return adminSection("托管账号", "所有用户导入的 WorkBuddy / TRAE 凭据，已标明所属用户。", `${rows.length} ACCOUNTS`, body, "success");
}

function renderAdminNotificationSection(rows) {
  rows = rows || [];
  if (!rows.length) {
    return adminSection("通知配置", "还没有可查看的通知配置。", "0 PUSH", `<p class="admin-empty">暂无通知配置。</p>`);
  }
  const enabledCount = rows.filter((item) => item.enabled).length;
  const body = `
    <div class="admin-notify-list">
      ${rows.map((item, index) => {
        const credential = item.has_key || item.has_url ? "凭据已保存" : "未配置凭据";
        const credentialTone = item.has_key || item.has_url ? "success" : "warning";
        const urlLine = item.has_url ? `
          <span><b>URL</b><code>${escapeHtml(item.url || "")}</code></span>
        ` : "";
        const secretBlock = urlLine ? `
                <div class="admin-secret-lines">
                  ${urlLine}
                </div>
        ` : "";
        return `
          <article class="admin-notify-card" style="--i:${Math.min(index, 10)}">
            <div class="admin-notify-main">
              ${productIconHtml(item.product, "sm")}
              <div class="min-w-0">
                <div class="admin-owned-title">
                  <b>${escapeHtml(item.product || "-")} 通知</b>
                  ${adminOwnerPill(item)}
                </div>
                <p title="${escapeAttr(item.path || "")}">
                  渠道：${escapeHtml(channelLabel(item.channel))} · 策略：${escapeHtml(ON_LABEL[item.on] || item.on || "未设置")} · 分组：${escapeHtml(item.group || item.product || "-")}
                </p>
                ${secretBlock}
              </div>
            </div>
            <div class="admin-notify-meta">
              <span class="admin-status info">${escapeHtml(channelLabel(item.channel))}</span>
              <span class="admin-status ${item.enabled ? "success" : "neutral"}">${item.enabled ? "已启用" : "已关闭"}</span>
              <span class="admin-status ${credentialTone}">${escapeHtml(credential)}</span>
            </div>
          </article>`;
      }).join("")}
    </div>`;
  return adminSection("通知配置", "按用户查看每个产品的推送渠道、策略与 URL；敏感字段不在页面展示。", `${enabledCount}/${rows.length} ON`, body, enabledCount ? "info" : "neutral");
}

function renderAdminNotificationsPanel(data) {
  const rows = (data?.admin?.notifications || []).filter((item) => item.enabled);
  const enabledCount = rows.filter((item) => item.enabled).length;
  const configuredCount = rows.filter((item) => item.has_key || item.has_url).length;
  const userCount = new Set(rows.map((item) => item.owner_username || item.owner_id || "")).size;
  return `
    <section class="setting-summary admin-summary">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="setting-live-dot ${enabledCount ? "is-on" : "is-off"}"></span>
            <h3>通知总览</h3>
          </div>
          <p>只读查看所有用户「已启用」的通知配置；未启用的配置自动隐藏。</p>
        </div>
        <span class="setting-status-pill ${enabledCount ? "info" : "neutral"}">READ ONLY</span>
      </div>
      <div class="admin-overview-grid">
        ${settingStat("所属用户", `${userCount}`, userCount ? "info" : "neutral")}
        ${settingStat("启用配置", `${enabledCount}`, enabledCount ? "success" : "neutral")}
        ${settingStat("已存凭据", `${configuredCount}/${enabledCount}`, configuredCount ? "warning" : "neutral")}
      </div>
    </section>
    ${renderAdminNotificationSection(rows)}
  `;
}

/* =========================================================================
   数据统计（管理员 · 全站）：总览卡 + 注册趋势折线 + 签到效果堆叠柱
   纯 CSS/SVG 绘制，不引入第三方图表库；沿用现有设计令牌。
   ====================================================================== */
function statsLineChart(points) {
  const W = 640, H = 190, PAD = 16;
  const n = points.length;
  if (!n) return `<p class="stat-empty">暂无数据</p>`;
  const max = Math.max(1, ...points.map((p) => p.count || 0));
  const xStep = n > 1 ? (W - PAD * 2) / (n - 1) : 0;
  const coords = points.map((p, i) => {
    const x = PAD + i * xStep;
    const y = H - PAD - ((p.count || 0) / max) * (H - PAD * 2);
    return { x, y, date: p.date, count: p.count || 0 };
  });
  const line = coords.map((c) => `${Math.round(c.x)},${Math.round(c.y)}`).join(" ");
  const area = `${Math.round(coords[0].x)},${H - PAD} ${line} ${Math.round(coords[n - 1].x)},${H - PAD}`;
  const labelStep = Math.ceil(n / 6);
  const labels = coords
    .filter((_, i) => i === 0 || i === n - 1 || i % labelStep === 0)
    .map((c) => `<text x="${c.x}" y="${H - 3}" text-anchor="middle" class="stat-tx">${escapeHtml(String(c.date).slice(5))}</text>`)
    .join("");
  const dots = coords.map((c) =>
    `<circle cx="${c.x}" cy="${c.y}" r="2.6" class="stat-dot"><title>${escapeHtml(c.date)} · ${c.count}</title></circle>`
  ).join("");
  return `
    <div class="stat-wrap-chart">
      <span class="stat-max">${max}</span>
      <svg viewBox="0 0 ${W} ${H}" class="stat-svg" role="img" aria-label="注册趋势">
        <defs>
          <linearGradient id="statRegFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stop-color="#3b82f6" stop-opacity="0.22"/>
            <stop offset="1" stop-color="#3b82f6" stop-opacity="0"/>
          </linearGradient>
        </defs>
        <polygon points="${area}" fill="url(#statRegFill)"/>
        <polyline points="${line}" fill="none" class="stat-line"/>
        ${dots}
        ${labels}
      </svg>
    </div>`;
}

function statsStackedBars(points) {
  const H = 138;
  if (!points.length) return `<p class="stat-empty">暂无数据</p>`;
  const totals = { success: 0, fail: 0, skip: 0 };
  const bars = points.map((p) => {
    const total = (p.success || 0) + (p.fail || 0) + (p.skip || 0);
    totals.success += p.success || 0;
    totals.fail += p.fail || 0;
    totals.skip += p.skip || 0;
    const base = total || 1;
    const s = Math.round(((p.success || 0) / base) * H);
    const f = Math.round(((p.fail || 0) / base) * H);
    const k = Math.max(0, H - s - f);
    const seg = (cls, h) => h > 0 ? `<i class="stat-seg ${cls}" style="height:${h}px"></i>` : "";
    return `
      <div class="stat-col" title="${escapeAttr(`${p.date} · 成功 ${p.success} / 失败 ${p.fail} / 轮空 ${p.skip}`)}">
        <div class="stat-bar"${total ? "" : ' data-empty="1"'}>
          ${seg("stat-seg--success", s)}
          ${seg("stat-seg--fail", f)}
          ${seg("stat-seg--skip", k)}
        </div>
        <span class="stat-x">${escapeHtml(String(p.date).slice(5))}</span>
      </div>`;
  }).join("");
  return `
    <div class="stat-cols">${bars}</div>
    <div class="stat-legend">
      <i><em class="stat-seg stat-seg--success"></em>成功 ${totals.success}</i>
      <i><em class="stat-seg stat-seg--fail"></em>失败 ${totals.fail}</i>
      <i><em class="stat-seg stat-seg--skip"></em>轮空 ${totals.skip}</i>
    </div>`;
}

/* 首页内嵌统计块：只在 admin 显示，独立于产品卡片轮询刷新。
   主轮询把 /api/status/stats 并进 payload.stats，这里按需重建 #homeStats；
   指纹没变就整个不动，避免图表每次轮询都重播动画。 */
function updateStats(data) {
  const host = $("#homeStats");
  if (!host) return;
  const isAdmin = data.auth?.role === "admin";
  if (!isAdmin || !data.stats) {
    host.hidden = true;
    return;
  }
  const sig = JSON.stringify([
    data.stats.overview,
    (data.stats.registration_trend || {}).points,
    (data.stats.signin_effect || {}).points,
  ]);
  if (host._statsSig === sig) return;
  host._statsSig = sig;
  host.hidden = false;
  host.innerHTML = renderStatsPanel(data.stats);
}

function renderStatsPanel(stats) {
  if (!stats) return `<p class="text-xs text-slate-500">暂无统计数据</p>`;
  const ov = stats.overview || {};
  const reg = stats.registration_trend || {};
  const sig = stats.signin_effect || {};
  return `
    <div class="stats-wrap space-y-4">
      <section class="setting-summary admin-summary">
        <div class="flex items-start justify-between gap-4">
          <div>
            <div class="flex items-center gap-2">
              <span class="setting-live-dot is-on"></span>
              <h3>全站概览</h3>
            </div>
            <p>用户、托管账号与今日签到汇总</p>
          </div>
          <span class="setting-status-pill info">STATS</span>
        </div>
        <div class="admin-overview-grid">
          ${settingStat("用户总数", ov.total_users ?? 0, "info")}
          ${settingStat("待审核", ov.pending_users ?? 0, (ov.pending_users || 0) > 0 ? "warning" : "neutral")}
          ${settingStat("托管账号", ov.managed_accounts ?? 0, "info")}
          ${settingStat("已启用", ov.enabled_accounts ?? 0, "success")}
          ${settingStat("今日成功", ov.signed_today ?? 0, (ov.signed_today || 0) > 0 ? "success" : "neutral")}
        </div>
      </section>

      <section class="stat-card">
        <div class="stat-card-head">
          <div><h3>注册趋势</h3><p>近 ${reg.days || 30} 天新增注册</p></div>
        </div>
        ${statsLineChart(reg.points || [])}
      </section>

      <section class="stat-card">
        <div class="stat-card-head">
          <div><h3>签到效果</h3><p>近 ${sig.days || 7} 天 成功 / 失败 / 轮空</p></div>
        </div>
        ${statsStackedBars(sig.points || [])}
      </section>
    </div>
  `;
}

function renderAdminUsers(rows) {
  const host = $("#adminList");
  if (!host) return;
  rows = rows || [];
  const pending = rows.filter((user) => user.status === "pending").length;
  const approved = rows.filter((user) => user.status === "approved").length;
  const admins = rows.filter((user) => user.role === "admin").length;
  const disabled = rows.filter((user) => user.status === "disabled").length;
  host.innerHTML = `
    <section class="setting-summary admin-summary">
      <div class="flex items-start justify-between gap-4">
        <div>
          <div class="flex items-center gap-2">
            <span class="setting-live-dot ${pending ? "is-waiting" : "is-on"}"></span>
            <h3>${pending ? `${pending} 个账号待审核` : "用户状态正常"}</h3>
          </div>
          <p>注册账号必须审核通过后才能登录工作台。</p>
        </div>
        <span class="setting-status-pill ${pending ? "warning" : "success"}">${rows.length} USERS</span>
      </div>
      <div class="admin-overview-grid">
        ${settingStat("已通过用户", `${approved}/${rows.length}`, approved ? "success" : "neutral")}
        ${settingStat("管理员", `${admins}`, admins ? "info" : "neutral")}
        ${settingStat("已停用", `${disabled}`, disabled ? "warning" : "neutral")}
      </div>
    </section>
    ${renderAdminUserSection(rows)}
  `;
}

/* ---- 最近记录 -------------------------------------------------------- */

const SEV_PILL = {
  success: "bg-emerald-50 text-emerald-700 border-emerald-200",
  warning: "bg-amber-50 text-amber-700 border-amber-200",
  danger:  "bg-rose-50 text-rose-700 border-rose-200",
  neutral: "bg-slate-100 text-slate-600 border-slate-200",
};
const sevPill = (sev) => SEV_PILL[statusClass(sev)] || SEV_PILL.neutral;

/* ---- 执行记录：日期分组用的小工具 ------------------------------------ */
function startOfDay(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return null;
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}
function dayKey(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "?";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function dayLabel(value) {
  const start = startOfDay(value);
  if (start == null) return "未知时间";
  const today = startOfDay(new Date());
  const diff = Math.round((today - start) / 86400000);
  const d = new Date(value);
  const week = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][d.getDay()];
  if (diff === 0) return `今天 · ${week}`;
  if (diff === 1) return `昨天 · ${week}`;
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${week}`;
}
function formatTimeShort(value) {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "--:--";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
/* 产品头像：优先使用产品图标，缺失时回落到字母渐变 */
const PRODUCT_AVATAR = {
  WorkBuddy: "from-blue-500 to-indigo-600",
  TRAE: "from-slate-600 to-slate-800",
};
const PRODUCT_ICON_SRC = {
  WorkBuddy: "/assets/product-workbuddy.webp?v=1",
  TRAE: "/assets/product-trae.webp?v=1",
};

function recentDayHtml(value, count) {
  return `
    <div class="recent-day flex items-center gap-2.5 px-1 pt-4 pb-1">
      <span class="text-[11px] font-bold tracking-wide text-slate-400 flex-none">${escapeHtml(dayLabel(value))}</span>
      <span class="h-px flex-1 bg-slate-100"></span>
      <span class="text-[10px] text-slate-300 font-mono flex-none">${count} 条</span>
    </div>`;
}

function recentRowHtml(row, idx) {
  const expandable = !!row.log_path;
  const bad = statusClass(row.severity) === "danger";
  const detail = [];
  if (row.credit != null) detail.push(`<b class="text-emerald-600 font-semibold">+${row.credit}</b>`);
  if (bad && row.note && row.note !== row.label) detail.push(escapeHtml(row.note));
  return `
    <div class="recent-row recent-row-card">
      <button type="button" ${expandable ? "" : "disabled"} data-log-toggle data-idx="${idx}" aria-expanded="false" title="${expandable ? "展开本次日志" : "这条记录没有关联日志"}"
              class="w-full flex items-center gap-3 text-left transition group
                     ${expandable ? "cursor-pointer hover:bg-slate-50" : "cursor-default"}">
        ${productIconHtml(row.product, "sm")}
        <span class="flex-1 min-w-0">
          <span class="flex items-center gap-1.5 min-w-0">
            <b class="text-[13px] font-semibold text-slate-900 truncate">${escapeHtml(row.account)}</b>
            ${row.streak_days ? `<span class="text-[10px] text-slate-400 flex-none whitespace-nowrap">连续 ${row.streak_days} 天</span>` : ""}
          </span>
          <span class="flex items-center gap-1 text-[11px] text-slate-500 mt-0.5 min-w-0">
            <span class="truncate">${escapeHtml(row.product)}${detail.length ? ` · ${detail.join(" · ")}` : ""}</span>
          </span>
        </span>
        <span class="flex-none flex flex-col items-end gap-1">
          <span class="text-[11px] font-semibold px-2 py-0.5 rounded-md border ${sevPill(row.severity)}">${escapeHtml(row.label)}</span>
          <span class="text-[11px] text-slate-400 font-mono tabular-nums">${formatTimeShort(row.time)}</span>
        </span>
        ${expandable ? `
        <svg class="w-3.5 h-3.5 text-slate-300 flex-none group-hover:text-slate-400" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="6 9 12 15 18 9"/>
        </svg>` : `<span class="w-3.5 flex-none"></span>`}
      </button>
      <div class="hidden pb-3" data-log-panel></div>
    </div>`;
}

function recentSummaryHtml(rows) {
  const success = rows.filter((row) => statusClass(row.severity) === "success").length;
  const warning = rows.filter((row) => statusClass(row.severity) === "warning").length;
  const danger = rows.filter((row) => statusClass(row.severity) === "danger").length;
  const latest = rows[0]?.time ? formatDateTime(rows[0].time) : "暂无";
  return `
    <section class="setting-summary recent-summary">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="setting-live-dot ${danger ? "is-off" : "is-on"}"></span>
            <h3>最近执行记录</h3>
          </div>
          <p>${rows.length ? `最近一条 ${latest}` : "暂无执行记录"}</p>
        </div>
        <span class="setting-status-pill ${danger ? "danger" : "success"}">${danger ? "NEED CHECK" : "STABLE"}</span>
      </div>
      <div class="setting-stat-grid">
        ${settingStat("成功", `${success}`, "success")}
        ${settingStat("待签", `${warning}`, warning ? "warning" : "neutral")}
        ${settingStat("异常", `${danger}`, danger ? "danger" : "neutral")}
      </div>
    </section>
  `;
}

function renderRecent(rows) {
  state.recentRows = rows || [];
  const count = $("#recentCount");
  if (count) count.textContent = rows.length ? `${rows.length} 条` : "";
  const list = $("#recentList");
  if (!list) return;
  if (!rows.length) {
    list.innerHTML = recentSummaryHtml([]) + `
      <div class="setting-card empty-state">
        <p class="text-sm text-slate-500 text-center py-8">暂无执行记录</p>
      </div>`;
    return;
  }
  // 只展示最近 20 条，但按自然日分组 —— 不分组的话 20 行同款连排，看不出节奏
  const visible = rows.slice(0, 20);
  const parts = [];
  let lastDay = null;
  visible.forEach((row, idx) => {
    const key = dayKey(row.time);
    if (key !== lastDay) {
      lastDay = key;
      const count = visible.filter((r) => dayKey(r.time) === key).length;
      parts.push(recentDayHtml(row.time, count));
    }
    parts.push(recentRowHtml(row, idx));
  });
  list.innerHTML = recentSummaryHtml(visible) + `<div class="recent-feed">${parts.join("")}</div>`;
}

// 写入内容并重播一次淡入 —— 直接改 innerHTML 是硬切，看起来像闪了一下。
function revealInto(panel, html) {
  panel.innerHTML = html;
  panel.classList.remove("reveal");
  void panel.offsetWidth;
  panel.classList.add("reveal");
}

/* ---- 执行日志的结构化渲染 -----------------------------------------------
   日志一行就是一次运行：[时间] {JSON}。原样甩 <pre> 的问题是
   一行几百字符只能横向滚，关键信息（结果/报告/积分）全埋在里面。
   所以按行解析：JSON 的字段表格化（key 暗色、value 可换行），解析不了的原样保留。 */
const LOG_RESULT_TONE = {
  ALREADY: "success", CLAIMED: "success", GROWTH: "success", AUTH_READY: "success", STATUS: "success",
  AUTH_ERROR: "danger", AUTH_REJECTED: "danger", FORBIDDEN: "danger", NO_AUTH: "danger",
  NO_SESSION: "danger", NETWORK: "danger", TIMEOUT: "danger", ERROR: "danger", UNKNOWN: "danger",
  INACTIVE: "neutral",
};
const LOG_RESULT_LABEL = {
  ALREADY: "今日已签", CLAIMED: "已领取", GROWTH: "成长中心", AUTH_READY: "凭据有效", STATUS: "状态正常",
  INACTIVE: "活动未开启", AUTH_ERROR: "认证被拒", AUTH_REJECTED: "认证被拒", FORBIDDEN: "权限拒绝",
  NO_AUTH: "无凭据", NO_SESSION: "无会话", NETWORK: "网络异常", TIMEOUT: "执行超时",
  ERROR: "执行异常", UNKNOWN: "未知状态",
};
/* 常见字段给中文名；没见过的键原样展示 */
const LOG_FIELD_LABEL = {
  report: "报告", note: "说明", credit: "本次积分", today_credit: "今日积分",
  streak_days: "连续天数", total_credits: "累计积分", growth: "成长中心", growth_result: "成长结果",
  checked_in: "已签到", needs_attention: "需关注", is_streak_day: "连签日", next_streak_day: "下次连签",
};
const LOG_FIELD_ORDER = ["report", "note", "credit", "today_credit", "streak_days", "total_credits",
  "growth", "growth_result", "checked_in", "needs_attention", "is_streak_day", "next_streak_day"];
/* 深色终端里配徽标，得用半透明色，不能照搬浅色页面的 pill 配色 */
const LOG_TONE_BADGE = {
  success: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  warning: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  danger: "bg-rose-500/15 text-rose-400 border-rose-500/30",
  neutral: "bg-slate-500/15 text-slate-400 border-slate-500/30",
};

function formatLogValue(key, value) {
  if (value === null || value === undefined || value === "") {
    return `<span class="text-slate-600">—</span>`;
  }
  if (typeof value === "boolean") {
    if (value && key === "needs_attention") return `<span class="text-rose-400 font-semibold">是</span>`;
    return value ? `<span class="text-emerald-400">是</span>` : `<span class="text-slate-500">否</span>`;
  }
  if (typeof value === "number") return `<span class="font-mono tabular-nums">${value}</span>`;
  return escapeHtml(String(value));
}

function logRunHtml(ts, obj, idx) {
  const code = String(obj.result || "").toUpperCase();
  const tone = LOG_RESULT_TONE[code] || "neutral";
  const label = LOG_RESULT_LABEL[code] || code || "—";
  // result 已经在上面那颗徽标里了，字段表里不再重复
  const entries = Object.entries(obj).filter(([key]) => key !== "result");
  entries.sort((a, b) => {
    const ia = LOG_FIELD_ORDER.indexOf(a[0]);
    const ib = LOG_FIELD_ORDER.indexOf(b[0]);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });
  const rows = entries.map(([key, value]) => `
    <div class="contents">
      <dt class="text-[10px] font-mono text-slate-500 pt-1 truncate" title="${escapeAttr(key)}">${escapeHtml(LOG_FIELD_LABEL[key] || key)}</dt>
      <dd class="text-[11px] leading-relaxed text-slate-300 pt-1 break-words min-w-0">${formatLogValue(key, value)}</dd>
    </div>`).join("");
  return `
    <div class="px-3 py-2.5 ${idx ? "border-t border-slate-800" : ""}">
      <div class="flex items-center gap-2 mb-0.5">
        <span class="text-[10px] font-mono text-slate-500 flex-none">${escapeHtml(ts)}</span>
        <span class="text-[10px] font-semibold px-1.5 py-px rounded border ${LOG_TONE_BADGE[tone]}">${escapeHtml(label)}</span>
        <span class="ml-auto text-[10px] text-slate-600 font-mono flex-none">#${idx + 1}</span>
      </div>
      <dl class="grid grid-cols-[76px_1fr] gap-x-3 m-0">${rows}</dl>
    </div>`;
}

function logPlainHtml(line, idx) {
  return `
    <div class="px-3 py-2 ${idx ? "border-t border-slate-800" : ""}">
      <pre class="m-0 text-[11px] leading-relaxed text-slate-300 font-mono whitespace-pre-wrap break-words">${escapeHtml(line)}</pre>
    </div>`;
}

function logLineHtml(line, idx) {
  const match = /^\[([^\]]+)\]\s*(\{[\s\S]*\})$/.exec(line.trim());
  if (match) {
    try {
      const obj = JSON.parse(match[2]);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) return logRunHtml(match[1], obj, idx);
    } catch (error) {
      // JSON 截断或非对象 → 按纯文本渲染，别把日志吞了
    }
  }
  return logPlainHtml(line, idx);
}

async function loadLogPanel(panel, logPath, options = {}) {
  if (!panel) return;
  const emptyText = options.emptyText || "该账号暂无 .log 文件。";
  const linesCount = Number(options.lines || 160);
  if (!logPath) {
    revealInto(panel, `<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">${escapeHtml(emptyText)}</p>`);
    return;
  }
  revealInto(panel, `<p class="text-xs text-slate-500 px-1 py-2">正在读取 .log 文件…</p>`);
  try {
    const res = await decryptedFetch(`/api/log?path=${encodeURIComponent(logPath)}&lines=${Math.max(1, Math.min(linesCount, 500))}`, {
      cache: "no-store",
    });
    const payload = res.data && typeof res.data === "object" ? res.data : {};
    if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
    if (!payload.exists) {
      revealInto(panel, `<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">${escapeHtml(payload.error || "日志文件不存在")}</p>`);
      return;
    }
    const lines = payload.lines || [];
    const raw = lines.join("\n");
    const kb = Math.max(1, Math.round((payload.size || 0) / 1024));
    const body = lines.length
      ? (options.raw
        ? `<pre class="log-raw">${escapeHtml(raw)}</pre>`
        : lines.map((line, i) => logLineHtml(line, i)).join(""))
      : `<p class="px-3 py-4 text-[11px] text-slate-500 text-center">日志为空，还没有写入过记录。</p>`;
    revealInto(panel, `
      <div class="rounded-lg overflow-hidden border border-slate-200">
        <div class="flex items-center justify-between gap-2 px-3 py-1.5 bg-slate-800">
          <span class="text-[10px] text-slate-400 font-mono truncate" title="${escapeAttr(payload.path)}">${escapeHtml(payload.path)}</span>
          <span class="flex items-center gap-2 flex-none">
            <span class="text-[10px] text-slate-500 whitespace-nowrap">末尾 ${lines.length} / 共 ${payload.total_lines} 行 · ${kb} KB</span>
            <button type="button" data-copy-log
                    class="h-5 px-1.5 rounded text-[10px] font-semibold border border-slate-600 text-slate-300 hover:bg-slate-700 hover:text-white transition">复制</button>
          </span>
        </div>
        <div class="log-scroll bg-slate-900 max-h-96 overflow-y-auto">${body}</div>
      </div>`);
    const copyButton = panel.querySelector("[data-copy-log]");
    if (copyButton) {
      copyButton.addEventListener("click", () => {
        copyText(raw)
          .then(() => showToast("日志已复制到剪贴板"))
          .catch(() => showToast("复制失败，请手动选择文本"));
      });
    }
  } catch (error) {
    revealInto(panel, `<p class="text-xs text-rose-700 bg-rose-50 border border-rose-200 rounded-lg px-3 py-2">读取失败：${escapeHtml(error.message)}</p>`);
  }
}

// 点一条执行记录 → 拉取那次跑的账号日志。内容会被搬进抽屉，
// 所以监听只能挂在 document 上（搬过去的是 innerHTML 字符串，原节点上的事件不会跟随）。
async function toggleRecentLog(button) {
  const panel = button.parentElement?.querySelector("[data-log-panel]");
  if (!panel) return;
  const row = (state.recentRows || [])[Number(button.dataset.idx)];
  if (!row) return;

  const icon = button.querySelector("svg");
  if (button.getAttribute("aria-expanded") === "true") {
    button.setAttribute("aria-expanded", "false");
    if (icon) icon.style.transform = "";
    panel.classList.add("hidden");
    panel.innerHTML = "";
    return;
  }
  button.setAttribute("aria-expanded", "true");
  if (icon) icon.style.transform = "rotate(180deg)";
  panel.classList.remove("hidden");

  if (!row.log_path) {
    revealInto(panel, `<p class="text-xs text-slate-500 px-1 py-2">这条记录没有关联日志文件。</p>`);
    return;
  }
  revealInto(panel, `<p class="text-xs text-slate-500 px-1 py-2">正在读取日志…</p>`);
  try {
    const res = await decryptedFetch(`/api/log?path=${encodeURIComponent(row.log_path)}&lines=120`, {
      cache: "no-store",
    });
    const payload = res.data && typeof res.data === "object" ? res.data : {};
    if (!payload.exists) {
      revealInto(panel, `<p class="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">${escapeHtml(payload.error || "日志文件不存在")}</p>`);
      return;
    }
    const lines = payload.lines || [];
    const raw = lines.join("\n");
    const kb = Math.max(1, Math.round((payload.size || 0) / 1024));
    const body = lines.length
      ? lines.map((line, i) => logLineHtml(line, i)).join("")
      : `<p class="px-3 py-4 text-[11px] text-slate-500 text-center">日志为空，还没有写入过记录。</p>`;
    revealInto(panel, `
      <div class="rounded-lg overflow-hidden border border-slate-200">
        <div class="flex items-center justify-between gap-2 px-3 py-1.5 bg-slate-800">
          <span class="text-[10px] text-slate-400 font-mono truncate" title="${escapeAttr(payload.path)}">${escapeHtml(payload.path)}</span>
          <span class="flex items-center gap-2 flex-none">
            <span class="text-[10px] text-slate-500 whitespace-nowrap">末尾 ${lines.length} / 共 ${payload.total_lines} 行 · ${kb} KB</span>
            <button type="button" data-copy-log
                    class="h-5 px-1.5 rounded text-[10px] font-semibold border border-slate-600 text-slate-300 hover:bg-slate-700 hover:text-white transition">复制</button>
          </span>
        </div>
        <div class="log-scroll bg-slate-900 max-h-80 overflow-y-auto">${body}</div>
      </div>`);
    // 复制按钮是 innerHTML 渲染后才存在的，闭包里带上原始文本；
    // 下次渲染会整体换掉这个节点，监听器也随之销毁，不会堆积。
    const copyButton = panel.querySelector("[data-copy-log]");
    if (copyButton) {
      copyButton.addEventListener("click", () => {
        copyText(raw)
          .then(() => showToast("日志已复制到剪贴板"))
          .catch(() => showToast("复制失败，请手动选择文本"));
      });
    }
  } catch (error) {
    revealInto(panel, `<p class="text-xs text-rose-700 px-1 py-2">读取失败：${escapeHtml(error.message)}</p>`);
  }
}

// 「清除」是显式意图：留空提交代表「不改」，所以点过清除才带上 *_clear 标记。
function markNotificationClear(button) {
  const form = button.closest("form[data-product]");
  if (!form) return;
  const field = button.dataset.clear;
  const input = form.querySelector(`input[name="${field}"]`);
  if (input) {
    input.value = "";
    input.placeholder = "已标记清除，保存后生效";
  }
  button.dataset.done = "1";
  button.textContent = "待清除";
  button.classList.add("text-rose-600", "border-rose-200", "bg-rose-50");
  showToast("已标记清除，点保存生效");
}

/* =========================================================================
   抽屉：账号详情 + 设置面板
   ====================================================================== */

function findAccount(name, product) {
  return (state.data?.accounts || []).find((item) => item.name === name && item.product === product) || null;
}

// kind 必须显式传：它决定 refreshActivePanel 认出当前是哪一屏。
// 不要从 title 反推 —— title 是会随时调整的中文文案，不是稳定的标识。
function openPanel({ title, sub = "", product = "", body = "", kind = "" }) {
  const drawer = $("#drawer");
  const mask = $("#drawerMask");
  if (!drawer || !mask) return;

  const chip = $("#drawerProduct");
  if (chip) {
    chip.textContent = product;
    chip.hidden = !product;
  }
  const titleNode = $("#drawerTitle");
  if (titleNode) titleNode.textContent = title;
  const subNode = $("#drawerSub");
  if (subNode) {
    subNode.textContent = sub;
    subNode.hidden = !sub;
  }
  const bodyNode = $("#drawerBody");
  if (bodyNode) {
    bodyNode.innerHTML = body;
    bodyNode.scrollTop = 0;
    // 新面板 = 干净的：刚渲染完还没有任何用户输入，
    // 静默刷新可以放心重建它（否则上一次编辑留下的 dirty 会一直卡住自动刷新）。
    state.panelDirty = false;
    // 摘掉再加 + 强制回流，切换面板时内容才会重新错位淡入
    bodyNode.classList.remove("is-swap");
    void bodyNode.offsetWidth;
    bodyNode.classList.add("is-swap");
  }

  window.clearTimeout(state.closeTimer);
  mask.hidden = false;
  drawer.hidden = false;
  nextFrame(() => {
    mask.classList.add("is-open");
    drawer.classList.add("is-open");
  });
  state.panel = kind || title;
  $("#drawerClose")?.focus();
}

function openDrawer(name, product) {
  const item = findAccount(name, product);
  if (!item) return;
  const displayName = String(item.display_name || item.name || "账号详情");
  const accountName = String(item.name || "");
  const subBits = [item.product, accountName && accountName !== displayName ? `账号 ${accountName}` : "", todayState(item).label]
    .filter(Boolean);
  state.activeAccount = { name, product };
  state.panel = "account";
  openPanel({
    title: displayName,
    sub: subBits.join(" · "),
    product: item.product,
    kind: "account",
    body: renderDrawerBody(item),
  });
  const logPanel = $("#drawerBody [data-account-log-panel]");
  if (logPanel) loadLogPanel(logPanel, item.log_path || "", { lines: 160, raw: true });
}

function openSettings(kind) {
  if (!state.data) {
    showToast("正在读取状态，请稍候");
    return;
  }
  if ((kind === "admin" || kind === "adminNotifications") && state.data.auth?.role !== "admin") {
    showToast("需要管理员权限");
    return;
  }
  const panels = {
    accounts: { title: "托管账号", sub: "凭据导入、账号状态与维护", body: () => renderManagedAccountsPanel(state.data) },
    schedules: { title: "时间计划", sub: "每日签到与轮询节奏", host: "#scheduleList" },
    notifications: { title: "通知配置", sub: "Server酱、飞书、企微、Bark 或 Webhook", host: "#notificationList" },
    recent: { title: "最近执行记录", sub: "", host: "#recentList" },
    archived: { title: "已归档账号", sub: "归档只是把凭据搬走，点「恢复」即可搬回", host: "#archivedList" },
    admin: { title: "用户管理", sub: "注册审核与管理员权限", host: "#adminList" },
    adminNotifications: { title: "通知总览", sub: "所有用户通知配置，只读查看", body: () => renderAdminNotificationsPanel(state.data) },
    help: { title: "使用指南", sub: "一步步上手：导出 → 托管 → 计划 → 通知", host: "#helpPanel" },
  };
  const cfg = panels[kind];
  if (!cfg) return;

  const node = cfg.host ? $(cfg.host) : null;
  const recentCount = $("#recentCount")?.textContent || "";
  const archivedCount = (state.data?.archived || []).length;
  const sub =
    kind === "recent" ? recentCount || "暂无记录"
    : kind === "archived" ? (archivedCount ? `${archivedCount} 个 · 恢复后立即重新参与签到` : "暂无归档")
    : kind === "accounts" ? `${(state.data?.accounts || []).length} 个账号 · 点击卡片查看详情`
    : kind === "admin" ? `${(state.data?.admin?.users || []).length} 个用户 · 注册审核与权限`
    : kind === "adminNotifications" ? `${(state.data?.admin?.notifications || []).length} 项配置 · 只读`
    : cfg.sub;

  state.activeAccount = null;
  openPanel({
    title: cfg.title,
    sub,
    product: "",
    kind,
    body: cfg.body ? cfg.body() : node ? node.innerHTML : `<p class="text-xs text-slate-500">暂无内容</p>`,
  });
}

/* =========================================================================
   页面级按需加载：点哪个页面，就只拉那个页面对应的 /api/status/{kind} 子资源，
   合并进 state.data 后只重画该面板，不再每次把全部子端点重发一遍。
   ====================================================================== */
function statusSliceUrl(kind) {
  return kind === "recent" ? "/api/status/recent?limit=80" : `/api/status/${kind}`;
}

async function fetchOneStatusPart(kind, signal) {
  const res = await decryptedFetch(statusSliceUrl(kind), { cache: "no-store", signal });
  if (res.status === 401) throw Object.assign(new Error("unauthorized"), { status: 401 });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.data || {};
}

// 把刚取到的子资源 slice 合并进 state.data，供 openSettings/render 直接复用。
function mergeStatusPart(kind, slice) {
  const d = state.data || {};
  if (kind === "accounts" || kind === "archived") {
    if (slice.accounts !== void 0) d.accounts = slice.accounts;
    if (slice.archived !== void 0) d.archived = slice.archived;
  } else if (kind === "schedules" && slice.schedules !== void 0) {
    d.schedules = slice.schedules;
  } else if (kind === "notifications" && slice.notifications !== void 0) {
    d.notifications = slice.notifications;
  } else if (kind === "recent" && slice.recent !== void 0) {
    d.recent = slice.recent;
  } else if ((kind === "admin" || kind === "adminNotifications") && slice.admin !== void 0) {
    d.admin = slice.admin;
  }
  state.data = d;
  state.lastStatusAt = Date.now();
}

// 按页面重画对应的隐藏面板承载区（accounts / adminNotifications 是 body 表达式，打开时内联渲染）。
function renderSettingsPanel(kind) {
  const d = state.data;
  if (!d) return;
  switch (kind) {
    case "schedules": renderSchedules(d.schedules || [], d); break;
    case "notifications": renderNotifications(d); break;
    case "recent": renderRecent(d.recent || []); break;
    case "archived": renderArchived(d.archived || []); break;
    case "admin": renderAdminUsers((d.admin || {}).users || []); break;
    default: break;
  }
}

// 静默拉取并合并某一个页面的子资源；有全量轮询在途时复用其结果。
async function refreshSettings(kind) {
  if (!state.authenticated) return false;
  if (state.inflight) {
    await state.inflight.catch(() => false);
    return true;
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), STATUS_TIMEOUT_MS);
  try {
    const slice = await fetchOneStatusPart(kind, controller.signal);
    mergeStatusPart(kind, slice);
    renderSettingsPanel(kind);
    return true;
  } catch (err) {
    if (err && err.status === 401) {
      setLocked(true, "请登录后查看监控台。");
      return false;
    }
    showServiceError("读取失败");
    showToast(err.name === "AbortError" ? "读取超时，请稍后重试" : `读取失败：${err.message}`);
    return false;
  } finally {
    window.clearTimeout(timer);
  }
}

async function openDrawerFresh(name, product) {
  // 抽屉正文只依赖 accounts（找账号）与 recent（历史/火花条），并行取这两片即可。
  await Promise.all([
    refreshSettings("accounts"),
    refreshSettings("recent"),
  ]);
  openDrawer(name, product);
}

async function openSettingsFresh(kind) {
  if ((kind === "admin" || kind === "adminNotifications") && state.data?.auth?.role !== "admin") {
    showToast("需要管理员权限");
    return;
  }
  await refreshSettings(kind);
  openSettings(kind);
}

function closeDrawer() {
  const drawer = $("#drawer");
  const mask = $("#drawerMask");
  window.clearTimeout(state.closeTimer);
  if (drawer) drawer.classList.remove("is-open");
  if (mask) mask.classList.remove("is-open");
  state.activeAccount = null;
  state.panel = "";
  if (!drawer || drawer.hidden) {
    if (drawer) drawer.hidden = true;
    if (mask) mask.hidden = true;
    return;
  }
  // 先播退场，落定后再置 hidden —— 否则是「啪」地消失
  state.closeTimer = window.setTimeout(() => {
    drawer.hidden = true;
    if (mask) mask.hidden = true;
  }, 340);
}

function refreshActiveAccount() {
  if (!state.activeAccount) return;
  const { name, product } = state.activeAccount;
  if (findAccount(name, product)) openDrawer(name, product);
}

/* 抽屉开着时把新数据搬回去 —— 否则触发签到跑完了，抽屉里还停在旧值，
   只能整页刷新才看得到，正是这次要修的问题。

   但有个例外：用户正在表单里填东西（选了文件、改了输入框）时不能重建，
   一重建就把填了一半的内容冲掉，比不刷新更糟。所以用 state.panelDirty 兜底：
   抽屉里发生过 input/change 就不动它；用户自己的操作（toggle/归档/保存）走
   force = true，那本来就该按新数据重画。 */
function refreshActivePanel({ force = false } = {}) {
  if (!force && state.panelDirty) return;
  if (state.panel === "account") {
    refreshActiveAccount();
    return;
  }
  if (["accounts", "schedules", "notifications", "recent", "archived", "admin", "adminNotifications"].includes(state.panel)) {
    openSettings(state.panel);
  }
}

function detailPair(label, value, title = "", mono = false, copyValue = "") {
  const cls = [mono ? "font-mono" : "", copyValue ? "has-copy" : ""].filter(Boolean).join(" ");
  return `
    <div class="detail-pair" title="${escapeAttr(title || value)}">
      <dt>${escapeHtml(label)}</dt>
      <dd class="${cls}">${escapeHtml(value || "—")}${copyValue ? `
        <button type="button" class="detail-copy" data-copy-value="${escapeAttr(copyValue)}"
                aria-label="复制${escapeAttr(label)}" title="复制">复制</button>` : ""}</dd>
    </div>
  `;
}

function detailSection(title, body, extraClass = "") {
  return `
    <section class="detail-section ${extraClass}">
      <h3>${escapeHtml(title)}</h3>
      ${body}
    </section>
  `;
}

function renderDrawerBody(item) {
  const today = todayState(item);
  const cred = credentialState(item);
  const disabled = item.enabled === false;
  // 只在日志里出现过的账号 credential_exists=false —— 没有文件可搬，
  // 启停和单独执行都必然失败，所以直接在界面上拦住，别让用户点了才知道。
  const hasCred = item.credential_exists !== false;
  const history = accountHistory(item, 5);
  const t = tone(today.severity);
  const c = tone(cred.severity);
  const isTrae = item.product === "TRAE";
  const displayName = String(item.display_name || item.name || "未命名账号");
  const credentialPath = item.credential_path || item.disabled_path || "";
  const logPath = item.log_path || "";
  const logFileName = pathFileName(logPath);
  const identity = accountIdentity(item);
  const recentRows = history.length ? history.map((row) => `
    <li class="detail-timeline-item">
      <span class="detail-timeline-dot ${tone(row.severity).dot}"></span>
      <div class="min-w-0">
        <div class="flex items-center justify-between gap-3">
          <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-semibold border ${sevPill(row.severity)}">${escapeHtml(row.label)}</span>
          <time class="text-[11px] text-slate-400 flex-none">${escapeHtml(formatDateTime(row.time))}</time>
        </div>
        ${row.note && row.note !== row.label ? `<p class="mt-1 text-xs text-slate-500 leading-relaxed">${escapeHtml(row.note)}</p>` : ""}
      </div>
    </li>
  `).join("") : `<li class="px-3 py-5 text-xs text-slate-500 text-center">还没有执行记录。</li>`;

  return `
    <section class="account-detail space-y-5">
      <div class="detail-hero">
        <div class="flex items-start justify-between gap-4">
          <div class="flex items-center gap-3 min-w-0">
            ${productIconHtml(item.product, "xl")}
            <div class="min-w-0">
              <p class="text-[11px] font-semibold text-slate-400 uppercase tracking-wide">${escapeHtml(item.product)}</p>
              <h3 class="mt-0.5 text-lg font-semibold text-slate-950 tracking-tight truncate">${escapeHtml(displayName)}</h3>
              <p class="mt-1 text-xs text-slate-500 truncate">账号 ${escapeHtml(item.name || "—")}</p>
            </div>
          </div>
          <span class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-[11px] font-semibold border flex-none ${t.pill}">
            <span class="w-1.5 h-1.5 rounded-full ${t.dot}"></span>${escapeHtml(today.label)}
          </span>
        </div>
        <div class="detail-hero-grid">
          <div>
            <span>今日</span>
            <b class="${t.text}">${escapeHtml(today.short)}</b>
          </div>
          <div>
            <span>凭据</span>
            <b class="${c.text}">${escapeHtml(cred.short)}</b>
          </div>
          <div>
            <span>最近</span>
            <b>${escapeHtml(today.time ? formatClock(today.time) : "—")}</b>
          </div>
          <div>
            <span>运行</span>
            <b>${escapeHtml(accountRuntimeLabel(item))}</b>
          </div>
        </div>
      </div>

      ${detailSection("状态概览", `
        <div class="detail-note ${today.severity}">
          <span class="w-1.5 h-1.5 rounded-full ${t.dot} flex-none mt-1.5"></span>
          <div class="min-w-0">
            <p class="font-medium text-slate-800">${escapeHtml(today.note)}</p>
            <p class="mt-1 text-[11px] text-slate-400">${escapeHtml(today.time ? `最近执行 ${formatDateTime(today.time)}` : "暂无执行记录")}</p>
          </div>
        </div>
        <dl class="detail-pair-grid mt-3">
          ${detailPair("今日状态", today.label)}
          ${detailPair("原始结果", item.latest?.result || "—", "最近一次日志中的 result", true)}
          ${detailPair("本次积分", item.latest?.credit != null ? `+${item.latest.credit}` : "—")}
          ${detailPair("连续天数", item.latest?.streak_days != null ? `${item.latest.streak_days} 天` : "—")}
        </dl>
      `)}

      ${detailSection("账号与凭据", `
        <dl class="detail-pair-grid">
          ${detailPair("显示名称", displayName)}
          ${detailPair("账号标识", item.name || "—", item.name || "", true)}
          ${detailPair("accountName", identity.name || "未记录", identity.name ? `accountName：${identity.name}` : "凭据里没有 accountName")}
          ${detailPair("账号 ID", identity.id || "未记录", identity.id ? `账号 ID：${identity.id}` : "凭据里没有账号 ID", true, identity.id)}
          ${detailPair("凭据文件", pathFileName(credentialPath) || "未发现", credentialPath || "未发现凭据文件", true)}
          ${detailPair("日志文件", logFileName || "暂无", logPath || "暂无日志文件", true)}
          ${detailPair("凭据到期", cred.detail)}
          ${detailPair("刷新有效期", credentialRefresh(item))}
          ${isTrae ? detailPair("设备 ID", item.ahaDeviceId ? compactMiddle(item.ahaDeviceId, 18) : "未记录", item.ahaDeviceId || "未记录", true) : ""}
        </dl>
      `)}

      ${detailSection("运行日志 .log 原始日志", `
        <div class="flex items-center justify-between gap-3">
          <div class="min-w-0">
            <p class="text-xs text-slate-500 leading-relaxed">读取当前账号关联的 .log 文件尾部记录。</p>
            <p class="mt-1 text-[11px] text-slate-400 font-mono truncate" title="${escapeAttr(logPath || "暂无日志文件")}">${escapeHtml(logPath || "暂无日志文件")}</p>
          </div>
          <button class="h-8 px-3 rounded-lg text-xs font-semibold border transition flex-none ${logPath ? "bg-white border-slate-200 text-slate-700 hover:border-blue-300 hover:text-blue-700" : "bg-slate-50 border-slate-200 text-slate-400 cursor-not-allowed"}"
                  type="button" data-account-log data-log-path="${escapeAttr(logPath)}"
                  ${logPath ? "" : `disabled aria-disabled="true" title="该账号暂无 .log 文件"`}>重新读取</button>
        </div>
        <div class="mt-3" data-account-log-panel data-log-path="${escapeAttr(logPath)}">
          <p class="text-xs text-slate-500 px-1 py-2">${logPath ? "正在读取 .log 文件…" : "该账号暂无 .log 文件。"}</p>
        </div>
      `, "detail-log-section")}

      ${detailSection("快捷操作", `
        <div class="detail-action-grid">
          <button class="h-9 px-3.5 rounded-lg text-sm font-medium border transition ${hasCred ? (disabled ? "bg-blue-50 text-blue-700 border-blue-200 hover:brightness-95" : "bg-amber-50 text-amber-700 border-amber-200 hover:brightness-95") : "bg-slate-50 text-slate-400 border-slate-200 cursor-not-allowed"}"
                  type="button" data-action="toggle-account" data-product="${escapeAttr(item.product)}"
                  data-name="${escapeAttr(item.name)}" data-next="${disabled ? "true" : "false"}"
                  ${hasCred ? "" : `disabled aria-disabled="true" title="该账号没有凭据文件，请先导入凭据"`}>
            ${disabled ? "启用该账号" : "停用该账号"}
          </button>
          <button class="h-9 px-3.5 rounded-lg text-sm font-medium border transition ${hasCred ? "bg-white border-slate-200 text-slate-700 hover:border-slate-300" : "bg-slate-50 border-slate-200 text-slate-400 cursor-not-allowed"}"
                  type="button" data-action="run-account" data-product="${escapeAttr(item.product)}"
                  data-name="${escapeAttr(item.name)}"
                  ${hasCred ? "" : `disabled aria-disabled="true" title="该账号没有凭据文件，无法单独执行"`}>只跑这个账号</button>
          <button class="h-9 px-3.5 rounded-lg text-sm font-medium bg-white border border-rose-200 text-rose-600 hover:bg-rose-50 transition"
                  type="button" data-action="archive-account" data-product="${escapeAttr(item.product)}"
                  data-name="${escapeAttr(item.name)}">归档</button>
          <button class="h-9 px-3.5 rounded-lg text-sm font-medium bg-rose-600 border border-rose-600 text-white hover:bg-rose-700 transition"
                  type="button" data-action="delete-account" data-product="${escapeAttr(item.product)}"
                  data-name="${escapeAttr(item.name)}">删除账号</button>
        </div>
        ${hasCred ? "" : `
          <p class="mt-2.5 flex items-start gap-1.5 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-2 leading-relaxed">
            <svg class="flex-none mt-0.5" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>
            </svg>
            <span>这个账号只在日志里出现过，<b class="font-semibold">没有凭据文件</b>，所以既不能启停也不能单独执行。要在界面上删掉这条记录，可以用「归档」。</span>
          </p>
        `}
      `)}

      ${detailSection("替换凭据", `
        <p class="text-xs text-slate-500 mb-3 leading-relaxed">凭据有新的导出文件时，在这里原地替换，账号名与配置保持不变。</p>
        <form class="flex flex-col gap-3" data-form="replace">
          <label class="block">
            <span class="block text-sm font-medium text-slate-700 mb-1.5">新的凭据 JSON</span>
            <input name="file" type="file" accept=".json,application/json" required
                   class="block w-full text-sm text-slate-600 file:mr-3 file:py-2 file:px-3.5 file:rounded-lg file:border-0 file:bg-slate-100 file:text-slate-700 file:text-sm file:font-medium hover:file:bg-slate-200">
          </label>
          <input type="hidden" name="product" value="${escapeAttr(item.product)}">
          <input type="hidden" name="account_name" value="${escapeAttr(item.name)}">
          <p class="form-status" data-form-status hidden></p>
          <button class="primary-btn self-start h-9 px-4 rounded-lg text-sm" type="submit">替换并保存</button>
        </form>
      `)}

      ${detailSection(`最近执行 · ${history.length || 0}`, `
        <ol class="detail-timeline">${recentRows}</ol>
      `)}
    </section>
  `;
}

function credentialRefresh(item) {
  if (item.refresh_days_left == null) return "未记录";
  const days = Math.round(item.refresh_days_left);
  const at = item.refresh_expires_at ? `（至 ${formatDateTime(item.refresh_expires_at)}）` : "";
  return `剩余 ${days} 天${at}`;
}

/* =========================================================================
   导入账号弹窗
   ====================================================================== */

function openUpload(product) {
  const modal = $("#uploadModal");
  if (!modal) return;
  const select = modal.querySelector("select[name='product']");
  if (select && product) select.value = product;
  window.clearTimeout(state.modalTimer);
  modal.hidden = false;
  nextFrame(() => modal.classList.add("is-open"));
  modal.querySelector("input[name='account_name']")?.focus();
}

function closeUpload() {
  const modal = $("#uploadModal");
  if (!modal) return;
  window.clearTimeout(state.modalTimer);
  modal.classList.remove("is-open");
  if (modal.hidden) return;
  state.modalTimer = window.setTimeout(() => { modal.hidden = true; }, 240);
}

/* =========================================================================
   时间计划 / 通知（Tailwind 版本）
   ====================================================================== */

function channelLabel(channel) {
  const labels = {
    serverchan: "Server酱",
    feishu: "飞书",
    wecom: "企业微信",
    bark: "Bark",
    webhook: "自定义 Webhook",
    none: "不推送",
  };
  return labels[String(channel || "").toLowerCase()] || channel || "未知渠道";
}

const MODE_LABEL = {
  silent: "静默签到",
  "silent-poll": "静默轮询",
  status: "只查状态",
  doctor: "体检",
};

// 抽屉里那条调度器状态：进程活着定时才在跑，所以这两件事必须一起看。
function schedulerBanner(data, items = []) {
  const active = !!data?.scheduler_active;
  const next = data?.next_run_at;
  const slots = data?.schedule_status || [];
  const fired = slots.filter((row) => row.fired).length;
  const enabled = (items || []).filter((item) => item.enabled).length;
  return `
    <section class="setting-summary">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="setting-live-dot ${active ? "is-on" : "is-off"}"></span>
            <h3>调度器${active ? "运行中" : "未运行"}</h3>
          </div>
          <p>${escapeHtml(next ? `下次执行 ${formatDateTime(next)}` : "暂无即将执行的计划")}</p>
        </div>
        <span class="setting-status-pill ${active ? "success" : "danger"}">${active ? "ACTIVE" : "OFFLINE"}</span>
      </div>
      <div class="setting-stat-grid">
        ${settingStat("启用产品", `${enabled}/${(items || []).length || 0}`, enabled ? "info" : "neutral")}
        ${settingStat("今日触发", `${fired}/${slots.length}`, fired ? "success" : "neutral")}
        ${settingStat("时间点", `${slots.length} 个`, "neutral")}
      </div>
    </section>`;
}

const FIELD_INPUT = "w-full h-10 px-3 bg-white border border-slate-200 rounded-lg text-sm focus:border-blue-400 focus:ring-4 focus:ring-blue-500/10 focus:outline-none";
const FIELD_LABEL = "block text-sm font-medium text-slate-700 mb-1.5";

function renderSchedules(items, data) {
  const host = $("#scheduleList");
  if (!host) return;
  const snapshot = data || state.data || {};
  const slots = snapshot.schedule_status || [];

  host.innerHTML = schedulerBanner(snapshot, items || []) + (items || []).map((item) => {
    const mine = slots.filter((row) => row.product === item.product);
    const done = mine.filter((row) => row.fired).length;
    const enabledTone = item.enabled ? "success" : "neutral";
    const pollTone = item.poll_enabled ? "info" : "neutral";
    return `
    <article class="setting-card schedule-card">
      <header class="setting-card-head">
        <div class="flex items-center gap-3 min-w-0">
          ${productIconHtml(item.product)}
          <div class="min-w-0">
            <h2>${escapeHtml(item.product)} 时间计划</h2>
            <p>${escapeHtml(item.description)} · ${escapeHtml(MODE_LABEL[item.daily_mode] || item.daily_mode || "静默签到")}</p>
          </div>
        </div>
        <span class="setting-status-pill ${item.enabled ? "success" : "neutral"}">${item.enabled ? "已启用" : "已暂停"}</span>
      </header>

      <div class="setting-stat-grid compact">
        ${settingStat("今日触发", `${done}/${mine.length}`, done ? "success" : "neutral")}
        ${settingStat("每日", item.daily_time || "未设置", enabledTone)}
        ${settingStat("轮询", item.poll_enabled ? `${(item.poll_times || []).length} 个` : "暂停", pollTone)}
      </div>

      <div class="schedule-strip">${scheduleSlotChips(mine)}</div>

      <form class="setting-form grid grid-cols-1 sm:grid-cols-2 gap-3" data-kind="schedule" data-product="${escapeAttr(item.product)}">
        <label class="setting-field">
          <span class="${FIELD_LABEL}">启用</span>
          <select name="enabled" class="${FIELD_INPUT}">
            <option value="true" ${item.enabled ? "selected" : ""}>启用</option>
            <option value="false" ${!item.enabled ? "selected" : ""}>暂停</option>
          </select>
        </label>
        <label class="setting-field">
          <span class="${FIELD_LABEL}">每日签到</span>
          <input name="daily_time" value="${escapeAttr(item.daily_time || "")}" placeholder="00:05" class="${FIELD_INPUT}">
        </label>
        <label class="setting-field">
          <span class="${FIELD_LABEL}">每日模式</span>
          <select name="daily_mode" class="${FIELD_INPUT}">
            ${["silent", "status"].map((value) => `<option value="${value}" ${(item.daily_mode || "silent") === value ? "selected" : ""}>${escapeHtml(MODE_LABEL[value] || value)}</option>`).join("")}
          </select>
        </label>
        <label class="setting-field">
          <span class="${FIELD_LABEL}">轮询补签</span>
          <select name="poll_enabled" class="${FIELD_INPUT}">
            <option value="true" ${item.poll_enabled ? "selected" : ""}>启用</option>
            <option value="false" ${!item.poll_enabled ? "selected" : ""}>暂停</option>
          </select>
        </label>
        <label class="setting-field">
          <span class="${FIELD_LABEL}">轮询模式</span>
          <select name="poll_mode" class="${FIELD_INPUT}">
            ${["silent-poll", "status"].map((value) => `<option value="${value}" ${item.poll_mode === value ? "selected" : ""}>${escapeHtml(MODE_LABEL[value] || value)}</option>`).join("")}
          </select>
        </label>
        <label class="setting-field">
          <span class="${FIELD_LABEL}">轮询时间</span>
          <input name="poll_times" value="${escapeAttr((item.poll_times || []).join(", "))}" placeholder="01:00, 05:00, 09:00" class="${FIELD_INPUT}">
        </label>
        <p class="setting-help sm:col-span-2">轮询时间用逗号分隔、24 小时制。重复执行会安静跳过已签到账号。</p>
        <p class="form-status sm:col-span-2" data-form-status hidden></p>
        <button class="primary-btn sm:col-span-2 h-10 rounded-lg text-sm" type="submit">保存时间计划</button>
      </form>
    </article>`;
  }).join("");
}

const ON_LABEL = {
  daily: "仅每日（推荐）",
  all: "每次执行都推",
  error: "仅异常时推",
};

const CLEAR_BTN = (field, shown) => shown
  ? `<button type="button" data-clear="${field}"
       class="flex-none h-10 px-3 rounded-lg border border-slate-200 text-xs text-slate-500 hover:text-rose-600 hover:border-rose-200 hover:bg-rose-50 transition">清除</button>`
  : "";

function productIconHtml(productKey, size = "lg") {
  const dim = {
    sm: "w-8 h-8 rounded-lg text-[11px]",
    md: "w-9 h-9 rounded-lg text-xs",
    lg: "w-10 h-10 rounded-xl text-sm",
    xl: "w-12 h-12 rounded-xl text-sm",
  }[size] || "w-10 h-10 rounded-xl text-sm";
  const icon = PRODUCT_ICON_SRC[productKey];
  if (icon) {
    return `<span class="product-icon ${dim}" title="${escapeAttr(productKey)}"><img src="${escapeAttr(icon)}" alt="" loading="lazy" decoding="async"></span>`;
  }
  const gradient = PRODUCT_AVATAR[productKey] || "from-slate-400 to-slate-600";
  return `<span class="${dim} grid place-items-center font-bold text-white flex-none bg-gradient-to-br ${gradient} shadow-sm shadow-slate-900/10">${escapeHtml(capital(productKey))}</span>`;
}

function settingStat(label, value, toneName = "neutral") {
  const palettes = {
    success: "bg-emerald-50 text-emerald-700 border-emerald-100",
    warning: "bg-amber-50 text-amber-700 border-amber-100",
    danger: "bg-rose-50 text-rose-700 border-rose-100",
    info: "bg-blue-50 text-blue-700 border-blue-100",
    neutral: "bg-slate-50 text-slate-700 border-slate-200",
  };
  return `
    <div class="setting-stat ${palettes[toneName] || palettes.neutral}">
      <span>${escapeHtml(label)}</span>
      <b>${escapeHtml(value)}</b>
    </div>
  `;
}

function scheduleSlotChips(rows) {
  if (!rows.length) return `<span class="setting-empty">暂无时间点</span>`;
  return rows.map((row) => {
    const done = row.fired ? "is-fired" : "";
    const label = row.kind === "daily" ? "每日" : "轮询";
    return `<span class="schedule-chip ${done}" title="${escapeAttr(`${row.label} · ${MODE_LABEL[row.mode] || row.mode || ""}`)}">${escapeHtml(row.clock)}<em>${escapeHtml(label)}</em></span>`;
  }).join("");
}

function renderNotifications(data) {
  const config = data.config?.notifications || {};
  const host = $("#notificationList");
  if (!host) return;
  const values = PRODUCTS.map((product) => ({ product, item: config[product.key] || {} }));
  const enabledCount = values.filter(({ item }) => item.enabled).length;
  const secretCount = values.filter(({ item }) => item.has_key || item.has_url).length;
  const channelCount = new Set(values.map(({ item }) => item.channel || "none")).size;

  host.innerHTML = `
    <section class="setting-summary">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="setting-live-dot ${enabledCount ? "is-on" : "is-off"}"></span>
            <h3>消息通知</h3>
          </div>
          <p>${enabledCount ? "签到结果会按策略推送到配置渠道" : "当前没有启用任何通知渠道"}</p>
        </div>
        <span class="setting-status-pill ${enabledCount ? "info" : "neutral"}">${enabledCount ? "PUSH ON" : "MUTED"}</span>
      </div>
      <div class="setting-stat-grid">
        ${settingStat("启用", `${enabledCount}/${PRODUCTS.length}`, enabledCount ? "info" : "neutral")}
        ${settingStat("凭据", `${secretCount}/${PRODUCTS.length}`, secretCount === PRODUCTS.length ? "success" : "warning")}
        ${settingStat("渠道", `${channelCount} 类`, "neutral")}
      </div>
    </section>
  ` + values.map(({ product, item }) => {
    const hint = item.has_key ? item.key : item.has_url ? item.url : "未配置凭据";
    const credentialLabel = item.has_key ? "Key 已保存" : item.has_url ? "URL 已保存" : "未配置凭据";
    const credentialTone = item.has_key || item.has_url ? "success" : "warning";
    return `
      <article class="setting-card notification-card">
        <header class="setting-card-head">
          <div class="flex items-center gap-3 min-w-0">
            ${productIconHtml(product.key)}
            <div class="min-w-0">
              <h2>${escapeHtml(product.key)} 通知</h2>
              <p>${escapeHtml(channelLabel(item.channel))} · ${escapeHtml(item.group || product.key)}</p>
            </div>
          </div>
          <span class="setting-status-pill ${item.enabled ? "info" : "neutral"}">${item.enabled ? "推送中" : "已关闭"}</span>
        </header>

        <div class="setting-stat-grid compact">
          ${settingStat("渠道", channelLabel(item.channel), item.channel === "none" ? "neutral" : "info")}
          ${settingStat("凭据", credentialLabel, credentialTone)}
          ${settingStat("策略", ON_LABEL[item.on] || item.on || "未设置", "neutral")}
        </div>

        <p class="setting-secret-hint" title="${escapeAttr(hint)}">${escapeHtml(hint)}</p>

        <form class="setting-form grid grid-cols-1 sm:grid-cols-2 gap-3" data-kind="notification" data-product="${escapeAttr(product.key)}">
          <label class="setting-field">
            <span class="${FIELD_LABEL}">启用</span>
            <select name="enabled" class="${FIELD_INPUT}">
              <option value="true" ${item.enabled ? "selected" : ""}>启用</option>
              <option value="false" ${!item.enabled ? "selected" : ""}>关闭</option>
            </select>
          </label>
          <label class="setting-field">
            <span class="${FIELD_LABEL}">渠道</span>
            <select name="channel" class="${FIELD_INPUT}">
              ${["serverchan", "feishu", "wecom", "bark", "webhook", "none"]
                .map((value) => `<option value="${value}" ${item.channel === value ? "selected" : ""}>${escapeHtml(channelLabel(value))}</option>`)
                .join("")}
            </select>
          </label>
          <div class="setting-field">
            <span class="${FIELD_LABEL}">Key</span>
            <div class="flex gap-2">
              <input name="key" type="password" placeholder="${escapeAttr(item.has_key ? "已保存，留空不改" : "SendKey / Bark Key")}" class="${FIELD_INPUT}">
              ${CLEAR_BTN("key", item.has_key)}
            </div>
          </div>
          <div class="setting-field">
            <span class="${FIELD_LABEL}">Webhook URL</span>
            <div class="flex gap-2">
              <input name="url" type="password" placeholder="${escapeAttr(item.has_url ? "已保存，留空不改" : "飞书/企微/Webhook 地址")}" class="${FIELD_INPUT}">
              ${CLEAR_BTN("url", item.has_url)}
            </div>
          </div>
          <label class="setting-field">
            <span class="${FIELD_LABEL}">推送策略</span>
            <select name="on" class="${FIELD_INPUT}">
              ${["daily", "all", "error"].map((value) => `<option value="${value}" ${item.on === value ? "selected" : ""}>${escapeHtml(ON_LABEL[value] || value)}</option>`).join("")}
            </select>
          </label>
          <label class="setting-field">
            <span class="${FIELD_LABEL}">来源分组</span>
            <input name="group" value="${escapeAttr(item.group || product.key)}" class="${FIELD_INPUT}">
          </label>
          <p class="form-status sm:col-span-2" data-form-status hidden></p>
          <div class="notification-actions sm:col-span-2">
            <button class="secondary-btn h-10 rounded-lg text-sm" type="button" data-test-notification>测试消息</button>
            <button class="primary-btn h-10 rounded-lg text-sm" type="submit">保存通知</button>
          </div>
        </form>
      </article>
    `;
  }).join("");
}

/* =========================================================================
   数据加载
   ====================================================================== */

/* 拉一次 /api/status。
   三个要点，都是为了「异步加载」这件事本身：
   1. 并发去重：定时轮询、盯变化轮询、手动点刷新经常撞在一起，
      以前会同时发两三个同样的请求。现在共用一个 in-flight Promise。
   2. 超时兜底：服务在但对端不回包时，没有超时的 fetch 会一直挂着，
      整个轮询循环就此停摆。15 秒拿不到就当失败，下一轮继续。
   3. 内容没变就不重画：每 20 秒把 #groups 的 innerHTML 重建一次，
      会打断 hover、丢掉横向滚动位置、重播进场动画 —— 纯属白干。
      指纹一致时只更新顶栏的「更新至」时间，DOM 一个字节都不动。
   返回 true 表示数据真的变了（调用方据此决定要不要刷新抽屉）。 */
// 用拆分后的 /api/status/* 子资源并行组装出完整 status payload，
// 供 render / dataSignature 复用原有结构；服务端有同一份缓存，整体开销小于单次聚合。
async function fetchStatusParts(role, signal) {
  const needsAdmin = role === "admin";
  const urls = [
    "/api/status/overview",
    "/api/status/accounts",
    "/api/status/recent?limit=80",
    "/api/status/schedules",
    "/api/status/notifications",
    "/api/status/tasks",
  ];
  if (needsAdmin) urls.push("/api/status/admin", "/api/status/stats");
  const results = await Promise.all(
    urls.map((url) => decryptedFetch(url, { cache: "no-store", signal }))
  );
  const unauthorized = results.find((r) => r.status === 401);
  if (unauthorized) throw Object.assign(new Error("unauthorized"), { status: 401 });
  const anyBad = results.find((r) => !r.ok);
  if (anyBad) throw new Error(`HTTP ${anyBad.status}`);
  const [ov, acc, rec, sch, notif, tasks] = results;
  const adminRes = needsAdmin ? results[6] : null;
  const statsRes = needsAdmin ? results[7] : null;
  return {
    generated_at: (ov.data || {}).generated_at,
    timezone: (ov.data || {}).timezone,
    summary: (ov.data || {}).summary,
    products: (ov.data || {}).products,
    scheduler_active: (ov.data || {}).scheduler_active,
    next_run_at: (ov.data || {}).next_run_at,
    schedule_status: (ov.data || {}).schedule_status,
    auth: (ov.data || {}).auth,
    accounts: (acc.data || {}).accounts,
    archived: (acc.data || {}).archived,
    recent: (rec.data || {}).recent,
    schedules: (sch.data || {}).schedules,
    notifications: (notif.data || {}).notifications,
    tasks: (tasks.data || {}).tasks,
    admin: needsAdmin ? (adminRes.data || null) : null,
    stats: needsAdmin ? (statsRes.data || null) : null,
  };
}

const STATUS_TIMEOUT_MS = 15000;

function loadStatus({ silent = false, force = false } = {}) {
  // 已经在飞了就直接复用，别再发一份
  if (state.inflight) {
    if (!silent) showToast("正在刷新…");
    return state.inflight;
  }
  const refresh = $("#refresh");
  if (refresh && !silent) refresh.classList.add("is-spinning");
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), STATUS_TIMEOUT_MS);

  const request = (async () => {
    try {
      let payload;
      try {
        payload = await fetchStatusParts(state.role, controller.signal);
      } catch (error) {
        if (error && error.status === 401) {
          setLocked(true, "请登录后查看监控台。");
          return false;
        }
        throw error;
      }

      state.lastStatusAt = Date.now();
      const next = dataSignature(payload);
      const changed = force || !state.data || next !== state.signature;
      if (changed) {
        render(payload);
      } else {
        // 内容没动：只把时间戳续上，DOM 保持原样（setLiveDot 恢复成正常色）
        state.data = payload;
        updateHeaderStamp(payload);
        setLiveDot((payload.summary?.attention || 0) ? "danger" : (payload.summary?.pending || 0) ? "warning" : "ok");
      }
      updateStats(payload); // 统计块单独刷新：不参与上面那个「内容没变就不重画」信号
      if (!silent) showToast(changed ? "已刷新" : "已是最新");
      return changed;
    } catch (error) {
      const timedOut = error.name === "AbortError";
      showServiceError(timedOut ? "服务响应超时" : "服务连接失败");
      setLiveDot("offline");
      if (!silent) showToast(timedOut ? "读取超时，请稍后重试" : `读取失败：${error.message}`);
      return false;
    } finally {
      window.clearTimeout(timer);
      if (refresh) window.setTimeout(() => refresh.classList.remove("is-spinning"), 420);
      state.inflight = null;
    }
  })();

  state.inflight = request;
  return request;
}

async function checkSession() {
  try {
    const res = await decryptedFetch("/api/auth/session", { cache: "no-store" });
    const data = res.data && typeof res.data === "object" ? res.data : {};
    if (data.authenticated) {
      unlock(data.username, data.role, data.status);
      await loadStatus({ silent: true });
      return;
    }
  } catch (error) {
    setLoginMessage("暂时无法连接服务，请稍后重试。", "danger");
    return;
  }
  setLocked(true);
}

/* =========================================================================
   交互
   ====================================================================== */

function setLoginButtonState(button, state) {
  if (!button) return;
  button.dataset.state = state;
  button.disabled = state !== "idle";
  const label = button.querySelector(".btn-label");
  const spinner = button.querySelector(".spinner");
  const check = button.querySelector(".check");
  if (label) label.hidden = state !== "idle";
  if (spinner) spinner.hidden = state !== "loading";
  if (check) check.hidden = state !== "success";
}

const wait = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

function setRunFeedback(button, state = "idle", label = "") {
  if (!button) return;
  if (!button.dataset.idleHtml) button.dataset.idleHtml = button.innerHTML;
  button.dataset.runState = state;
  button.disabled = state !== "idle";
  button.setAttribute("aria-busy", state === "loading" ? "true" : "false");
  if (state === "idle") {
    button.innerHTML = button.dataset.idleHtml;
    button.removeAttribute("data-run-state");
    button.removeAttribute("aria-busy");
    return;
  }
  const icon = state === "loading"
    ? `<span class="run-spinner" aria-hidden="true"></span>`
    : state === "danger"
      ? `<span class="run-feedback-icon" aria-hidden="true">!</span>`
      : state === "neutral"
        ? `<span class="run-feedback-icon" aria-hidden="true">i</span>`
        : `<span class="run-feedback-icon" aria-hidden="true">✓</span>`;
  button.innerHTML = `${icon}<span>${escapeHtml(label)}</span>`;
}

/* 兼容旧调用点：语义已从「等 1.8s / 5.6s 补拉两次」升级成「盯着数据变化轮询」。
   固定延迟那套的问题很实在 —— TRAE 单账号跑完要十几秒，两次都扑空，
   用户就只能整页刷新，正是这次要修的现象。 */
function scheduleStatusRefresh() {
  watchForUpdate();
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  const card = document.querySelector("[data-login-card]");
  const data = Object.fromEntries(new FormData(form).entries());
  setLoginButtonState(button, "loading");
  setLoginMessage("正在验证…", "muted");
  try {
    const res = await decryptedFetch("/api/auth/login", { method: "POST", json: data });
    const payload = res.data && typeof res.data === "object" ? res.data : {};
    if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);

    // 成功 → 按钮切到 success，绿光晕闪过，登录卡片淡出，再解锁工作台
    setLoginButtonState(button, "success");
    if (card) card.classList.add("is-success");
    await wait(520);
    if (card) card.classList.add("is-leaving");
    await wait(360);

    unlock(payload.username, payload.role, payload.status);
    form.reset();
    setLoginButtonState(button, "idle");
    await loadStatus({ silent: true });
    showToast("已登录");
  } catch (error) {
    const message = error.message || "登录失败";
    setLocked(true, message);
    setLoginMessage(message, "danger");
    showToast(message, "danger", { title: "登录失败" });
    setLoginButtonState(button, "idle");
  }
}

function showRegisterForm(show) {
  const loginForm = $("#loginForm");
  const registerForm = $("#registerForm");
  const showRegister = $("#showRegister");
  if (loginForm) loginForm.hidden = !!show;
  if (registerForm) registerForm.hidden = !show;
  if (showRegister) showRegister.hidden = !!show;
  resetPasswordToggles(document);
  setLoginMessage(show ? "注册后可直接登录；同一设备重复注册会转入人工审核。" : "账号与密码由管理员维护。");
  const target = show ? "#registerForm input[name='username']" : "#loginForm input[name='username']";
  window.setTimeout(() => $(target)?.focus(), 80);
}

async function register(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  const data = Object.fromEntries(new FormData(form).entries());
  data.fingerprint = await browserFingerprint();
  if (data.password !== data.confirm) {
    setLoginMessage("两次输入的密码不一致。", "danger");
    showToast("两次输入的密码不一致。", "warning", { title: "请检查密码" });
    return;
  }
  setButtonBusy(button, true, "提交中…");
  setLoginMessage("正在创建账号…", "muted");
  try {
    const res = await decryptedFetch("/api/auth/register", { method: "POST", json: data });
    const payload = res.data && typeof res.data === "object" ? res.data : {};
    if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
    const needsReview = !!payload.needs_review;
    form.reset();
    showRegisterForm(false);
    setLoginMessage(
      payload.message || (needsReview ? "注册已提交，请等待管理员审核。" : "注册成功，现在就可以登录。"),
      needsReview ? "muted" : "success"
    );
    showToast(
      needsReview ? "注册已提交，等待管理员审核" : "注册成功，请直接登录",
      needsReview ? "warning" : "success"
    );
  } catch (error) {
    setLoginMessage(error.message || "注册失败", "danger");
    showToast(`注册失败：${error.message}`);
  } finally {
    setButtonBusy(button, false);
  }
}

// 浏览器特征指纹：只作为**风控参考**上报，不参与服务端的账号唯一性判定。
// 服务端唯一的设备判据是从请求头自己算出来的指纹（见 server.py 的
// server_fingerprint）—— 客户端上报的值随时能改，一旦拿它去重就等于没防。
async function browserFingerprint() {
  const parts = [
    navigator.userAgent || "",
    navigator.language || "",
    navigator.languages ? navigator.languages.join(",") : "",
    navigator.platform || "",
    navigator.hardwareConcurrency || 0,
    navigator.maxTouchPoints || 0,
    screen.width + "x" + screen.height + "x" + screen.colorDepth,
    (screen.width * devicePixelRatio || 0),
    new Date().getTimezoneOffset(),
    Intl.DateTimeFormat ? Intl.DateTimeFormat().resolvedOptions().timeZone || "" : "",
    (navigator.plugins ? Array.from(navigator.plugins).map((p) => p.name).join(",") : ""),
  ];
  const source = parts.join("|");
  try {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(source));
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  } catch {
    // 个别环境(如部分 WebView)没有 crypto.subtle，退化成稳定前缀降级指纹
    let h = 0;
    for (let i = 0; i < source.length; i++) h = (Math.imul(31, h) + source.charCodeAt(i)) | 0;
    return "d32-" + (h >>> 0).toString(16);
  }
}

async function logout() {
  try {
    await apiPost("/api/auth/logout", {});
  } catch (error) {
    // 服务端失败也清本地登录态
  } finally {
    setLocked(true, "已退出。");
    window.setTimeout(() => window.location.reload(), 300);
  }
}

// account 为空 = 跑整个产品；非空 = 只跑这一个账号（后端会换成单账号命令）。
async function runNow(product, button, account = "") {
  if (!state.authenticated) {
    setLocked(true, "请先登录。");
    return;
  }
  setRunFeedback(button, "loading", account ? "执行中…" : "启动中…");
  showToast(account ? `正在启动「${account}」…` : "正在启动签到任务…");
  let restoreDelay = 1400;
  try {
    const payload = await apiPost("/api/run", {
      product: product || "all",
      mode: "silent",
      account: account || "",
    });
    const count = payload.started?.length || 0;
    if (count) {
      setRunFeedback(button, "success", "已启动");
      if (account) showToast(`已启动「${account}」的签到任务，稍后自动刷新`);
      else showToast(`已启动 ${count} 个签到任务，稍后自动刷新`);
      scheduleStatusRefresh();
      restoreDelay = 2200;
    } else {
      setRunFeedback(button, "neutral", "无账号");
      showToast("没有需要执行的账号");
      restoreDelay = 1600;
    }
  } catch (error) {
    setRunFeedback(button, "danger", "启动失败");
    showToast(`执行失败：${error.message}`);
    restoreDelay = 2200;
  } finally {
    window.setTimeout(() => setRunFeedback(button, "idle"), restoreDelay);
  }
}

async function uploadAccount(event) {
  event.preventDefault();
  if (!state.authenticated) {
    setLocked(true, "请先登录。");
    return;
  }
  const form = event.currentTarget;
  const button = form.querySelector("button[type='submit']");
  setFormStatus(form, "正在上传凭据…", "loading");
  setFormBusy(form, true, "上传中…");
  try {
    const payload = await apiPost("/api/accounts/upload", new FormData(form));
    if (payload.status) render(payload.status);
    setFormStatus(form, savedMessage("已导入"), "success");
    form.reset();
    closeUpload();
    showToast("账号已导入");
  } catch (error) {
    setFormStatus(form, `导入失败：${error.message}`, "danger");
    showToast(`导入失败：${error.message}`);
  } finally {
    setFormBusy(form, false);
  }
}

function setFormStatus(form, message = "", tone = "muted") {
  const node = form?.querySelector("[data-form-status]");
  if (!node) return;
  node.textContent = message;
  node.hidden = !message;
  node.dataset.tone = tone;
}

function setFormBusy(form, busy, busyText = "保存中…") {
  const button = form?.querySelector("button[type='submit']");
  if (!button) return;
  if (!button.dataset.idleText) button.dataset.idleText = button.textContent.trim();
  button.disabled = !!busy;
  button.textContent = busy ? busyText : button.dataset.idleText;
}

function setButtonBusy(button, busy, busyText = "处理中…") {
  if (!button) return;
  if (!button.dataset.idleText) button.dataset.idleText = button.textContent.trim();
  button.disabled = !!busy;
  button.textContent = busy ? busyText : button.dataset.idleText;
}

function drawerForm(kind, product) {
  return [...document.querySelectorAll("#drawerBody form[data-kind][data-product]")]
    .find((form) => form.dataset.kind === kind && form.dataset.product === product) || null;
}

function savedMessage(label = "已保存") {
  return `${label} · ${formatClock(new Date())}`;
}

function notificationFormPayload(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  data.product = form.dataset.product;
  data.enabled = data.enabled === "true";
  // 输入框留空代表「不改」；只有点过「清除」才带上显式清空标记。
  if (form.querySelector("[data-clear='key'][data-done='1']")) data.key_clear = true;
  if (form.querySelector("[data-clear='url'][data-done='1']")) data.url_clear = true;
  return data;
}

async function handleToggle(product, name, enabled) {
  try {
    const payload = await apiPost("/api/accounts/toggle", { product, name, enabled });
    if (payload.status) render(payload.status);
    if (state.activeAccount) openDrawer(name, product);
    showToast(enabled ? "账号已启用" : "账号已停用");
  } catch (error) {
    showToast(`操作失败：${error.message}`);
  }
}

async function handleArchive(product, name) {
  if (!window.confirm(`归档 ${product} / ${name}？\n\n归档后不再参与签到，凭据会被搬到 archive/ 目录。\n这不是删除 —— 顶栏「已归档」里随时可以恢复。`)) return;
  try {
    const payload = await apiPost("/api/accounts/archive", { product, name });
    if (payload.status) render(payload.status);
    closeDrawer();
    showToast(`${name} 已归档，顶栏「已归档」里可恢复`);
  } catch (error) {
    showToast(`操作失败：${error.message}`);
  }
}

async function handleDelete(product, name, button) {
  const ok = window.confirm(
    `永久删除 ${product} / ${name}？\n\n将删除这个账号的凭据文件和对应日志，删除后不会进入「已归档」，也不能在界面中恢复。`
  );
  if (!ok) return;
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "删除中…";
  }
  try {
    const payload = await apiPost("/api/accounts/delete", { product, name });
    if (payload.status) render(payload.status);
    closeDrawer();
    showToast(`${name} 已删除`);
  } catch (error) {
    showToast(`删除失败：${error.message}`);
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

/* 归档可逆 —— 这是「归档」二字能不能用的前提。 */
async function handleRestore(product, name, button) {
  const original = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "恢复中…";
  }
  try {
    const payload = await apiPost("/api/accounts/restore", { product, name });
    if (payload.status) {
      render(payload.status);
      // 从顶栏「已归档」抽屉里点的恢复：抽屉里那份是拷贝，不重搬就还挂着已恢复的账号
      refreshActivePanel({ force: true });
      // 恢复完最后一个是常态 —— 这时顶栏入口自己也消失了，抽屉留在那儿会没有出口
      if (!(payload.status.archived || []).length && state.panel === "archived") closeDrawer();
    }
    showToast(`${name} 已恢复到 ${product} 启用目录`);
  } catch (error) {
    showToast(`恢复失败：${error.message}`);
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

async function handleAdminAction(button) {
  const original = button.textContent;
  const action = button.dataset.adminAction;
  const userId = button.dataset.userId;
  if (action === "delete") {
    const name = button.dataset.userName || "该用户";
    const ok = window.confirm(
      `删除用户「${name}」？\n\n会永久删除，不可恢复：\n` +
      `· 登录账号与面板配置\n` +
      `· 工作空间 data/users/u${userId}/（已上传的凭据、日志、归档）\n` +
      `· 该用户的定时任务与今天的调度触发记录\n\n` +
      `若该用户正在签到，需等任务结束后才能删除。`
    );
    if (!ok) return;
  }
  button.disabled = true;
  button.textContent = "处理中…";
  try {
    const payload = await apiPost("/api/admin/users/update", { action, user_id: userId });
    if (payload.status) {
      render(payload.status);
      refreshActivePanel({ force: true });
    } else if (payload.users) {
      renderAdminUsers(payload.users);
      refreshActivePanel({ force: true });
    }
    if (action === "delete") {
      // 后端会把清理结果回报上来：工作区有没有真删掉、清了几条调度残留。
      // 这些信息不该只躺在服务端日志里 —— 管理员点一下就该知道地盘干净没。
      const cleanup = (payload.user && payload.user.cleanup) || {};
      if (cleanup.workspace_removed) {
        showToast(`用户已删除，工作空间 ${cleanup.workspace || ""} 已一并清理`);
      } else if (cleanup.workspace) {
        showToast(`用户已删除，但工作空间 ${cleanup.workspace} 未能删除，请查看服务端日志`);
      } else {
        showToast("用户已删除");
      }
    } else {
      showToast("用户状态已更新");
    }
  } catch (error) {
    button.disabled = false;
    button.textContent = original;
    showToast(`操作失败：${error.message}`);
  }
}

async function saveSchedule(event) {
  // 事件分发已在 document 的 submit 委托里按字段名完成，这里只认 data-product。
  // 不要再加类名判断：表单是动态渲染的，类名一变就会把「保存配置」静默拦死。
  const form = event.target.closest("[data-product]");
  if (!form) return;
  event.preventDefault();
  const data = Object.fromEntries(new FormData(form).entries());
  data.product = form.dataset.product;
  data.enabled = data.enabled === "true";
  data.poll_enabled = data.poll_enabled === "true";
  setFormStatus(form, "正在保存时间计划…", "loading");
  setFormBusy(form, true);
  try {
    const payload = await apiPost("/api/schedules", data);
    if (payload.status) render(payload.status);
    refreshActivePanel({ force: true });
    setFormStatus(drawerForm("schedule", data.product) || form, savedMessage("时间计划已保存"), "success");
    showToast("定时任务已保存");
  } catch (error) {
    setFormStatus(form, `保存失败：${error.message}`, "danger");
    showToast(`保存失败：${error.message}`);
  } finally {
    setFormBusy(form, false);
  }
}

async function saveNotification(event) {
  const form = event.target.closest("[data-product]");
  if (!form) return;
  event.preventDefault();
  const data = notificationFormPayload(form);
  setFormStatus(form, "正在保存通知配置…", "loading");
  setFormBusy(form, true);
  try {
    const payload = await apiPost("/api/notifications", data);
    if (payload.status) render(payload.status);
    refreshActivePanel({ force: true });
    setFormStatus(drawerForm("notification", data.product) || form, savedMessage("通知配置已保存"), "success");
    showToast("通知配置已保存");
  } catch (error) {
    setFormStatus(form, `保存失败：${error.message}`, "danger");
    showToast(`保存失败：${error.message}`);
  } finally {
    setFormBusy(form, false);
  }
}

async function testNotification(button) {
  const form = button.closest("form[data-kind='notification'][data-product]");
  if (!form) return;
  const data = notificationFormPayload(form);
  setFormStatus(form, "正在发送测试消息…", "loading");
  setButtonBusy(button, true, "发送中…");
  try {
    const payload = await apiPost("/api/notifications/test", data);
    if (payload.status) render(payload.status);
    refreshActivePanel({ force: true });
    const live = drawerForm("notification", data.product) || form;
    setFormStatus(live, savedMessage("测试消息已发送"), "success");
    showToast("测试消息已发送");
  } catch (error) {
    setFormStatus(form, `测试失败：${error.message}`, "danger");
    showToast(`测试失败：${error.message}`);
  } finally {
    if (button.isConnected) setButtonBusy(button, false);
  }
}

/* =========================================================================
   绑定
   ====================================================================== */

function on(selector, type, handler) {
  const node = $(selector);
  if (node) node.addEventListener(type, handler);
  return node;
}

on("#loginForm", "submit", login);
on("#registerForm", "submit", register);
on("#showRegister", "click", () => showRegisterForm(true));
on("#showLogin", "click", () => showRegisterForm(false));
on("#logout", "click", logout);
on("#refresh", "click", () => loadStatus({ force: true }));
on("#uploadForm", "submit", uploadAccount);
on("#uploadClose", "click", closeUpload);
on("#drawerClose", "click", closeDrawer);
on("#drawerMask", "click", closeDrawer);

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-toggle-password]");
  if (!button) return;
  event.preventDefault();
  togglePasswordVisibility(button);
});

/* 抽屉里只要有用户输入，静默刷新就别重建抽屉 —— 会把填了一半的内容冲掉。
   （用户自己的保存/启停等操作走 refreshActivePanel({force:true})，不受这个限制。） */
on("#drawerBody", "input", () => { state.panelDirty = true; });
on("#drawerBody", "change", () => { state.panelDirty = true; });

on("#openAccounts", "click", () => openSettingsFresh("accounts"));
on("#openSchedules", "click", () => openSettingsFresh("schedules"));
on("#openNotifications", "click", () => openSettingsFresh("notifications"));
on("#openRecent", "click", () => openSettingsFresh("recent"));
on("#openArchived", "click", () => openSettingsFresh("archived"));
on("#openAdmin", "click", () => openSettingsFresh("admin"));
on("#openAdminNotifications", "click", () => openSettingsFresh("adminNotifications"));
on("#openHelp", "click", () => openSettingsFresh("help"));

if (new URLSearchParams(window.location.search).has("toast")) {
  window.setTimeout(() => {
    showToast("通知组件已加载", "success");
    showToast("正在同步最新状态…", "loading", { duration: 4200 });
    showToast("账号需要管理员审核", "warning");
    showToast("这是一条失败提醒示例", "danger");
  }, 450);
}

// 委托：schedule-form 与 notify-form 都已通过 data-product 标识
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.id === "loginForm" || form.id === "registerForm" || form.id === "uploadForm") return;
  if (form.matches("[data-form='replace']")) return;
  if (!form.dataset.product) return;

  // 通过表单内容判断是 schedule 还是 notification
  if (form.querySelector("select[name='channel'], input[name='key']")) {
    saveNotification(event);
  } else if (form.querySelector("select[name='poll_enabled'], input[name='poll_times']")) {
    saveSchedule(event);
  }
});

// 抽屉里的按钮都在 document 级委托：
// openSettings 是把隐藏宿主的 innerHTML 复制进抽屉的，原节点上的监听不会跟过去。
document.addEventListener("click", (event) => {
  const copyBtn = event.target.closest("[data-copy-value]");
  if (copyBtn) {
    event.preventDefault();
    event.stopPropagation();
    copyText(copyBtn.dataset.copyValue || "")
      .then(() => showToast("已复制"))
      .catch(() => showToast("复制失败，请手动选择"));
    return;
  }
  const logBtn = event.target.closest("[data-log-toggle]");
  if (logBtn) {
    event.preventDefault();
    toggleRecentLog(logBtn);
    return;
  }
  const clearBtn = event.target.closest("[data-clear]");
  if (clearBtn) {
    event.preventDefault();
    markNotificationClear(clearBtn);
    return;
  }
  const testNotifyBtn = event.target.closest("[data-test-notification]");
  if (testNotifyBtn) {
    event.preventDefault();
    testNotification(testNotifyBtn);
    return;
  }
});

on("#groups", "click", (event) => {
  const runBtn = event.target.closest("button[data-run]");
  if (runBtn) {
    runNow(runBtn.dataset.run, runBtn);
    return;
  }
  const addBtn = event.target.closest("button[data-add]");
  if (addBtn) {
    openUpload(addBtn.dataset.add);
    return;
  }
  const card = event.target.closest("article[data-account]");
  if (card) openDrawerFresh(card.dataset.account, card.dataset.product);
});

on("#groups", "keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const card = event.target.closest("article[data-account]");
  if (!card) return;
  event.preventDefault();
  openDrawerFresh(card.dataset.account, card.dataset.product);
});

on("#drawerBody", "click", (event) => {
  const logReload = event.target.closest("button[data-account-log]");
  if (logReload) {
    event.preventDefault();
    if (logReload.disabled) return;
    const panel = logReload.closest(".detail-section")?.querySelector("[data-account-log-panel]");
    loadLogPanel(panel, logReload.dataset.logPath || "", { lines: 160, raw: true });
    return;
  }
  const addBtn = event.target.closest("button[data-add]");
  if (addBtn) {
    event.preventDefault();
    openUpload(addBtn.dataset.add);
    return;
  }
  const openAccountBtn = event.target.closest("button[data-open-account]");
  if (openAccountBtn) {
    event.preventDefault();
    openDrawerFresh(openAccountBtn.dataset.name, openAccountBtn.dataset.product);
    return;
  }
  const button = event.target.closest("button[data-action], button[data-admin-action]");
  if (button) {
    if (button.disabled) return;
    const { action, product, name, next } = button.dataset;
    if (button.dataset.adminAction) {
      handleAdminAction(button);
      return;
    }
    if (action === "toggle-account") {
      // data-next 已经是「点完应该变成什么」的目标值，直接采用。
      // 不要写成 data-enabled !== "true" 这类「当前状态取反」：data-enabled 存的
      // 本来就是目标值，取反会让每一下都把状态搬向反面。
      handleToggle(product, name, next === "true");
      return;
    }
    if (action === "archive-account") {
      handleArchive(product, name);
      return;
    }
    if (action === "delete-account") {
      handleDelete(product, name, button);
      return;
    }
    if (action === "run-account") {
      // 必须带上 name：后端按 (product, account) 定位，只传 product 会跑整个产品。
      runNow(product, button, name);
      return;
    }
    if (action === "restore-account") {
      // 顶栏「已归档」抽屉里的恢复按钮。抽屉内容是从宿主 innerHTML 拷过来的副本，
      // 所以只有挂在 #drawerBody 上的这个委托能收到点击 —— 挂在源节点上收不到。
      handleRestore(product, name, button);
      return;
    }
  }
  const managedRow = event.target.closest("[data-managed-account]");
  if (managedRow) {
    event.preventDefault();
    openDrawerFresh(managedRow.dataset.account, managedRow.dataset.product);
    return;
  }
  const accountCard = event.target.closest("article[data-account]");
  if (accountCard) {
    event.preventDefault();
    openDrawerFresh(accountCard.dataset.account, accountCard.dataset.product);
  }
});

on("#drawerBody", "keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  if (event.target.closest("button, input, select, textarea")) return;
  const row = event.target.closest("[data-managed-account]");
  if (!row) return;
  event.preventDefault();
  openDrawerFresh(row.dataset.account, row.dataset.product);
});

on("#archivedSection", "click", (event) => {
  const button = event.target.closest("button[data-action='restore-account']");
  if (!button || button.disabled) return;
  handleRestore(button.dataset.product, button.dataset.name, button);
});

on("#drawerBody", "submit", (event) => {
  const form = event.target.closest("form[data-form='replace']");
  if (!form) return;
  event.preventDefault();
  setFormStatus(form, "正在替换凭据…", "loading");
  setFormBusy(form, true, "替换中…");
  const name = form.querySelector("input[name='account_name']").value;
  const product = form.querySelector("input[name='product']").value;
  apiPost("/api/accounts/upload", new FormData(form))
    .then((payload) => {
      if (payload.status) render(payload.status);
      openDrawer(name, product);
      const live = document.querySelector("#drawerBody form[data-form='replace']");
      setFormStatus(live || form, savedMessage("凭据已替换"), "success");
      showToast("凭据已替换");
    })
    .catch((error) => {
      setFormStatus(form, `替换失败：${error.message}`, "danger");
      showToast(`替换失败：${error.message}`);
    })
    .finally(() => {
      setFormBusy(form, false);
      form.reset();
    });
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("#uploadModal")?.hidden) {
    closeUpload();
    return;
  }
  if (!$("#drawer")?.hidden) closeDrawer();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.authenticated) loadStatus({ silent: true });
});

checkSession();
