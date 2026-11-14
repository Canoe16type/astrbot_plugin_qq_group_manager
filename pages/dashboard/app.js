
// QQ官机群管 WebUI 前端
/* ---------------- 宿主 bridge（惰性解析，兼容晚注入） ----------------
   宿主页（AstrBot）会把 bridge 挂到 window.AstrBotPluginPage，注入时机可能晚于本模块执行。
   以前这里直接写 `const bridge = window.AstrBotPluginPage`，等于把加载那一刻的 undefined
   永久缓存下来——于是概览 / 记录 / 词典这些模块级代码全线报
   「加载失败：Cannot read properties of undefined (reading 'apiGet')」，
   而群规审核那段（自己有一份 bridge + 重试循环）却正常，看起来就像只有词典页坏了。
   所以改成：调用时再解析 + 启动时等一下注入，晚注入也能自愈。 */
let _bridge = null;
async function waitBridge(timeoutMs) {
  const limit = Number(timeoutMs) > 0 ? Number(timeoutMs) : 5000;
  const t0 = Date.now();
  let b = window.AstrBotPluginPage;
  while (!b && Date.now() - t0 < limit) {
    await new Promise((r) => setTimeout(r, 100));
    b = window.AstrBotPluginPage;
  }
  _bridge = b || null;
  return _bridge;
}
function bridgeNow() {
  const b = _bridge || window.AstrBotPluginPage;
  if (!b) throw new Error("面板 bridge 未就绪（宿主页还没注入 AstrBotPluginPage），刷新页面即可重试");
  return b;
}
const bridge = new Proxy({}, {
  get(_t, prop) {
    const b = bridgeNow();
    const v = b[prop];
    return typeof v === "function" ? v.bind(b) : v;
  },
});

/* ---------------- 时间口径：跟 AstrBot 全局 timezone 一致 ---------------- */
// 面板显示的后端配置时长一律是「秒」；这里只做展示换算，判定逻辑不碰。
let TZ = "Asia/Shanghai";
function tzLabel(name){
  try{
    const parts = new Intl.DateTimeFormat("en-US",{timeZone:name,timeZoneName:"shortOffset"})
      .formatToParts(new Date()).find(x=>x.type==="timeZoneName");
    return (parts && parts.value) ? parts.value.replace("GMT","UTC") : name;
  }catch(e){ return name; }
}
function applyTimeZone(name){
  const t = String(name || "").trim();
  if(t){
    try{ new Intl.DateTimeFormat("zh-CN",{timeZone:t}); TZ = t; }catch(e){ /* 非法时区名回退默认 */ }
  }
  const chip = $("tzChip");
  if(chip) chip.textContent = tzLabel(TZ);
  // 只更新时区两个值，整段文案是静态中文，不必重写 innerHTML
  const cur = $("tzCur"), lbl = $("tzLbl");
  if(cur) cur.textContent = TZ;
  if(lbl) lbl.textContent = tzLabel(TZ);
}
const _dtf = () => new Intl.DateTimeFormat("zh-CN",{timeZone:TZ,year:"numeric",month:"2-digit",day:"2-digit",
  hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});

const $ = (id) => document.getElementById(id);

// ---------------- 主题（自动跟随系统/AstrBot + 手动切换） ----------------
const THEME_KEY = "qqgm-theme";

// iframe 沙箱（无 allow-same-origin）下访问 localStorage 会抛 SecurityError，
// 这里统一捕获降级，避免 init 流程中断。
function getLS(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function setLS(key, val) {
  try { localStorage.setItem(key, val); } catch { /* 沙箱下忽略 */ }
}

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  const btn = $("themeToggle");
  if (btn) {
    // 新版按钮内部是 svg 图标（由 [data-theme] 的 only-light / only-dark 控制显隐），
    // 老版没有图标时才用 emoji 占位，避免把图标覆盖掉。
    if (!btn.querySelector("svg")) btn.textContent = t === "dark" ? "☀️" : "🌙";
    btn.title = t === "dark" ? "切换到浅色主题" : "切换到深色主题";
  }
}
function detectParentTheme() {
  try {
    const p = window.parent && window.parent.document;
    if (!p) return null;
    const dt = p.documentElement && p.documentElement.dataset.theme;
    if (dt) return dt;
    const html = p.querySelector("html");
    if (html && html.dataset.theme) return html.dataset.theme;
  } catch (e) { /* 跨域忽略 */ }
  return null;
}
function initTheme() {
  const saved = getLS(THEME_KEY);
  if (saved === "dark" || saved === "light") { applyTheme(saved); return; }
  const parent = detectParentTheme();
  if (parent === "dark" || parent === "light") { applyTheme(parent); return; }
  applyTheme(window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  if (!getLS(THEME_KEY)) applyTheme(e.matches ? "dark" : "light");
});
document.addEventListener("DOMContentLoaded", () => {
  const btn = $("themeToggle");
  if (btn) {
    btn.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      setLS(THEME_KEY, next);
    });
  }
});

/* ---------------- 开关升级：原生 checkbox → 轨道开关 ----------------
   只加视觉层，不碰 DOM 结构与事件：
   1) input 本体保留（id 不变，$("s-xxx").checked 与 change 监听照旧可用）；
   2) 在 input 后面插一个空 <i class="tsw">，外观由 CSS 的 :checked + .tsw 驱动；
   3) 幂等：重复调用不会重复插入；动态渲染出的新开关由 MutationObserver 兜住。 */
function upgradeChecks(root) {
  const scope = root || document;
  scope.querySelectorAll("label.field.check").forEach((lab) => {
    const input = lab.querySelector('input[type="checkbox"]');
    if (!input || lab.querySelector(":scope > .tsw")) return;
    const track = document.createElement("i");
    track.className = "tsw";
    track.setAttribute("aria-hidden", "true");
    input.after(track);
  });
}
document.addEventListener("DOMContentLoaded", () => upgradeChecks());
if (typeof MutationObserver === "function") {
  let pending = false;
  const mo = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; upgradeChecks(); });
  });
  document.addEventListener("DOMContentLoaded", () => {
    mo.observe(document.body, { childList: true, subtree: true });
  });
}


const state = {
  status: null,
  lastGroups: [],      // 最近一次接口返回的群列表（分群下拉兜底用）
  logs: [],            // 全部日志（前端过滤显示）
  sseId: null,
  sseConnected: false,
  sseConnecting: false,  // 正在建立连接（防重入）
  sseRetry: 0,           // 连续失败次数，用于指数退避
  sseGen: 0,             // 连接代次：旧连接的回调靠它作废，避免把 sseId 又写回来
  sseTimer: null,        // 重连定时器（去重，避免并发重连）
};

// ---------------- 工具 ----------------
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- 空状态 / 加载中占位 ----------------
   以前只有一个灰圆点（.empty::before），看着像「图标没加载出来」。
   现在统一「软圆底 + sprite 线性图标 + 文案」，图标沿用页面 sprite 里的 #i-*。 */
const EMPTY_ICONS = {
  load: "i-refresh", warn: "i-warn", ok: "i-ok", door: "i-door", mute: "i-mute",
  records: "i-records", recall: "i-recall", logs: "i-logs", groups: "i-groups",
  robot: "i-robot", settings: "i-settings", shield: "i-shield", search: "i-search",
  book: "i-book", review: "i-review", overview: "i-overview",
};
function emptyBox(icon, text) {
  const id = EMPTY_ICONS[icon] || icon || "i-overview";
  const spin = id === "i-refresh" ? " spin" : "";   // 加载中：刷新图标转起来
  return `<div class="empty"><svg class="ico eico${spin}" aria-hidden="true"><use href="#${id}"/></svg>` +
         `<span>${text}</span></div>`;
}

// 时间一律按 AstrBot 配置时区（默认 Asia/Shanghai / UTC+8）渲染，不跟随浏览器时区
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return String(ts ?? "");
  return _dtf().format(d).replace(/\//g, "-");
}

// 剩余时长（比 fmtDur 更适合倒计时：不会出现「1400秒」这种）
function fmtLeft(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600),
    m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
  if (d) return `${d}天${h}小时`;
  if (h) return `${h}小时${m}分`;
  if (m) return `${m}分${s2}秒`;
  return `${s2}秒`;
}

// 禁言到期：官方接口给的是 RFC3339，直接显示太难读 → 「剩余 xx（至 本地时间）」
function fmtMuteExpire(iso) {
  const t = Date.parse(iso);
  if (!t) return String(iso || "").trim() || "?";
  const ts = Math.floor(t / 1000);
  const left = ts - Math.floor(Date.now() / 1000);
  const abs = fmtTime(ts).slice(0, 16); // 2026-10-02 00:51
  if (left <= 0) return `已到期（${abs}），可点刷新`;
  return `剩余 ${fmtLeft(left)}（至 ${abs}）`;
}

// ---------------- 自定义弹窗 / Toast（iframe 沙箱内 confirm/alert 不可用） ----------------
let _modalMask = null;
function ensureModal() {
  if (_modalMask) return _modalMask;
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `
    <div class="modal-box">
      <div class="modal-title">⚠️ 操作确认</div>
      <div class="modal-msg"></div>
      <div class="modal-btns">
        <button class="btn" data-act="cancel">取消</button>
        <button class="btn danger" data-act="ok">确定</button>
      </div>
    </div>`;
  document.body.appendChild(mask);
  _modalMask = mask;
  return mask;
}
function showConfirm(msg) {
  return new Promise((resolve) => {
    const modal = ensureModal();
    modal.querySelector(".modal-msg").textContent = msg;
    modal.classList.add("show");
    const okBtn = modal.querySelector('[data-act="ok"]');
    const cancelBtn = modal.querySelector('[data-act="cancel"]');
    const done = (val) => {
      modal.classList.remove("show");
      okBtn.onclick = null;
      cancelBtn.onclick = null;
      modal.onclick = null;
      resolve(val);
    };
    okBtn.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
    modal.onclick = (e) => { if (e.target === modal) done(false); };
    okBtn.focus();
  });
}
/* 文本输入弹窗：原生 prompt() 在沙箱 iframe 里被浏览器直接拦掉（点了像没反应），
   所以一切「让用户输一行字」都必须走这个。返回 string；取消返回 null。 */
function showPrompt(opts) {
  const o = typeof opts === "string" ? { msg: opts } : (opts || {});
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "modal-mask show";
    mask.innerHTML = `
      <div class="modal-box">
        <div class="modal-title">${esc(o.title || "请输入")}</div>
        <div class="modal-msg">${esc(o.msg || "")}</div>
        <input class="input modal-input" type="text" maxlength="${Number(o.maxlength) || 200}"
               placeholder="${esc(o.placeholder || "")}" value="${esc(o.value || "")}" />
        <div class="modal-btns">
          <button class="btn" data-act="cancel">取消</button>
          <button class="btn primary" data-act="ok">确定</button>
        </div>
      </div>`;
    document.body.appendChild(mask);
    const input = mask.querySelector(".modal-input");
    const done = (val) => {
      mask.remove();
      resolve(val);
    };
    mask.querySelector('[data-act="ok"]').onclick = () => done(input.value);
    mask.querySelector('[data-act="cancel"]').onclick = () => done(null);
    mask.onclick = (e) => { if (e.target === mask) done(null); };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); done(input.value); }
      else if (e.key === "Escape") { e.preventDefault(); done(null); }
    });
    setTimeout(() => { input.focus(); input.select(); }, 30);
  });
}

function showToast(msg) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 2600);
}

// ---------------- Tab 切换（一级页签，切页即按需拉数据） ----------------
const TAB_LOADERS = {
  status: () => loadStatus(),
  ops: () => { fillOpsGroupSelect(); loadRequests(); loadMutes(); },
  moderation: () => { if (window.__qqgmModerationLoad) window.__qqgmModerationLoad(); },
  members: () => { if (window.__qqgmMembersLoad) window.__qqgmMembersLoad(); },
  lexicons: () => loadLexicons(),
  records: () => { loadHistory(); loadRecalls(); if (window.__qqgmRecordsLoad) window.__qqgmRecordsLoad(); },
  logs: () => loadLogHistory(),
  settings: () => loadSettings(),
  approval: () => loadApprovalStrategies(),
};

/* ---------------- 顶部标题（跟页签联动，页面里不再重复写 h2） ---------------- */
const TAB_HEAD = {
  status: ["概览", "机器人平台、功能开关与全局设置一览"],
  settings: ["插件设置", "表单由 _conf_schema.json 自动生成，与 AstrBot 配置面板读写同一份配置"],
  ops: ["群管", "入群申请与当前禁言状态一览；同意 / 拒绝请在群内用管理员指令处理"],
  moderation: ["群规审核", "左侧选群，右侧按「群规 / 举报设置 / 话术模板」分栏配置，按群独立保存"],
  members: ["成员档案", "openid ↔ 昵称/备注 对照表；官方事件只给 openid，名字靠这里维护，退群通知也从这里取名"],
  lexicons: ["词典", "关键词维度用的词库：内置开源词库 + 本地自传词库，改动立即生效"],
  records: ["记录", "禁言历史、撤回历史与模型判定记录；试跑用当前选中群的提示词"],
  logs: ["日志", "实时日志流（SSE），可按等级与关键词过滤"],
  approval: ["入群审批", "官方「入群自动审批策略」：白名单号码申请入群自动通过，按机器人维度管理"],
};
function syncTop(name) {
  const m = TAB_HEAD[name];
  if (!m) return;
  const titleEl = $("topTitle"), subEl = $("topSub");
  if (titleEl) titleEl.textContent = m[0];
  if (subEl) subEl.textContent = m[1];
}
function switchTab(name) {
  const t = document.querySelector('.tab[data-tab="' + name + '"]');
  if (t) t.click();
}
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("tab-" + btn.dataset.tab).classList.add("active");
    syncTop(btn.dataset.tab);
    const load = TAB_LOADERS[btn.dataset.tab];
    if (load) load();
  });
});

// ---------------- 「记录」页内的子页签 ----------------
document.querySelectorAll(".rec-tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".rec-tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".rec-pane").forEach((pn) => pn.classList.remove("active"));
    btn.classList.add("active");
    const pane = $("rec-pane-" + btn.dataset.recTab);
    if (pane) pane.classList.add("active");
  });
});
// 判定记录里的「切换群 →」：直接跳到「群规审核」选群
const _scopeGo = $("btnRecScopeGo");
if (_scopeGo) _scopeGo.onclick = () => {
  const t = document.querySelector('.tab[data-tab="moderation"]');
  if (t) t.click();
};
// 反向快捷（与上面那个对称）：群规审核页头的「📋 判定记录」→ 记录页的判定记录子页签
const _goVerdicts = $("btn-go-verdicts");
if (_goVerdicts) _goVerdicts.onclick = () => {
  const tab = document.querySelector('.tab[data-tab="records"]');
  if (tab) tab.click();
  const sub = document.querySelector('.rec-tab[data-rec-tab="verdicts"]');
  if (sub) sub.click();
  // 记录页按「当前选中群」取数；从上个群切过来时补一次刷新，免得看到旧群数据
  setTimeout(() => { try { loadRecords(); } catch (e) {} }, 0);
};

// ---------------- 状态 ----------------
async function loadStatus() {
  try {
    state.status = await bridge.apiGet("status");
  } catch (e) {
    $("platformList").innerHTML = emptyBox("warn", "状态加载失败: " + esc(e.message));
    return;
  }
  const s = state.status;
  // 渲染阶段的异常也要兜住：否则任何一个卡片渲染出错都会静默卡在「加载中」
  try {
  const pv = $("pluginVersion");
  if (pv && s.version) pv.textContent = s.version;
  $("pollStatus").textContent = s.poll_running ? "轮询运行中" : "轮询未启动";
  $("pollStatus").style.color = s.poll_running ? "var(--green)" : "var(--orange)";

  const plats = s.platforms || [];
  const groups = s.known_groups || [];
  const f = s.features || {};
  const FEATURE_KEYS = ["enable_mute", "enable_approval", "enable_join_request_notify", "enable_welcome", "enable_new_group_welcome"];
  const onCount = FEATURE_KEYS.filter((k) => f[k]).length;

  // Hero 状态条 + KPI 共用的申请通道信息（必须在 if 外声明，KPI 也要用）
  const ap = s.approval || {};
  const jmode = ap.join_event_mode || "interval";
  const btnOn = !!(ap.buttons && ap.buttons.enabled && ap.buttons.channel_ready);
  const jmodeLabel = { event_first: "事件为主", interval: "纯轮询", off: "仅事件" }[jmode] || "纯轮询";
  const online = jmode === "off" ? true : !!s.poll_running;

  const hs = $("heroState");
  if (hs) {
    hs.textContent = jmode === "off"
      ? "● 仅事件通道"
      : (s.poll_running ? (jmode === "event_first" ? "● 事件 + 轮询兜底" : "● 轮询运行中") : "● 轮询未启动");
    hs.className = "hero-badge " + (online ? "on" : "off");
    $("heroDesc").textContent = `${plats.length} 个机器人平台 · ${groups.length} 个已知群 · ${s.pending_count || 0} 个待处理申请 · 审批按钮${btnOn ? "已启用" : "未启用"}`;
  }

  // KPI 指标
  const kpi = $("kpiRow");
  if (kpi) {
    kpi.innerHTML = `
      <div class="kpi"><div class="kpi-val">${groups.length}</div><div class="kpi-label">已知群</div></div>
      <div class="kpi ${(s.pending_count || 0) > 0 ? "o" : "g"}"><div class="kpi-val">${s.pending_count || 0}</div><div class="kpi-label">待处理入群申请</div></div>
      <div class="kpi p"><div class="kpi-val">${onCount}<small>/ ${FEATURE_KEYS.length}</small></div><div class="kpi-label">功能开关已开启</div></div>
      <div class="kpi g"><div class="kpi-val">${jmodeLabel}</div><div class="kpi-label">${jmode === "off" ? "申请获取方式（不轮询）" : `申请获取方式 · 轮询 ${(ap.poll_interval_effective ?? s.poll_interval) || "—"} 秒`}</div></div>`;
  }

  // 平台
  $("platformList").innerHTML = plats.length
    ? plats.map((p) => `
      <div class="kv-item">
        <span class="k">机器人</span>
        <span class="v">${esc(p.name || p.id)}${p.name ? `（平台 ID：${esc(p.id)}）` : ""}</span>
        <span class="k" style="margin-top:6px">AppID（动态获取）</span>
        <span class="v mono">${esc(p.appid_masked || p.appid)}</span>
        <span class="k" style="margin-top:6px">状态</span>
        <span class="v" style="color:${p.enable ? "var(--green)" : "var(--text-dim)"}">${p.enable ? "已启用" : "未启用"}</span>
      </div>`).join("")
    : emptyBox("robot", "未找到启用的 QQ 官方机器人平台");

  // 功能开关
  /* 概览的功能开关：key = 后端 features 字段，group/schemaKey = 点进去要定位到设置页哪一项 */
  const fmap = [
    ["enable_mute",                 "禁言指令",              "basic",           "enable_mute"],
    ["enable_welcome",              "成员入群欢迎",           "basic",           "enable_welcome"],
    ["enable_group_add_intro",      "机器人入群自我介绍",      "basic",           "enable_group_add_intro"],
    ["enable_new_group_welcome",    "机器人入群·首条兜底",     "basic",           "enable_new_group_welcome"],
    ["enable_review",               "群规审核",              "moderation",      "enable"],
    ["enable_approval",             "入群审批",              "approval",        "enable_approval"],
    ["enable_join_request_notify",  "新申请群内通知",         "approval",        "enable_join_request_notify"],
    ["enable_recall",               "撤回指令",              "recall",          "enable_recall"],
    ["enable_member_remove_notify", "退群通知",              "member_event",    "enable_member_remove_notify"],
    ["enable_member_registry",      "成员档案",              "member_registry", "enabled"],
  ];
  const fl = $("featureList");
  fl.innerHTML = fmap.map(([k, name, group, skey]) => `
    <button type="button" class="feature-item clickable" data-group="${group}" data-skey="${skey}"
            title="点击跳到「设置 · ${name}」">
      <span class="${f[k] ? "on" : "off"}">${f[k] ? "● 开启" : "○ 关闭"}</span>
      <span class="name">${name}</span>
    </button>`).join("");
  fl.querySelectorAll("[data-skey]").forEach((b) => {
    b.addEventListener("click", () => {
      switchTabSafe("settings");
      focusSetting(b.dataset.skey, b.dataset.group);
    });
  });

  // 管理员判定
  const a = s.admin || {};
  $("adminInfo").innerHTML = `
    <div class="kv-item"><span class="k">判定模式</span><span class="v">${a.mode === "all" ? "全部条件满足" : "任一条件满足"}</span></div>
    <div class="kv-item"><span class="k">AstrBot 管理员名单</span><span class="v">${a.use_astrbot_admin ? "启用" : "关闭"}</span></div>
    <div class="kv-item"><span class="k">QQ 群角色(群主/管理员)</span><span class="v">${a.use_qq_role ? "启用" : "关闭"}</span></div>
    <div class="kv-item"><span class="k">额外管理员 OpenID</span><span class="v mono">${(a.extra_openids || []).join("、") || "无"}</span></div>`;

  // 已知群（表格）
  const gbox = $("groupList");
  gbox.innerHTML = groups.length
    ? `<div class="group-table-wrap"><table class="group-table">
        <thead><tr><th style="width:34%">群名</th><th style="width:30%">群 OpenID</th><th>所属机器人</th><th style="text-align:right;width:14%">操作</th></tr></thead>
        <tbody>${groups.map((g) => `
          <tr>
            <td><span class="gname">${esc(g.alias || g.name || "未命名群")}</span>${g.alias ? '<span class="gtag">备注</span>' : ""}</td>
            <td class="mono" style="font-size:12px;color:var(--text-dim)">${esc(g.group_openid)}</td>
            <td>${esc(g.bot || g.platform_id)}</td>
            <td style="text-align:right"><button class="btn ghost small" data-manage="${esc(g.group_openid)}">📋 群规</button></td>
          </tr>`).join("")}</tbody>
      </table></div>`
    : emptyBox("robot", "暂无记录。机器人第一次在群里互动后会自动记录");
  // 一键跳到「群规审核」并选中该群
  gbox.querySelectorAll("[data-manage]").forEach((b) => {
    b.addEventListener("click", () => {
      const t = document.querySelector('.tab[data-tab="moderation"]');
      if (t) t.click();
      if (window.__qqgmSelectGroup) window.__qqgmSelectGroup(b.dataset.manage);
    });
  });

  // 待审批徽标
  const badge = $("reqBadge");
  const n = s.pending_count || 0;
  if (n > 0) { badge.textContent = n; badge.classList.remove("hidden"); }
  else { badge.classList.add("hidden"); }

  // 记录 tab 的群筛选下拉（撤回历史那份以前漏了填充）
  fillHistoryGroupSelect();
  fillRecallGroupSelect();
  } catch (e) {
    console.error("[qqgm] status render failed", e);
    ["platformList", "featureList", "adminInfo", "groupList"].forEach((id) => {
      const el = $(id);
      if (el) el.innerHTML = emptyBox("warn", "状态渲染失败: " + esc(e.message) + "（接口数据正常，属前端 bug，请反馈）");
    });
    const k = $("kpiRow");
    if (k) k.innerHTML = "";
  }
}

$("btnRefreshStatus").addEventListener("click", loadStatus);
if ($("btnGoSettings")) $("btnGoSettings").addEventListener("click", () => {
  const t = document.querySelector('.tab[data-tab="settings"]');
  if (t) t.click();
});
if ($("btnGoMod")) $("btnGoMod").addEventListener("click", () => {
  const t = document.querySelector('.tab[data-tab="moderation"]');
  if (t) t.click();
});
$("btnPollNow").addEventListener("click", async () => {
  const btn = $("btnPollNow");
  btn.disabled = true; btn.textContent = "轮询中…";
  try {
    const r = await bridge.apiPost("poll");
    showToast(`轮询完成: ${Object.entries(r.groups || {}).map(([g, s]) => `${g} → ${s}`).join("；") || "(无可管理群)"}`);
  } catch (e) {
    showToast(`轮询失败: ${e.message}`);
  } finally {
    btn.disabled = false; btn.textContent = "🔄 立即轮询一次";
  }
});

// ---------------- 待审批 ----------------
async function loadRequests() {
  const box = $("requestList");
  box.innerHTML = emptyBox("load", "加载中…");
  try {
    const g = $("opsGroup") ? $("opsGroup").value : "all";
    const params = {};
    if (g && g !== "all") params.group_openid = g;
    const r = await bridge.apiGet("requests", params);
    rememberGroups(r.groups);
    fillGroupSelect("opsGroup", "全部群（汇总）");
    const list = r.requests || [];
    const all = (r.scope || (g && g !== "all" ? "group" : "all")) === "all";
    if (all) {
      const badge = $("reqBadge");
      const n = list.length;
      if (n) { badge.textContent = n; badge.classList.remove("hidden"); }
      else { badge.classList.add("hidden"); }
    }
    box.innerHTML = (
      (r.errors || []).map((er) => `<div class="row-item"><div class="left"><span class="hint">⚠️ ${esc(er.group_display || "")}（${esc(er.bot || "")}）查询失败，已跳过：${esc(er.error || "")}</span></div></div>`).join("")
      + (list.length
        ? list.map((it) => `
        <div class="row-item">
          <div class="left">
            <span class="seq">${it.seq}</span>
            ${all ? groupTagOf(it) : ""}
            <span><b>${esc(it.username || it.member_openid)}</b></span>
            <span class="tag">${it.apply_source === "invited" ? "被邀请" : "主动申请"}</span>
            ${it.verify_info && it.verify_info.verify_message ? `<span class="tag">验证: ${esc(it.verify_info.verify_message)}</span>` : ""}
          </div>
          <div class="sub">${esc(it.apply_at)}</div>
        </div>`).join("")
        : emptyBox("ok", all ? "当前没有待处理的入群申请 ✅" : "这个群当前没有待处理的入群申请 ✅"))
    );
  } catch (e) {
    box.innerHTML = emptyBox("warn", `加载失败: ${esc(e.message)}`);
  }
}
$("btnRefreshRequests").addEventListener("click", loadRequests);

// ---------------- 禁言 ----------------
async function loadMutes() {
  const box = $("muteList");
  box.innerHTML = emptyBox("load", "加载中…");
  try {
    const g = $("opsGroup") ? $("opsGroup").value : "all";
    const params = {};
    if (g && g !== "all") params.group_openid = g;
    const r = await bridge.apiGet("mutes", params);
    rememberGroups(r.groups);
    fillGroupSelect("opsGroup", "全部群（汇总）");
    const members = r.members || [];
    const all = (r.scope || (g && g !== "all" ? "group" : "all")) === "all";
    let html = "";
    // 单群模式：显示该群的全员禁言状态；汇总模式：逐群列出
    const rule = r.global_rule || {};
    if (!all && rule.mode && rule.mode !== "none") {
      html += muteRuleRow("", rule);
    }
    (r.group_rules || []).forEach((gr) => {
      html += muteRuleRow(groupTagOf(gr), gr);
    });
    (r.errors || []).forEach((er) => {
      html += `<div class="row-item"><span class="hint">${esc(er.group_display || "")} 查询失败：${esc(er.error || "")}</span></div>`;
    });
    html += members.length
      ? members.map((m) => `
        <div class="row-item">
          <div class="left">
            ${all ? groupTagOf(m) : ""}
            <span>🔇 <b>${esc(m.username || m.member_openid)}</b></span>
            ${m.member_openid ? `<span class="sub mono">${esc(m.member_openid)}</span>` : ""}
          </div>
          <div class="sub">到期: ${esc(fmtMuteExpire(m.mute_expire_at))}</div>
        </div>`).join("")
      : emptyBox("ok", all ? "当前没有成员被禁言 ✅" : "这个群当前没有成员被禁言 ✅");
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = emptyBox("warn", `加载失败: ${esc(e.message)}`);
  }
}
$("btnRefreshMutes").addEventListener("click", loadMutes);
$("opsGroup") && $("opsGroup").addEventListener("change", () => {
  const sel = $("opsGroup");
  const hint = $("opsScopeHint");
  if (hint) hint.textContent = sel.value === "all" ? "汇总所有已知群，逐群查询实时状态" : "";
  loadRequests(); loadMutes();
});

// ---------------- 禁言历史 ----------------
/* 群标签：同一群在不同机器人下是两回事，所以「群名 · 机器人」一起显示 */
function groupLabelOf(g) {
  if (!g) return "";
  const nm = g.display || g.name || g.alias || String(g.group_openid || "").slice(0, 10);
  return g.bot ? `${nm} · ${g.bot}` : nm;
}
function groupLabelByOpenid(openid) {
  const groups = (state.status && state.status.known_groups) || [];
  const hit = groups.find((x) => x.group_openid === openid);
  return hit ? groupLabelOf(hit) : (openid ? openid.slice(0, 8) + "…" : "");
}
function groupTag(openid) {
  return groupTagLabel(groupLabelByOpenid(openid));
}
function groupTagLabel(label) {
  return label ? `<span class="tag">群 ${esc(label)}</span>` : "";
}
// 群管列表（申请 / 禁言）的行自带 group_display，直接用它，
// 免得「概览」还没加载完就先点进群管时只能看到 openid 前缀。
function groupTagOf(item) {
  if (!item) return "";
  return groupTagLabel(item.group_display || groupLabelByOpenid(item.group_openid));
}

// ---- 全员禁言：mode 只是「配置过哪种类型」，还要看规则有没有启用 ----
const WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
// hideDisabled：整条规则都没生效时，行上已经打了「规则已停用」，就不再逐条重复
function muteWindowText(w, hideDisabled) {
  if (!w) return "";
  const days = (w.weekdays || []).filter((d) => d >= 1 && d <= 7);
  const dayText = days.length === 7 ? "每天" : (days.map((d) => WEEKDAY_CN[d - 1]).join("、") || "未指定星期");
  const when = w.kind === "recurring"
    ? `${dayText} ${w.start && w.end ? `${w.start}→${w.end}` : "时段未配置"}`
    : `${w.start_at ? fmtMuteAbs(w.start_at) : "?"} → ${w.end_at ? fmtMuteAbs(w.end_at) : "?"}`;
  if (!w.enabled) return `${when}${hideDisabled ? "" : "（已停用）"}`;
  if (w.kind === "recurring") return when;
  // 单次时段标出「已过期 / 未开始 / 进行中」——过期时段不该看着像还在禁言；
  // 时间残缺/解析不出来时不硬猜，直接不标状态
  const now = Date.now();
  const ts = Date.parse(w.start_at || "");
  const te = Date.parse(w.end_at || "");
  const st = te && te < now ? "已过期" : (ts && ts > now ? "未开始" : (ts && te ? "进行中" : ""));
  return st ? `${when}（${st}）` : when;
}
function fmtMuteAbs(iso) {
  const t = Date.parse(iso);
  if (!t) return String(iso || "").trim() || "?";
  return fmtTime(Math.floor(t / 1000)).slice(0, 16);
}
// 返回「全员禁言模式」那一行的 HTML。tag 传空字符串即「不显示群标签」（单群模式）
function muteRuleRow(tag, rule) {
  const mode = String((rule && rule.mode) || "").toLowerCase();
  const windows = (rule && rule.windows) || [];
  const active = !!(rule && rule.active);
  let text = "未知";
  let cls = "";
  if (mode === "always") {
    text = "始终禁言 · 生效中";
    cls = "bad";
  } else if (mode === "schedule") {
    // active＝规则已启用；live＝此刻真的落在某个时段里。两者分开说，避免「已过期」被当成「正在禁言」
    const live = rule.live === true;
    text = active ? (live ? "定时禁言 · 生效中" : "定时禁言 · 已启用") : "定时禁言 · 未生效";
    cls = active ? (live ? "warn" : "") : "";
  } else {
    text = mode;
  }
  let note = "";
  if (mode === "schedule" && !active) {
    note = windows.length ? "规则已停用" : "无生效时段";
  } else if (mode === "schedule" && rule.live === false) {
    note = "当前不在时段内";
  }
  const muteWholeOff = mode === "schedule" && !active && windows.length > 0;
  const spans = windows.map((w) => muteWindowText(w, muteWholeOff)).filter(Boolean);
  return `<div class="row-item"><div class="left">${tag}` +
    `<span>全员禁言模式: <b>${esc(text)}</b></span>` +
    (note ? `<span class="tag ${cls}">${esc(note)}</span>` : "") +
    (spans.length ? `<span class="sub">${esc(spans.join("；"))}</span>` : "") +
    `</div></div>`;
}

// 任意接口只要带了 groups，就记一份，供下拉兜底（status 未就绪时不至于空着）
function rememberGroups(list) {
  if (Array.isArray(list) && list.length) state.lastGroups = list;
  return state.lastGroups || [];
}
function knownGroups() {
  const st = (state.status && state.status.known_groups) || [];
  return st.length ? st : (state.lastGroups || []);
}
function fillGroupSelect(id, allLabel, groupList) {
  const sel = $(id);
  if (!sel) return;
  const groups = (groupList && groupList.length) ? groupList : knownGroups();
  const cur = sel.value;
  sel.innerHTML = `<option value="all">${esc(allLabel || "全部群")}</option>` +   // 兜底：群列表来自 status 或最近一次接口返回
    groups.map((g) => `<option value="${esc(g.group_openid)}">${esc(groupLabelOf(g))}</option>`).join("");
  // 原来默认选中项可能已经不存在（群被改过名 / 已删除），回落到「全部群」
  sel.value = Array.from(sel.options).some((o) => o.value === cur) ? cur : "all";
}

function fillHistoryGroupSelect() { fillGroupSelect("histGroup", "全部群"); }
function fillRecallGroupSelect() { fillGroupSelect("recallGroup", "全部群"); }
function fillOpsGroupSelect() { fillGroupSelect("opsGroup", "全部群（汇总）"); }
$("histGroup").addEventListener("change", loadHistory);

async function loadHistory() {
  const box = $("historyList");
  box.innerHTML = emptyBox("load", "加载中…");
  try {
    const params = { limit: 100 };
    const g = $("histGroup").value;
    if (g && g !== "all") params.group_openid = g;
    const r = await bridge.apiGet("history", params);
    const list = r.records || [];
    box.innerHTML = list.length
      ? list.map((it) => {
          const stMap = { active: ["禁言中", "on"], unmuted: ["已解禁", "off"], expired: ["已到期", "off"] };
          const st = stMap[it.status] || [it.status || "未知", "off"];
          const op = it.operator_name || (it.operator || "").slice(0, 8);
          return `
        <div class="row-item">
          <div class="left">
            <span class="seq">#${it.id}</span>
            ${($("histGroup").value === "all") ? groupTag(it.group_openid) : ""}
            <span class="tag">${esc(it.mute_at_text)}</span>
            <span><b>${esc(it.member_openid.slice(0, 8))}</b></span>
            <span class="tag">时长 ${esc(it.duration_text || it.seconds + "秒")}</span>
            <span class="tag">理由: ${esc(it.reason || "（无理由）")}</span>
            <span class="feature-item" style="display:inline-flex;margin-left:4px"><span class="${st[1]}">${st[0]}</span></span>
            <span class="tag">到期 ${esc(it.expire_at_text)}</span>
          </div>
          <div class="sub">
            <span style="margin-right:8px">操作: ${esc(op)}</span>
            <button class="btn danger small" data-del="${it.id}" title="删除这条记录">✕</button>
          </div>
        </div>`;
        }).join("")
      : emptyBox("records", "暂无禁言历史记录（执行 /群管 禁言 后会自动记录）");
    // 删除按钮
    box.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!(await showConfirm(`确认删除禁言记录 #${btn.dataset.del}？`))) return;
        try {
          await bridge.apiPost("history/delete", { id: Number(btn.dataset.del) });
          loadHistory();
        } catch (e) {
          showToast(`删除失败: ${e.message}`);
        }
      });
    });
  } catch (e) {
    box.innerHTML = emptyBox("warn", "加载失败: " + esc(e.message));
  }
}
$("btnRefreshHistory").addEventListener("click", loadHistory);
$("btnHistoryClear").addEventListener("click", async () => {
  const g = $("histGroup").value;
  const scope = g ? "当前选中的群" : "全部群";
  if (!(await showConfirm(`确认清空${scope}的禁言历史？此操作不可恢复！`))) return;
  try {
    const r = await bridge.apiPost("history/clear", g ? { group_openid: g } : {});
    showToast(`已清空 ${r.cleared} 条记录`);
    loadHistory();
  } catch (e) {
    showToast(`清空失败: ${e.message}`);
  }
  });

// ==================== 撤回历史 ====================
// 下拉填充统一走上面的 fillGroupSelect()（群名 · 机器人）
$("recallGroup").addEventListener("change", loadRecalls);

async function loadRecalls() {
  const box = $("recallList");
  box.innerHTML = emptyBox("load", "加载中…");
  try {
    const params = { limit: 100 };
    const g = $("recallGroup").value;
    if (g && g !== "all") params.group_openid = g;
    const r = await bridge.apiGet("recalls", params);
    const list = r.records || [];
    box.innerHTML = list.length
      ? list.map((it) => {
          const op = it.operator_name || (it.operator || "").slice(0, 8);
          const src = it.source === "quote" ? "引用撤回" : "消息ID撤回";
          const content = it.content ? esc(it.content) : `<span class="hint">（无原文，仅保留消息 ID）</span>`;
          return `
        <div class="row-item">
          <div class="left">
            <span class="seq">#${it.id}</span>
            ${($("recallGroup").value === "all") ? groupTag(it.group_openid) : ""}
            <span class="tag">${esc(it.recalled_at_text)}</span>
            <span class="tag">${src}</span>
            <span class="tag mono">${esc(it.message_id)}</span>
          </div>
          <div class="sub">
            <div class="recall-content">${content}</div>
            <div style="margin-top:4px">
              <span style="margin-right:8px">操作: ${esc(op)}</span>
              <button class="btn danger small" data-del="${it.id}" title="删除这条记录">✕</button>
            </div>
          </div>
        </div>`;
        }).join("")
      : emptyBox("recall", "暂无撤回记录（引用消息后发送 /群管 撤回 会自动留存原文）");
    // 删除按钮
    box.querySelectorAll("[data-del]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!(await showConfirm(`确认删除撤回记录 #${btn.dataset.del}？`))) return;
        try {
          await bridge.apiPost("recalls/delete", { id: Number(btn.dataset.del) });
          loadRecalls();
        } catch (e) {
          showToast(`删除失败: ${e.message}`);
        }
      });
    });
  } catch (e) {
    box.innerHTML = emptyBox("warn", "加载失败: " + esc(e.message));
  }
}
$("btnRefreshRecalls").addEventListener("click", loadRecalls);
$("btnRecallsClear").addEventListener("click", async () => {
  const g = $("recallGroup").value;
  const scope = g ? "当前选中的群" : "全部群";
  if (!(await showConfirm(`确认清空${scope}的撤回历史？此操作不可恢复！`))) return;
  try {
    const r = await bridge.apiPost("recalls/clear", g ? { group_openid: g } : {});
    showToast(`已清空 ${r.cleared} 条记录`);
    loadRecalls();
  } catch (e) {
    showToast(`清空失败: ${e.message}`);
  }
});

// ---------------- 日志 ----------------
function logMatches(entry) {
  const lv = $("logLevel").value;
  const kw = $("logKeyword").value.trim().toLowerCase();
  if (lv !== "ALL" && entry.level !== lv) return false;
  if (kw && !entry.msg.toLowerCase().includes(kw)) return false;
  return true;
}

function renderLogs() {
  const box = $("logList");
  const items = state.logs.filter(logMatches);
  box.innerHTML = items.length
    ? items.map((e) => `
      <div class="log-line"><span class="lt">${esc(fmtTime(e.ts))}</span><span class="lv lv-${e.level}">${e.level}</span><span class="msg">${esc(e.msg)}</span></div>`).join("")
    : emptyBox("search", "暂无匹配日志");
  if ($("logAutoScroll").checked) {
    box.scrollTop = box.scrollHeight;
  }
}

function fmtBytes(b) {
  const n = Number(b) || 0;
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
}

async function loadLogHistory() {
  try {
    const r = await bridge.apiGet("logs", { limit: 200 });
    state.logs = (r.logs || []).slice().reverse(); // 后端倒序，这里转正序
    renderLogs();
    const hint = $("logFileHint");
    if (hint) {
      const kd = Number(r.keep_days || 0);
      hint.textContent =
        `｜磁盘 ${r.file_count || 0} 个文件 · ${fmtBytes(r.file_bytes)} · ` +
        (kd > 0 ? `自动清理超过 ${kd} 天的文件` : "磁盘日志永久保留（可在设置页改）") +
        ` · 内存缓冲 ${r.buffer_entries || 0}/${r.buffer_max || 0} 条 · ${fmtBytes(r.buffer_bytes)}（检索走磁盘）`;
    }
  } catch (e) {
    $("logList").innerHTML = emptyBox("warn", "日志加载失败: " + esc(e.message));
  }
}

function connectSSE() {
  // 注意：state.sseId 只代表「已成功建立的连接」。断线时必须清掉它，
  // 否则这个守卫会让所有重连调用直接 return——面板就永久停在「重连中…」。
  if (state.sseId || state.sseConnecting) return;
  state.sseConnecting = true;
  const gen = state.sseGen;
  bridge.subscribeSSE("logs/stream", {
    onOpen() {
      if (gen !== state.sseGen) return;
      const wasRetry = state.sseRetry > 0;
      state.sseConnected = true;
      state.sseRetry = 0;
      $("sseState").textContent = "实时流: 已连接";
      $("sseState").style.color = "var(--green)";
      // 断线期间后端不会补发，重连成功后重新拉一次历史把缺口补上
      if (wasRetry) loadLogHistory();
    },
    onMessage(event) {
      let data = event.parsed || event.raw;
      if (typeof data === "string") {
        try { data = JSON.parse(data); } catch { return; }
      }
      if (data && typeof data.index !== "undefined") {
        state.logs.push(data);
        if (state.logs.length > 3000) state.logs.splice(0, state.logs.length - 3000);
        renderLogs();
      }
    },
    onError() {
      if (gen !== state.sseGen) return;   // 旧连接的错误，忽略
      state.sseConnected = false;
      state.sseId = null;                 // 关键：允许重连
      state.sseConnecting = false;
      state.sseGen += 1;                  // 让本次连接后续的 then 回调失效
      scheduleSSEReconnect();
    },
  }).then((id) => {
    if (gen !== state.sseGen) return;
    state.sseId = id;
    state.sseConnecting = false;
  }).catch(() => {
    if (gen !== state.sseGen) return;
    state.sseConnecting = false;
    scheduleSSEReconnect();
  });
}

function scheduleSSEReconnect() {
  if (state.sseTimer) return;            // 已经排好队了，别重复排
  const delay = Math.min(15000, 1000 * Math.pow(2, Math.min(state.sseRetry, 4)));
  state.sseRetry += 1;
  $("sseState").textContent = `实时流: 断开，${Math.round(delay / 1000)} 秒后重连…`;
  $("sseState").style.color = "var(--orange)";
  state.sseTimer = setTimeout(() => {
    state.sseTimer = null;
    state.sseId = null;
    state.sseConnecting = false;
    connectSSE();
  }, delay);
}

$("logLevel").addEventListener("change", renderLogs);
$("logKeyword").addEventListener("input", renderLogs);
$("btnLogClear").addEventListener("click", async () => {
  if (!(await showConfirm("清空日志？内存日志与磁盘日志文件都会被删除（此操作不可恢复）。"))) return;
  try {
    const r = await bridge.apiPost("logs/clear");
    state.logs = [];
    renderLogs();
    showToast(`已清空内存 ${r.cleared || 0} 条，磁盘文件已删除`);
    loadLogHistory();
  } catch (e) {
    showToast(`清空失败: ${e.message}`);
  }
});

/* ================= 群规审核（原独立 page 的内容，已并入本页 tab） ================= */
(function () {
// 群规审核 · 插件 Page
// 通过 window.AstrBotPluginPage bridge 调用后端（路由前缀 mod/...）。
let bridge = window.AstrBotPluginPage;

const TPL_FIELDS = [
  ["mod_hit", "命中违规（群内回复）"],
  ["mod_clean", "未违规（群内回复）"],
  ["mod_hit_general", "综合判定违规（未对应具体规则，可留空）"],
  ["mod_receiving", "举报受理回执"],
  ["mod_failed", "审核失败"],
  ["mod_no_quote", "未引用消息就举报"],
  ["mod_disabled", "本群未开启审核"],
  ["mod_dry_run", "试运行提示"],
  ["mod_need_admin", "非管理员改群规"],
  ["mod_rules_empty", "本群尚无群规"],
  ["mod_report_limit", "举报过于频繁"],
  ["mod_layer_off", "模型层已关闭（未做语义判定）"],
  ["mod_uncertain", "把握不足未自动处理（只记录）"],
  ["mod_quote_lost", "引用消息失联（找不到被举报消息）"],
  ["mod_exempt", "被举报人处于豁免名单"],
  ["mod_variant_recalled", "变体疑似（已自动撤回）"],
  ["mod_variant_logged", "变体疑似（仅记录，未撤回）"],
  ["mod_variant_strike_notify", "变体累计达阈值：@ 管理员提醒"],
  ["mod_variant_strike_muted", "变体累计达阈值：已自动禁言"],
  ["mod_admin_registered", "管理员登记成功（变体提醒名单）"],
  ["mod_admin_unregistered", "管理员取消登记"],
  ["mod_admin_not_registered", "取消登记时本来就不在名单"],
  ["msg_mod_warn", "警告提示（智能分级：轻微/中度）"],
  ["msg_mod_todo", "转人工复核回执（高危但置信度不足）"],
  ["msg_mod_revoke", "撤销处罚并致歉"],
  ["mod_admin_list", "查看提醒名单"],
  ["mod_admin_list_empty", "提醒名单为空时的提示"],
  ["mod_admin_register_off", "登记成功但本群未开提醒"],
  ["mod_todo_muted", "待办人工处理后的回执"],
];
const DIM_LABELS = { keyword: "关键词", rule: "群规", semantic: "语义分析", vision: "图片识别" };
const CAT_LABELS = { ad: "广告引流", abuse: "人身攻击", porn: "色情低俗", illegal: "违法违规", spam: "刷屏灌水", other: "其他违规" };
const PROVIDER_SOURCE_LABEL = { config: "配置指定", session: "群内当前对话模型", default: "全局默认模型", fallback: "兜底模型", none: "无可用模型" };
const SEVERITIES = [["low", "低"], ["medium", "中"], ["high", "高"]];

let bots = [];
let current = "";
let detail = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg, isErr, ms) {
  const t = $("modToast");
  t.textContent = msg;
  t.style.background = isErr ? "var(--danger)" : "var(--text)";
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), ms || 2200);
}

function fmtDur(sec) {
  sec = parseInt(sec || 0, 10);
  if (!sec) return "0";
  if (sec % 86400 === 0) return sec / 86400 + "天";
  if (sec % 3600 === 0) return sec / 3600 + "小时";
  if (sec % 60 === 0) return sec / 60 + "分钟";
  return sec + "秒";
}

function parseDur(text) {
  const s = String(text || "").trim();
  if (!s) return 3600;
  const m = s.match(/^(\d+(?:\.\d+)?)\s*(秒|s|分|分钟|m|小时|时|h|天|d)?$/i);
  if (!m) return 3600;
  const n = parseFloat(m[1]);
  const u = (m[2] || "秒").toLowerCase();
  if (/^(分|分钟|m)$/.test(u)) return Math.round(n * 60);
  if (/^(小时|时|h)$/.test(u)) return Math.round(n * 3600);
  if (/^(天|d)$/.test(u)) return Math.round(n * 86400);
  return Math.round(n);
}

function ts2str(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ---------------- 群列表 ---------------- */
// 群名不再自动静默拉取：打开面板只读本地已存的名字，
// 需要刷新时手动点「↻ 补群名」或在群里发 /群管 群名（管理员）。

async function loadGroups() {
  const res = await bridge.apiGet("mod/groups");
  bots = res.bots || [];
  const flat = res.items || [];
  if (!bots.length && flat.length) {
    bots = [{ platform_id: "", display: "未知机器人", appid_masked: "", groups: flat }];
  }
  renderGroups();
}

function findGroup(g) {
  for (const b of bots) for (const x of (b.groups || [])) if (x.group === g) return x;
  return null;
}

function botTitle(b) {
  const parts = [];
  if (b.name && b.platform_id && b.name !== b.platform_id) parts.push(`${b.name}（${b.platform_id}）`);
  else parts.push(b.name || b.platform_id || "未知机器人");
  if (b.appid_masked) parts.push(`AppID ${b.appid_masked}`);
  if (b.enable === false) parts.push("未启用");
  return parts.join(" · ");
}

function groupLabel(g) {
  const tail = String(g.group || "").slice(-6);
  // 后端一般会给 display(=备注名或群名)，这里再兜一层 alias/name，避免只拿到原始字段时显示成「未命名群」
  return g.display || g.alias || g.name || `未命名群 ${tail}`;
}

function renderGroups() {
  const kw = ($("search").value || "").trim().toUpperCase();
  const ul = $("group-list");
  ul.innerHTML = "";
  let shown = 0;
  (bots || []).forEach((b) => {
    const list = (b.groups || []).filter((g) =>
      !kw || groupLabel(g).toUpperCase().includes(kw) || String(g.group || "").toUpperCase().includes(kw));
    if (!list.length) return;
    const head = document.createElement("li");
    head.className = "bot-head";
    head.title = "点一下：给这个机器人的群补拉群名（只拉还没名字的）";
    head.innerHTML =
      `<span class="bot-ico">🤖</span>` +
      `<span class="bot-name">${esc(botTitle(b))}</span>` +
      `<span class="meta">${list.length} 群</span>`;
    head.onclick = () => refreshNames(b.platform_id ? { platform_id: b.platform_id } : { all: true });
    ul.appendChild(head);
    list.forEach((g) => {
      const li = document.createElement("li");
      if (g.group === current) li.className = "active";
      const dotCls = !g.enabled ? "" : g.dry_run ? "dry" : "on";
      const nameCls = g.alias ? "gname alias" : (g.name ? "gname" : "gname unnamed");
      li.innerHTML =
        `<span class="dot ${dotCls}" title="${g.enabled ? (g.dry_run ? "试运行中" : "审核已开启") : "未启用"}"></span>` +
        `<span class="${nameCls}" title="${esc(g.group)}">${esc(groupLabel(g))}${g.alias ? " ✎" : ""}</span>` +
        `<span class="meta">${g.rule_count} 条</span>`;
      li.onclick = () => selectGroup(g.group);
      ul.appendChild(li);
      shown++;
    });
  });
  if (!shown) {
    ul.innerHTML = `<li class="empty-li">${kw ? "没匹配到群" : "暂无群记录（机器人在过的群会自动出现在这里）"}</li>`;
  }
}

/* 手动刷新群名（默认只补还没名字的群；force=true 才重拉已有的） */
async function refreshNames(opts) {
  const body = Object.assign({}, opts || {});
  if (!body.group && !body.platform_id) body.all = true;
  toast(body.force ? "正在重拉群名…" : "正在补齐群名…");
  try {
    const r = await bridge.apiPost("mod/group_names/refresh", body);
    toast(r.message || "已更新");
    await loadGroups();
  } catch (e) {
    toast("拉取失败：" + e.message, true);
  }
}

function renderNameHint(item) {
  const el = $("name-hint");
  item = item || {};
  let text = "";
  let cls = "name-hint";
  let action = "";   // 手动按钮（服务端默认会跳过已有名字的群）
  if (item.alias) {
    text = `备注名「${item.alias}」——仅本面板显示，群本身的名称${item.name ? "：" + item.name : "未获取"}`;
    cls = "name-hint ok";
    action = `<button class="btn small" id="btn-force-name">重拉本群群名</button>`;
  } else if (item.name) {
    text = `群名来自官方接口：${item.name}（已记录，不会再自动请求接口）`;
    cls = "name-hint ok";
    action = `<button class="btn small" id="btn-force-name">重拉本群群名</button>`;
  } else if (item.name_state === "denied") {
    text = "群名取不到：群信息接口（/v2/groups/{openid}/info）仅白名单机器人可用（错误码 11253，需向腾讯平台运营申请），可点「✎ 备注」自己起个名字";
    cls = "name-hint warn";
    action = `<button class="btn small" id="btn-force-name">重试</button>`;
  } else if (item.name_state === "failed") {
    text = `群名拉取失败：${item.name_error || "未知错误"}（可重试或手动备注）`;
    cls = "name-hint warn";
    action = `<button class="btn small" id="btn-force-name">重试</button>`;
  } else {
    text = "尚未拉取群名。可点右上「↻ 补群名」，或在群里发 /群管 群名（需群信息接口权限）";
    action = `<button class="btn small" id="btn-force-name">拉取本群群名</button>`;
  }
  el.className = cls;
  el.innerHTML = esc(text) + (action ? " " + action : "");
  const fb = $("btn-force-name");
  if (fb) fb.onclick = () => refreshNames({ group: current, force: true }).catch(() => {});
}

function openNameEdit() {
  if (!detail) return toast("先选一个群", true);
  const item = findGroup(current) || {};
  $("name-input").value = item.alias || "";
  $("name-edit").hidden = false;
  $("name-input").focus();
}

async function saveName(alias) {
  if (!current) return toast("先选一个群", true);
  try {
    await bridge.apiPost("mod/group_rename", { group: current, alias: String(alias || "") });
    toast(alias ? "备注已保存" : "已清空备注");
    $("name-edit").hidden = true;
    await loadGroups();
    await selectGroup(current);
  } catch (e) {
    toast("保存失败：" + e.message, true);
  }
}

/* ---------------- 单群详情 ---------------- */
async function selectGroup(g) {
  current = g;
  renderGroups();
  try {
    detail = await bridge.apiGet("mod/detail", { group: g });
  } catch (e) {
    toast("读取群配置失败：" + e.message, true);
    return;
  }
  const item = findGroup(g) || detail || {};
  const bot = (detail && detail.bot) || {};
  $("group-title").textContent = groupLabel(item);
  const st = detail.settings || {};
  $("group-sub").textContent =
    `🤖 ${bot.name || bot.platform_id || "未知机器人"} · ${g} · ` +
    `审核${st.enabled ? "已开启" : "未开启"} · ${st.dry_run ? "试运行" : "正式执行"} · 巡检${st.auto_scan ? "开" : "关"}`;
  const scopeEl = $("recScopeGroup");
  if (scopeEl) scopeEl.textContent = groupLabel(item) + "（" + String(g).slice(-6) + "）";
  renderNameHint(item);
  renderRules();
  renderSettings();
  renderTemplates();
  renderStats();
  await loadRecords();
  await loadTodo();
  await loadAdmins();
}

/* ---------------- 群规 ---------------- */
function renderRules() {
  const box = $("rules-list");
  const rules = (detail && detail.rules) || [];
  const cats = Object.assign({}, CAT_LABELS, detail ? detail.cat_names : {});
  box.innerHTML = rules.length ? "" : '<div class="desc">本群还没有规则。可点下方「添加一条规则」，或用群内指令 /群管 群规 添加。</div>';
  rules.forEach((r, i) => {
    const el = document.createElement("div");
    el.className = "rule";
    el.innerHTML = `
      <div class="rule-head">
        <b>规则 ${i + 1} <code>${esc(r.id || "")}</code></b>
        <label><input type="checkbox" data-f="enabled" ${r.enabled === false ? "" : "checked"} /> 启用</label>
        <button class="btn btn-danger" data-act="del">删除</button>
      </div>
      <label>规则名<input type="text" data-f="name" value="${esc(r.name || "")}" /></label>
      <label>时长<input type="text" data-f="duration" value="${esc(fmtDur(r.seconds))}" placeholder="如 10分钟 / 2小时 / 1天（最小 60 秒，最长 30 天）" /></label>
      <label class="full">判定说明<input type="text" data-f="desc" value="${esc(r.desc || "")}" placeholder="例如：含联系方式、二维码、引流话术" /></label>
      <label>类型
        <select data-f="category">${Object.keys(cats).map((k) => `<option value="${k}" ${r.category === k ? "selected" : ""}>${esc(cats[k])}</option>`).join("")}</select>
      </label>
      <label>严重度
        <select data-f="severity">${SEVERITIES.map(([k, v]) => `<option value="${k}" ${r.severity === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      </label>`;
    el.querySelector('[data-act="del"]').onclick = () => {
      el.remove();
      toast("已从列表移除，别忘了点保存");
    };
    box.appendChild(el);
  });
}

function collectRules() {
  const out = [];
  document.querySelectorAll("#rules-list .rule").forEach((el, i) => {
    const get = (f) => el.querySelector(`[data-f="${f}"]`);
    out.push({
      id: (el.querySelector("code") || {}).textContent || `rule_${String(i + 1).padStart(2, "0")}`,
      name: get("name").value.trim(),
      desc: get("desc").value.trim(),
      seconds: Math.max(60, Math.min(parseDur(get("duration").value), 30 * 86400)),
      category: get("category").value,
      severity: get("severity").value,
      enabled: get("enabled").checked,
    });
  });
  return out;
}

/* ---------------- 举报设置 ---------------- */
const S_KEYS = ["enabled", "dry_run", "auto_scan", "auto_scan_llm", "enable_keyword", "enable_model",
  "enable_vision", "keep_context", "reply_when_clean",
  "context_size", "confidence_threshold", "max_mute_seconds", "report_limit_per_hour", "daily_budget",
  "llm_queue_limit", "llm_timeout",
  "recall_max_age_seconds",
  "variant_check", "variant_recall", "variant_confusable", "variant_abbr", "variant_fuzzy", "variant_min_chars",
  "variant_strike_enabled", "variant_strike_window_days", "variant_strike_threshold",
  "variant_strike_notify", "variant_strike_mute", "variant_strike_mute_threshold",
  "variant_strike_mute_seconds", "variant_strike_require_confirm", "variant_strike_cooldown_hours",
  "todo_enabled", "smart_penalty", "smart_tier_bump_at", "smart_force_mute_priors",
  "escalate_enabled", "escalate_window_days", "escalate_step", "escalate_max_multiplier"];

function renderSettings() {
  const st = (detail && detail.settings) || {};
  S_KEYS.forEach((k) => {
    const el = $("s-" + k);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!st[k];
    else el.value = st[k] == null ? "" : st[k];
  });
  $("s-whitelist").value = (st.whitelist || []).join("\n");
  const kws = st.keywords || {};
  $("s-keywords").value = Object.keys(kws)
    .map((c) => (kws[c] || []).map((p) => `${c}: ${p}`).join("\n"))
    .filter(Boolean)
    .join("\n");
}

function collectSettings() {
  const out = {};
  S_KEYS.forEach((k) => {
    const el = $("s-" + k);
    if (!el) return;
    if (el.type === "checkbox") out[k] = el.checked;
    else out[k] = parseFloat(el.value || 0) || 0;
  });
  out.whitelist = $("s-whitelist").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const kws = {};
  $("s-keywords").value.split("\n").forEach((line) => {
    const i = line.indexOf(":");
    if (i < 0) return;
    const cat = line.slice(0, i).trim();
    const pat = line.slice(i + 1).trim();
    if (cat && pat) (kws[cat] = kws[cat] || []).push(pat);
  });
  out.keywords = kws;
  out.context_size = parseInt(out.context_size || 0, 10);
  out.max_mute_seconds = parseInt(out.max_mute_seconds || 0, 10);
  out.recall_max_age_seconds = parseInt(out.recall_max_age_seconds || 0, 10);
  out.report_limit_per_hour = parseInt(out.report_limit_per_hour || 0, 10);
  out.llm_queue_limit = Math.max(0, Math.min(parseInt(out.llm_queue_limit || 3, 10), 20));
  out.llm_timeout = Math.max(0, Math.min(parseInt(out.llm_timeout || 45, 10), 300));
  out.variant_min_chars = Math.max(2, Math.min(parseInt(out.variant_min_chars || 3, 10), 12));
  out.variant_strike_window_days = Math.max(1, Math.min(parseInt(out.variant_strike_window_days || 7, 10), 90));
  out.variant_strike_threshold = Math.max(1, Math.min(parseInt(out.variant_strike_threshold || 3, 10), 50));
  out.variant_strike_mute_threshold = Math.max(1, Math.min(parseInt(out.variant_strike_mute_threshold || 5, 10), 50));
  out.variant_strike_mute_seconds = Math.max(60, Math.min(parseInt(out.variant_strike_mute_seconds || 600, 10), 86400));
  out.variant_strike_cooldown_hours = Math.max(0, Math.min(parseInt(out.variant_strike_cooldown_hours || 0, 10), 168));
  out.escalate_window_days = Math.max(1, Math.min(parseInt(out.escalate_window_days || 30, 10), 365));
  out.smart_tier_bump_at = Math.max(1, Math.min(parseInt(out.smart_tier_bump_at || 2, 10), 20));
  out.smart_force_mute_priors = Math.max(0, Math.min(parseInt(out.smart_force_mute_priors || 0, 10), 99));
  return out;
}

/* ---------------- 话术模板 ---------------- */
function renderTemplates() {
  const tpl = (detail && detail.templates) || {};
  const dflt = (detail && detail.default_templates) || {};
  const box = $("tpl-fields");
  box.innerHTML = "";
  TPL_FIELDS.forEach(([key, label]) => {
    const el = document.createElement("label");
    el.className = "field block";
    const overridden = detail && detail.templates_override && detail.templates_override[key] !== undefined;
    el.innerHTML =
      `<span>${esc(label)}${overridden ? ' <b style="color:var(--primary)">（本群已覆盖）</b>' : ""}</span>` +
      `<textarea rows="2" data-tpl="${key}" placeholder="全局默认：${esc(dflt[key] || "")}"></textarea>`;
    box.appendChild(el);
    el.querySelector("textarea").value = tpl[key] || "";
  });

  const dims = Object.assign({}, DIM_LABELS, (detail && detail.dim_names) || {});
  const dimBox = $("dim-fields");
  dimBox.innerHTML = "";
  Object.keys(DIM_LABELS).forEach((k) => {
    const el = document.createElement("label");
    el.className = "field";
    el.innerHTML = `<span style="min-width:70px">${k}</span><input type="text" data-dim="${k}" value="${esc(dims[k] || "")}" />`;
    dimBox.appendChild(el);
  });

  const cats = Object.assign({}, CAT_LABELS, (detail && detail.cat_names) || {});
  const catBox = $("cat-fields");
  catBox.innerHTML = "";
  Object.keys(CAT_LABELS).forEach((k) => {
    const el = document.createElement("label");
    el.className = "field";
    el.innerHTML = `<span style="min-width:70px">${k}</span><input type="text" data-cat="${k}" value="${esc(cats[k] || "")}" />`;
    catBox.appendChild(el);
  });
}

function collectTemplates() {
  const t = {};
  document.querySelectorAll("[data-tpl]").forEach((el) => {
    const v = el.value.trim();
    if (v !== "") t[el.dataset.tpl] = v;
  });
  const dims = {};
  document.querySelectorAll("[data-dim]").forEach((el) => { dims[el.dataset.dim] = el.value.trim(); });
  const cats = {};
  document.querySelectorAll("[data-cat]").forEach((el) => { cats[el.dataset.cat] = el.value.trim(); });
  t.dim_names = dims;
  t.cat_names = cats;
  return t;
}

/* ---------------- 统计 / 记录 / 试跑 ---------------- */
const btnVerdictRefresh = $("btnVerdictRefresh");
if (btnVerdictRefresh) btnVerdictRefresh.addEventListener("click", () => { loadRecords(); loadTodo(); });
const btnTodoRefresh = $("btnTodoRefresh");
if (btnTodoRefresh) btnTodoRefresh.addEventListener("click", loadTodo);
const btnAdminRefresh = $("btnAdminRefresh");
if (btnAdminRefresh) btnAdminRefresh.addEventListener("click", loadAdmins);
const btnVerdictClearOne = $("btnVerdictClearOne");
if (btnVerdictClearOne) btnVerdictClearOne.addEventListener("click", () => clearVerdicts("group"));
const btnVerdictClearAll = $("btnVerdictClearAll");
if (btnVerdictClearAll) btnVerdictClearAll.addEventListener("click", () => clearVerdicts("all"));

function renderStats() {
  const s = (detail && detail.stats) || {};
  $("stat-row").innerHTML = [
    ["累计判定", s.total || 0],
    ["判定违规", s.hit || 0],
    ["缓存命中率", ((s.cache_rate || 0) * 100).toFixed(0) + "%"],
    ["今日判定", s.today_total || 0],
    ["变体疑似", s.variant_hits || 0],
    ["变体累计禁言", s.variant_strike_muted || 0],
    ["待办", s.todo_open || 0],
  ].map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
}

async function loadRecords() {
  const res = await bridge.apiGet("mod/verdicts", { group: current, limit: 80 });
  const rows = $("verdict-rows");
  rows.innerHTML = "";
  (res.items || []).forEach((v) => {
    const tr = document.createElement("tr");
    const dim = (detail && detail.dim_names) || DIM_LABELS;
    const cats = (detail && detail.cat_names) || CAT_LABELS;
    tr.innerHTML =
      `<td>${ts2str(v.created_at)}</td>` +
      `<td>${esc((v.target_name || v.target || "-").slice(0, 12))}</td>` +
      `<td class="vtext" data-vd="${v.id}" title="${esc(v.text || "")}">${esc((v.text || "—").slice(0, 24))}${(v.text || "").length > 24 ? "…" : ""}` +
      (v.images ? ` <span class="hint">🖼${v.images}</span>` : "") + `</td>` +
      `<td>${esc(dim[v.basis] || v.basis || "-")}</td>` +
      `<td><span class="tag ${v.violation ? "hit" : "clean"}">${v.violation ? esc(cats[v.category] || v.category || "违规") : "未违规"}</span>` +
      (v.variant_hit ? ` <span class="tag" title="疑似变体写法（${esc((v.variant || {}).method || "")}）${v.variant_recalled ? "，已按变体撤回" : ""}">变体</span>` : "") + `</td>` +
      `<td>${v.confidence == null ? "-" : Number(v.confidence).toFixed(2)}</td>` +
      `<td>${v.revoked ? '<span class="tag" title="' + esc((v.revoked.reason || "已撤销")) + '">已撤销</span> ' : ""}` +
      `${v.executed ? "已禁言 " + fmtDur(v.ban_seconds) : esc(v.skipped_reason || (v.violation ? "未执行" : "-"))}</td>` +
      `<td>${v.from_cache ? "是" : "否"}</td>` +
      `<td style="white-space:nowrap"><button class="btn" data-fb="${v.id}">误判</button> ` +
      `<button class="btn" data-ok="${v.id}" title="判定正确，加入提示词正例">正确</button> ` +
      (v.executed && !v.revoked ? `<button class="btn" data-rv="${v.id}" title="撤销这条处罚并致歉">撤销</button> ` : "") +
      `<button class="btn danger" data-dv="${v.id}" title="删除这条本地记录">✕</button></td>`;
    tr.querySelector("[data-fb]").onclick = () => markFeedback(v.id, "false_positive");
    tr.querySelector("[data-ok]").onclick = () => markFeedback(v.id, "true_positive");
    tr.querySelector("[data-vd]").onclick = () => showVerdictDetail(v);
    tr.querySelector("[data-dv]").onclick = () => delVerdict(v.id);
    const rv = tr.querySelector("[data-rv]");
    if (rv) rv.onclick = () => revokeVerdict(v);
    rows.appendChild(tr);
  });
  if (!(res.items || []).length) rows.innerHTML = '<tr><td colspan="9" style="color:var(--sub)">暂无记录</td></tr>';
}

/* ---------------- 待办（半自动：机器筛、人确认） ---------------- */
const TODO_KIND_LABEL = { low_confidence: "低置信未处理", variant: "变体疑似",
  variant_strike: "变体累计（达阈值）", variant_strike_mute: "变体累计（已禁言）" };

async function loadTodo() {
  const rows = $("todo-rows");
  if (!rows) return;
  if (!current) {
    rows.innerHTML = '<tr><td colspan="6" style="color:var(--sub)">先在左侧选一个作用群</td></tr>';
    return;
  }
  try {
    const res = await bridge.apiGet("mod/todo", { group: current, limit: 50 });
    const items = res.items || [];
    const cnt = $("todo-count");
    if (cnt) cnt.textContent = `${res.count || 0} 条待处理（近 ${res.window_days || 7} 天）`;
    rows.innerHTML = "";
    items.forEach((it) => {
      const tr = document.createElement("tr");
      const vkind = it.variant_method === "pinyin" ? "谐音" : (it.variant_method ? "形近" : "");
      const strike = it.variant_strike || {};
      const strikeTxt = strike.count
        ? ` <span class="hint">累计 ×${strike.count}/${strike.window_days || 7}天${strike.muted ? " · 已自动禁言" : (strike.notified ? " · 已提醒" : "")}</span>`
        : "";
      tr.innerHTML =
        `<td>${esc(it.created_at_text || ts2str(it.created_at))}</td>` +
        `<td><span class="tag ${String(it.kind || "").startsWith("variant") ? "hit" : ""}">${esc(TODO_KIND_LABEL[it.kind] || it.kind)}</span></td>` +
        `<td>${esc((it.target_name || it.target || "-").slice(0, 12))}</td>` +
        `<td title="${esc(it.text || "")}">${esc((it.text || "").slice(0, 18))}` +
        (vkind ? `<span class="hint"> · ${esc(vkind)}${it.variant_recalled ? " · 已撤回" : ""}</span>` : "") + strikeTxt + `</td>` +
        `<td>${esc(it.suggest_text || "-")}</td>` +
        `<td style="white-space:nowrap">` +
        `<button class="btn small" data-td-act="recall">撤回</button> ` +
        `<button class="btn small" data-td-act="mute">禁言</button> ` +
        `<button class="btn ghost small" data-td-act="ignore">忽略</button></td>`;
      tr.querySelector('[data-td-act="recall"]').onclick = () => handleTodo(it.id, "recall");
      tr.querySelector('[data-td-act="mute"]').onclick = () => handleTodo(it.id, "mute", it.suggest_seconds || 600);
      tr.querySelector('[data-td-act="ignore"]').onclick = () => handleTodo(it.id, "ignore");
      rows.appendChild(tr);
    });
    if (!items.length) rows.innerHTML = '<tr><td colspan="6" style="color:var(--sub)">没有待办 ✅</td></tr>';
  } catch (e) {
    rows.innerHTML = `<tr><td colspan="6" style="color:var(--sub)">读取失败：${esc(e.message)}</td></tr>`;
  }
}

/* ---------------- 变体提醒名单（管理员自助登记 + 面板移出） ---------------- */
const ROLE_LABEL = { qq_admin: "QQ 群管理员", astrbot_admin: "AstrBot 管理员", owner: "群主", admin: "群管理员" };

async function loadAdmins() {
  const rows = $("admin-rows");
  if (!rows) return;
  if (!current) {
    rows.innerHTML = '<tr><td colspan="5" style="color:var(--sub)">先在左侧选一个作用群</td></tr>';
    return;
  }
  try {
    const res = await bridge.apiGet("mod/admins", { group: current });
    const items = res.items || [];
    const cnt = $("admin-count");
    if (cnt) cnt.textContent = `${res.count || 0} 人${res.notify === false ? "（提醒已关）" : ""}`;
    rows.innerHTML = "";
    items.forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>${esc(a.name || "-")}</td>` +
        `<td>${esc(String(a.openid || "").slice(0, 12))}…</td>` +
        `<td>${esc(ROLE_LABEL[a.role] || a.role || "-")}</td>` +
        `<td>${esc(ts2str(a.at))}</td>` +
        `<td><button class="btn ghost small" data-adm-rm="1">移出</button></td>`;
      tr.querySelector("[data-adm-rm]").onclick = async () => {
        if (!(await showConfirm(`把 ${a.name || a.openid} 移出提醒名单？`))) return;
        try {
          const r = await bridge.apiPost("mod/admins/remove", { group: current, openid: a.openid });
          if (r.error) return toast(r.error, true);
          toast(r.removed ? "已移出提醒名单" : "本来就不在名单里");
          loadAdmins();
        } catch (e) {
          toast("移出失败：" + e.message, true);
        }
      };
      rows.appendChild(tr);
    });
    if (!items.length) {
      rows.innerHTML = '<tr><td colspan="5" style="color:var(--sub)">还没有人登记。让群管理员在群里发「/群管 管理登记」即可加入</td></tr>';
    }
  } catch (e) {
    rows.innerHTML = `<tr><td colspan="5" style="color:var(--sub)">读取失败：${esc(e.message)}</td></tr>`;
  }
}

async function handleTodo(id, action, seconds) {
  const label = { recall: "撤回这条消息", mute: "禁言该成员", ignore: "标记为不处理" }[action] || action;
  if (action !== "ignore" && !(await showConfirm(`确认${label}？（记录 #${id}）`))) return;
  try {
    const r = await bridge.apiPost("mod/todo/handle", { group: current, ids: [id], action, seconds });
    if (r.error) return toast(r.error, true);
    const first = (r.items || [])[0] || {};
    toast(`${label}：${first.note || "已处理"}`);
    await loadTodo();
    if (action !== "ignore") loadRecords();
  } catch (e) {
    toast("处理失败：" + e.message, true);
  }
}

async function markFeedback(id, label = "false_positive") {
  try {
    const r = await bridge.apiPost("mod/feedback", { id, label, note: "Page 标记" });
    const n = r && r.samples != null ? `（当前 ${r.samples} 条同类样本）` : "";
    showToast(label === "false_positive"
      ? `已记为误判样本，会进入提示词示例${n}`
      : `已记为正确样本，会作为正例进入提示词${n}`);
    loadRecords();
  } catch (e) {
    toast("标记失败：" + e.message, true);
  }
}

/* 撤销处罚：面板侧（AstrBot 管理员）——会尝试解禁，并留下审计 */
async function revokeVerdict(v) {
  const reason = "面板管理员人工复核撤销";
  if (!(await showConfirm(`确认撤销记录 #${v.id} 的处罚？\n将尝试解除禁言，并从「前科」统计中排除。`))) return;
  try {
    const r = await bridge.apiPost("mod/verdicts/revoke", { id: v.id, group: v.group, reason: reason });
    if (r.ok === false) throw new Error(r.error || "撤销失败");
    if (r.already) toast("这条记录之前已经撤销过了");
    else if (r.unmute && r.unmute.needed && r.unmute.ok === false) toast("已撤销，但解禁失败：" + (r.unmute.code || "请手动解禁"), true);
    else toast("已撤销" + (r.unmute && r.unmute.needed ? "，禁言已解除" : ""));
    loadRecords();
  } catch (e) {
    toast("撤销失败：" + e.message, true);
  }
}

/* 按天清理判定记录：仅手动，不自动 */
async function pruneVerdicts() {
  const el = $("prune-days");
  const days = parseInt((el && el.value) || "0", 10);
  if (!days || days <= 0) return toast("请填写要删除多少天以前的记录", true);
  if (!(await showConfirm(`删除「${current ? "本群" : "全部群"}」${days} 天以前的判定记录？\n该操作不可撤销，建议先导出 CSV。`))) return;
  try {
    const r = await bridge.apiPost("mod/verdicts/prune", { days: days, group: current || "" });
    if (r.ok === false) throw new Error(r.error || "清理失败");
    toast(`已删除 ${r.removed} 条（${days} 天以前）`);
    loadRecords();
  } catch (e) {
    toast("清理失败：" + e.message, true);
  }
}

/* 判定详情：原文 + 完整字段，用来人工二次确认（旧记录只有 200 字，会标注） */
function showVerdictDetail(v) {
  const old = document.getElementById("vd-modal");
  if (old) old.remove();
  const dim = (detail && detail.dim_names) || DIM_LABELS;
  const cats = (detail && detail.cat_names) || CAT_LABELS;
  const textLen = v.text_len != null ? v.text_len : (v.text || "").length;
  const rows = [
    ["群", v.group || "-"],
    ["时间", ts2str(v.created_at)],
    ["对象", v.target_name || v.target || "-"],
    ["来源", v.reporter === "auto_scan" ? "自动巡检" : (v.reporter || "-")],
    ["维度", dim[v.basis] || v.basis || "-"],
    ["类型", v.violation ? (cats[v.category] || v.category || "违规") : "未违规"],
    ["严重度", v.severity || "-"],
    ["置信度", v.confidence == null ? "-" : Number(v.confidence).toFixed(2)],
    ["理由", v.reason || "-"],
    ["命中规则", v.rule_name || "-"],
    ["动作", v.executed ? "已禁言 " + fmtDur(v.ban_seconds) : (v.skipped_reason || (v.violation ? "未执行" : "-"))],
    ["执行模式", v.dry_run ? "试运行" : "真实执行"],
    ["模型", v.model || "-"],
    ["人工标注", v.feedback === "false_positive" ? "误判" : (v.feedback === "true_positive" ? "正确" : "-")],
    ["撤销", v.revoked ? `已于 ${ts2str(v.revoked.at)} 由 ${v.revoked.by_name || v.revoked.by || "-"} 撤销`
      + `（${v.revoked.reason || "未填理由"}）` + (v.revoked.unmute_needed ? (v.revoked.unmute_ok ? "，禁言已解除" : "，解禁失败") : "") : "-"],
  ];
  const wrap = document.createElement("div");
  wrap.id = "vd-modal";
  wrap.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;display:flex;align-items:center;justify-content:center;padding:24px";
  wrap.innerHTML =
    `<div style="background:var(--panel,#1b1b1f);color:var(--text,#eee);max-width:760px;width:100%;max-height:82vh;overflow:auto;border-radius:12px;padding:18px;line-height:1.7;box-shadow:0 12px 40px rgba(0,0,0,.4)">` +
    `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">` +
    `<b>判定详情 #${v.id}</b><button class="btn small" id="vd-close">关闭</button></div>` +
    `<div>${rows.map(([k, val]) => `<div><span style="color:var(--sub,#999)">${k}：</span>${esc(val)}</div>`).join("")}</div>` +
    `<div style="margin-top:12px;color:var(--sub,#999)">原文（${textLen} 字${v.text_truncated ? "，超出上限已截断" : ""}` +
    `${v.images ? `｜含 ${v.images} 张图片（官方链接有时效，未存档）` : ""}）：</div>` +
    `<pre style="white-space:pre-wrap;word-break:break-all;background:rgba(127,127,127,.14);padding:10px;border-radius:8px;margin:6px 0;font-size:12px">${esc(v.text || "（这条是旧版记录，当时只留了 200 字以内，且可能为空）")}</pre>` +
    `<button class="btn small" id="vd-copy">复制原文</button></div>`;
  wrap.onclick = (e) => { if (e.target === wrap) wrap.remove(); };
  document.body.appendChild(wrap);
  document.getElementById("vd-close").onclick = () => wrap.remove();
  const cp = document.getElementById("vd-copy");
  if (cp) cp.onclick = () => {
    const t = v.text || "";
    if (navigator.clipboard) navigator.clipboard.writeText(t).then(() => showToast("已复制")).catch(() => {});
    else toast("浏览器不允许复制，请手动选中", true);
  };
}

/* 导出判定记录：服务端筛选，前端拼 CSV（带 BOM，Excel 打开不乱码） */
function csvCell(v) {
  const s = v == null ? "" : String(v);
  return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}
async function exportVerdicts() {
  try {
    const res = await bridge.apiGet("mod/verdicts/export", { group: current, limit: 5000 });
    const items = res.items || [];
    if (!items.length) return showToast("没有可导出的记录");
    const dim = (detail && detail.dim_names) || DIM_LABELS;
    const cats = (detail && detail.cat_names) || CAT_LABELS;
    const head = ["时间", "群", "消息ID", "来源", "对象", "原文", "原文总长", "是否截断", "图片数",
      "维度", "类型", "是否违规", "严重度", "置信度", "理由", "命中规则",
      "动作", "已执行", "试运行", "模型", "缓存命中", "人工标注", "标注备注"];
    const lines = [head.join(",")];
    items.forEach((v) => {
      lines.push([
        ts2str(v.created_at), v.group, v.msg_id, v.reporter === "auto_scan" ? "自动巡检" : v.reporter,
        v.target_name || v.target, v.text, v.text_len != null ? v.text_len : (v.text || "").length,
        v.text_truncated ? "是" : "否", v.images || 0,
        dim[v.basis] || v.basis, v.violation ? (cats[v.category] || v.category || "违规") : "未违规",
        v.violation ? "是" : "否", v.severity, v.confidence, v.reason, v.rule_name,
        v.executed ? "已禁言" + fmtDur(v.ban_seconds) : (v.skipped_reason || (v.violation ? "未执行" : "-")),
        v.executed ? "是" : "否", v.dry_run ? "是" : "否", v.model, v.from_cache ? "是" : "否",
        v.feedback === "false_positive" ? "误判" : (v.feedback === "true_positive" ? "正确" : ""),
        v.feedback_note || "",
      ].map(csvCell).join(","));
    });
    const stamp = new Date().toISOString().slice(0, 10);
    const blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `qqgm-verdicts-${current ? current.slice(-6) : "all"}-${stamp}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    showToast(`已导出 ${items.length} 条判定记录`);
  } catch (e) {
    toast("导出失败：" + e.message, true);
  }
}
const btnVerdictExport = document.getElementById("btnVerdictExport");
if (btnVerdictExport) btnVerdictExport.addEventListener("click", exportVerdicts);
const _goModTpl = document.getElementById("btnGoModTpl");
if (_goModTpl) _goModTpl.onclick = () => {
  switchTabSafe("moderation");
  const mt = document.querySelector('.mod-tab[data-mod-tab="templates"]');
  if (mt) mt.click();
  setTimeout(() => {
    const f = document.querySelector("#tpl-fields");
    if (f) f.scrollIntoView({ behavior: "smooth", block: "start" });
  }, 60);
};
const _btnPrune = document.getElementById("btnPruneVerdicts");
if (_btnPrune) _btnPrune.addEventListener("click", pruneVerdicts);

async function delVerdict(id) {
  if (!(await showConfirm(`删除判定记录 #${id}？只删本地这条留痕，不影响已生效的禁言。`))) return;
  try {
    const r = await bridge.apiPost("mod/verdicts/delete", { id });
    showToast(r.message || "已删除");
    loadRecords();
  } catch (e) {
    showToast("删除失败：" + e.message, true);
  }
}

async function clearVerdicts(scope) {
  const g = scope === "group" ? current : "";
  if (scope === "group" && !g) return showToast("先在下方选择要操作的群", true);
  const what = g ? "当前作用群" : "全部群";
  if (!(await showConfirm(`清空${what}的判定记录？此操作不可恢复（仅删本地留痕，不影响已生效的禁言）。`))) return;
  try {
    const r = await bridge.apiPost("mod/verdicts/clear", g ? { group: g } : {});
    showToast(r.message || `已清空 ${r.cleared} 条`);
    loadRecords();
  } catch (e) {
    showToast("清空失败：" + e.message, true);
  }
}

async function runTest() {
  const text = $("test-text").value.trim();
  if (!text) return toast("先输入一条消息内容", true);
  $("test-result").textContent = "判定中…（走模型可能要几秒）";
  try {
    const priorEl = $("test-prior");
    const prior = Math.max(0, Math.min(99, parseInt((priorEl && priorEl.value) || "0", 10) || 0));
    const r = await bridge.apiPost("mod/test", { group: current, text, prior: prior });
    if (r.error) {
      $("test-result").textContent = "❌ " + r.error;
      return;
    }
    const v = r.verdict || {};
    const dim = (detail && detail.dim_names) || DIM_LABELS;
    const cats = (detail && detail.cat_names) || CAT_LABELS;
    $("test-result").textContent = [
      `违规：${v.violation ? "是" : "否"}`,
      `维度（话术里的“根据”）：${dim[v.basis] || v.basis || "-"}`,
      `类型：${cats[v.category] || v.category || "-"}　严重度：${v.severity || "-"}`,
      `置信度：${v.confidence == null ? "-" : v.confidence}　${r.used_llm ? "（调用了模型）" : "（零成本层命中）"}`,
      `判定模型：${r.provider && r.provider.id ? `${r.provider.display}（${PROVIDER_SOURCE_LABEL[r.provider.source] || r.provider.source}）` : "未调用模型"}`,
      `理由：${v.reason || "-"}`,
      `拟执行：${(() => {
        const a = (r.action && r.action.action) || "none";
        const base = a === "mute" ? "禁言 " + (r.duration || "")
          : a === "warn" ? (r.action.recall ? "撤回 + 警告" : "警告（不撤回）")
          : a === "todo" ? "转人工待办（不自动处罚）" : "无动作";
        const prior = r.action && r.action.prior_offenses
          ? `｜前科 ${r.action.prior_offenses} 次` + (r.action.escalation && r.action.escalation !== 1 ? `，倍率 ×${r.action.escalation}` : "")
          : "";
        return base + prior + (r.action && r.action.smart ? "（智能分级已开启）" : "");
      })()}` ,
      `变体层：${r.variant ? `疑似命中（${r.variant.hint}｜强度 ${r.variant.strength}｜${r.variant.count} 处）` : "未命中"}${r.variant && !(detail && detail.settings && detail.settings.variant_check) ? "（本群未开启变体检测，此处仅试跑）" : ""}`,
      "",
      `群内会回复：${(() => {
        const a = (r.action && r.action.action) || "none";
        if (!v.violation) return "根据多重维度评判，此消息不涉嫌违规";
        if (a === "mute") return `根据${dim[v.basis] || "多重维度"}，符合${cats[v.category] || "违规"}，已被禁言${r.duration}`;
        if (a === "warn") return "本消息已记录（警告），再次出现将按群规处理";
        if (a === "todo") return "已转人工复核，暂不处罚";
        return "未执行处罚（置信度不足或命中豁免）";
      })()}` ,
    ].join("\n");
  } catch (e) {
    $("test-result").textContent = "❌ " + e.message;
  }
}

/* ---------------- 保存 ---------------- */
async function saveRules() {
  if (!current) return toast("先选一个群", true);
  try {
    const r = await bridge.apiPost("mod/rules/save", { group: current, rules: collectRules() });
    toast(`已保存 ${r.count || 0} 条群规`);
    await selectGroup(current);
  } catch (e) {
    toast("保存失败：" + e.message, true);
  }
}

/* ---------------- 群规导出 / 转移到其他群 ---------------- */
function _ruleTypeLabel(cat) {
  const all = Object.assign({}, CAT_LABELS, (detail && detail.cat_names) || {});
  return all[cat] || cat || "其他";
}

/* 生成给群公告用的文本：只收「启用」的规则，别把停用的也贴出去 */
function buildRulesAnnouncement() {
  const rules = collectRules().filter((r) => r.enabled !== false);
  if (!rules.length) return "";
  const gname = groupLabelByOpenid(current) || "本群";
  const lines = ["【" + gname + " 群规】", ""];
  rules.forEach((r, i) => {
    lines.push((i + 1) + ". " + (r.name || "未命名规则") + "（" + _ruleTypeLabel(r.category) + "）"
      + "｜违规处理：禁言 " + fmtDur(r.seconds));
    if (r.desc) lines.push("   说明：" + r.desc);
  });
  lines.push("", "共 " + rules.length + " 条 · 生成于 " + new Date().toLocaleString("zh-CN"));
  return lines.join("\n");
}

/* 通用弹层：面板里几个小对话框共用，省得每处写一遍 overlay */
function openOverlay(id, innerHtml) {
  const old = document.getElementById(id);
  if (old) old.remove();
  const wrap = document.createElement("div");
  wrap.id = id;
  wrap.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;"
    + "display:flex;align-items:center;justify-content:center;padding:24px";
  wrap.innerHTML = '<div style="background:var(--panel,#1b1b1f);color:var(--text,#eee);max-width:720px;'
    + 'width:100%;max-height:82vh;overflow:auto;border-radius:12px;padding:18px;'
    + 'line-height:1.7;box-shadow:0 12px 40px rgba(0,0,0,.4)">' + innerHtml + '</div>';
  wrap.onclick = (e) => { if (e.target === wrap) wrap.remove(); };
  document.body.appendChild(wrap);
  return wrap;
}

function exportRules() {
  if (!current) return toast("先选一个群", true);
  const text = buildRulesAnnouncement();
  if (!text) return toast("本群没有启用的群规，先添加并保存", true);
  const wrap = openOverlay("rules-export",
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
    + '<b>导出群规</b><button class="btn small" data-x="close">关闭</button></div>'
    + '<div class="desc" style="margin:0 0 8px">只包含<b>已启用</b>的规则；复制后可直接粘到群公告，也可下载 .txt。</div>'
    + '<textarea id="rules-export-text" spellcheck="false" style="width:100%;height:300px;font-size:12px;line-height:1.7"></textarea>'
    + '<div style="display:flex;gap:8px;margin-top:10px">'
    + '<button class="btn btn-primary small" data-x="copy">复制</button>'
    + '<button class="btn small" data-x="download">下载 .txt</button></div>');
  wrap.querySelector("#rules-export-text").value = text;
  wrap.querySelector('[data-x="close"]').onclick = () => wrap.remove();
  wrap.querySelector('[data-x="copy"]').onclick = () => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text)
        .then(() => toast("已复制，去群公告里粘贴吧"))
        .catch(() => toast("复制失败，请手动选中", true));
    } else {
      const ta = wrap.querySelector("#rules-export-text");
      ta.select();
      toast("浏览器不允许自动复制，已帮你选中", true);
    }
  };
  wrap.querySelector('[data-x="download"]').onclick = () => {
    const name = (groupLabelByOpenid(current) || "群规").replace(/[\\/:*?"<>|·\s]+/g, "-");
    const blob = new Blob(["\ufeff" + text], { type: "text/plain;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "群规-" + name + "-" + new Date().toISOString().slice(0, 10) + ".txt";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  };
}

async function transferRules() {
  if (!current) return toast("先选一个群", true);
  // 取「已保存」的群规：转移是动数据，不能把页面上没保存的半成品推过去
  let saved = [];
  try {
    const d = await bridge.apiGet("mod/detail", { group: current });
    saved = (d && d.rules) || [];
  } catch (e) {
    return toast("读取本群群规失败：" + e.message, true);
  }
  if (!saved.length) return toast("本群还没有已保存的群规", true);
  const all = (state.status && state.status.known_groups) || [];
  const targets = all.filter((g) => g.group_openid !== current);
  if (!targets.length) return toast("没有其他群可选", true);

  const opts = targets.map((g) =>
    '<option value="' + esc(g.group_openid) + '">' + esc(groupLabelOf(g)) + '</option>').join("");
  const rows = saved.map((r, i) => {
    const rid = String(r.id || "");
    const label = (i + 1) + ". " + (r.name || "未命名规则")
      + "（" + _ruleTypeLabel(r.category) + "）｜禁言 " + fmtDur(r.seconds);
    return '<label class="field check" style="display:flex;gap:8px;align-items:flex-start;margin:2px 0">'
      + '<input type="checkbox" data-rid="' + esc(rid) + '"' + (rid ? " checked" : " disabled") + ' />'
      + '<span>' + esc(label) + '</span></label>';
  }).join("");

  const wrap = openOverlay("rules-transfer",
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
    + '<b>转移群规到其他群</b><button class="btn small" data-x="close">关闭</button></div>'
    + '<div class="desc" style="margin:0 0 8px">源群：<b>' + esc(groupLabelByOpenid(current) || "当前群")
    + '</b>（已保存 ' + saved.length + ' 条）。勾选要转移的规则，默认全选。</div>'
    + '<div style="display:flex;align-items:center;gap:8px;margin:0 0 6px">'
    + '<button class="btn small" id="transfer-all" type="button">全选 / 全不选</button>'
    + '<span class="desc">已选 <b id="transfer-count">0 / 0</b></span></div>'
    + '<div style="max-height:220px;overflow:auto;border:1px solid rgba(127,127,127,.25);'
    + 'border-radius:8px;padding:8px 10px">' + rows + '</div>'
    + '<label class="field" style="margin-top:10px"><span>目标群</span><select id="transfer-target">'
    + opts + '</select></label>'
    + '<label class="field check" style="margin-top:8px"><input type="radio" name="transfer-mode" value="append" checked />'
    + '<span>追加（保留目标群原有规则，id 自动去重）</span></label>'
    + '<label class="field check"><input type="radio" name="transfer-mode" value="replace" />'
    + '<span><b>覆盖</b>（先清空目标群原有规则，再写入勾选的，不可撤销）</span></label>'
    + '<div style="display:flex;gap:8px;margin-top:12px">'
    + '<button class="btn btn-primary small" data-x="ok">确认转移</button>'
    + '<button class="btn small" data-x="close2">取消</button></div>');

  const boxes = () => Array.from(wrap.querySelectorAll("input[data-rid]"));
  const pickedIds = () => boxes().filter((b) => b.checked).map((b) => b.dataset.rid).filter(Boolean);
  const syncCount = () => {
    const el = wrap.querySelector("#transfer-count");
    if (el) el.textContent = pickedIds().length + " / " + boxes().length;
  };
  wrap.querySelector("#transfer-all").onclick = () => {
    const bs = boxes().filter((b) => !b.disabled);
    const target = !bs.every((b) => b.checked);
    bs.forEach((b) => { b.checked = target; });
    syncCount();
  };
  boxes().forEach((b) => b.addEventListener("change", syncCount));
  syncCount();

  const close = () => wrap.remove();
  wrap.querySelector('[data-x="close"]').onclick = close;
  wrap.querySelector('[data-x="close2"]').onclick = close;
  wrap.querySelector('[data-x="ok"]').onclick = async () => {
    const to = wrap.querySelector("#transfer-target").value;
    const mode = (wrap.querySelector('input[name="transfer-mode"]:checked') || {}).value || "append";
    const ids = pickedIds();
    if (!to) return toast("先选目标群", true);
    if (!ids.length) return toast("至少勾选一条规则", true);
    if (mode === "replace" && !(await showConfirm("覆盖会先清空目标群现有的全部群规，再写入这 " + ids.length + " 条，确定继续？"))) return;
    try {
      const r = await bridge.apiPost("mod/rules/transfer", { from: current, to: to, mode: mode, ids: ids });
      if (r.ok === false) throw new Error(r.error || "转移失败");
      toast("已转移 " + r.count + " 条；目标群现有 " + r.total + " 条");
      close();
    } catch (e) {
      toast("转移失败：" + e.message, true);
    }
  };
}

async function saveSettings() {
  if (!current) return toast("先选一个群", true);
  try {
    await bridge.apiPost("mod/settings/save", { group: current, settings: collectSettings() });
    toast("已保存本群举报设置");
    await selectGroup(current);
  } catch (e) {
    toast("保存失败：" + e.message, true);
  }
}

async function saveTemplates() {
  if (!current) return toast("先选一个群", true);
  try {
    await bridge.apiPost("mod/templates/save", { scope: current, templates: collectTemplates() });
    toast("已保存本群话术");
    await selectGroup(current);
  } catch (e) {
    toast("保存失败：" + e.message, true);
  }
}

async function clearTemplates() {
  if (!current) return toast("先选一个群", true);
  const empty = { dim_names: {}, cat_names: {} };
  TPL_FIELDS.forEach(([k]) => { empty[k] = ""; });
  try {
    await bridge.apiPost("mod/templates/save", { scope: current, templates: empty });
    toast("已清空本群覆盖，恢复继承全局");
    await selectGroup(current);
  } catch (e) {
    toast("操作失败：" + e.message, true);
  }
}


/* ---------------- 初始化（并入 dashboard 页：点 tab 才拉数据，懒加载） ---------------- */
let modInited = false;

async function initModeration() {
  if (modInited) return;
  modInited = true;
  let b = window.AstrBotPluginPage;
  if (!b) {
    for (let i = 0; i < 20 && !b; i++) {
      await new Promise((r) => setTimeout(r, 150));
      b = window.AstrBotPluginPage;
    }
  }
  bridge = b;
  if (!bridge) {
    toast("bridge 未就绪，请刷新页面", true);
    return;
  }
  try { await bridge.ready(); } catch (e) { console.warn("bridge 未就绪", e); }

  document.querySelectorAll("#tab-moderation .mod-tab").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll("#tab-moderation .mod-tab").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll("#tab-moderation .mod-pane").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $("mod-pane-" + btn.dataset.modTab).classList.add("active");
    };
  });
  $("btn-refresh").onclick = () => loadGroups().catch((e) => toast("刷新失败：" + e.message, true));
  $("btn-rename-group").onclick = openNameEdit;
  $("btn-refresh-names").onclick = () => refreshNames({ all: true }).catch(() => {});
  $("btn-cancel-name").onclick = () => { $("name-edit").hidden = true; };
  $("btn-clear-name").onclick = () => saveName("");
  $("btn-save-name").onclick = () => saveName($("name-input").value);
  $("name-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveName($("name-input").value);
    if (e.key === "Escape") $("name-edit").hidden = true;
  });
  $("search").oninput = renderGroups;
  $("btn-add-rule").onclick = () => {
    if (!detail) return toast("先选一个群", true);
    detail.rules = (detail.rules || []).concat([{ name: "", desc: "", category: "ad", severity: "medium", seconds: 3600, enabled: true }]);
    renderRules();
  };
  $("btn-save-rules").onclick = saveRules;
  const _btnExport = $("btn-export-rules");
  if (_btnExport) _btnExport.onclick = exportRules;
  const _btnTransfer = $("btn-transfer-rules");
  if (_btnTransfer) _btnTransfer.onclick = transferRules;
  $("btn-save-settings").onclick = saveSettings;
  $("btn-save-templates").onclick = saveTemplates;
  $("btn-clear-templates").onclick = clearTemplates;
  $("btn-test").onclick = runTest;

  try {
    await loadGroups();
  } catch (e) {
    toast("加载群列表失败：" + e.message, true);
  }
}

// 供外层页签调用
window.__qqgmModerationLoad = () => { initModeration().catch(() => {}); };
// 概览页「去配置群规」：跳过来后自动选中该群
window.__qqgmSelectGroup = (g) => initModeration().then(() => selectGroup(g)).catch(() => {});
// 「记录」页的判定记录/试跑：需要先有「当前群」，没有就默认选第一个
window.__qqgmRecordsLoad = () => initModeration().then(() => {
  if (current) return null;
  const first = bots[0] && (bots[0].groups || [])[0];
  return first ? selectGroup(first.group) : null;
}).catch(() => {});
})();


// ---------------- ⚙️ 设置页：schema 驱动表单（与 AstrBot 配置双向同步） ----------------
const SET_ICONS = {
  basic: "🧩", admin: "🛡️", recall: "♻️", approval: "🚪",
  moderation: "📋", templates: "💬", member_event: "👋", log: "📜",
};
const setState = {
  sub: "general", // 子页签：general=常规设置 / templates=消息模板
  tplFormats: {}, // 模板 key -> {format, url, width, height}
  groups: [],     // 后端返回的 schema
  providers: [],
  reload: {},     // 需要重载插件才生效的项 {"group": ["key"]}
  fields: [],     // 表单字段注册表
  original: {},   // "group.key" -> 已保存的值
  dirty: {},      // "group.key" -> 当前编辑值（与 original 不同才有）
  collapsed: {},  // 分组折叠状态
  onlyDirty: false,
  keyword: "",
  loaded: false,
};

function setKeyOf(g, k) { return g + "." + k; }
function normVal(v) { return JSON.stringify(v === undefined ? null : v); }

function fmtDefault(v) {
  if (v === undefined || v === null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

// 按类型 + 当前值构建控件
const OPTION_LABELS = {
  any: "任一条件满足即可", all: "全部条件满足才行",
  DEBUG: "DEBUG（最详细）", INFO: "INFO（常规）", WARNING: "WARNING（警告）", ERROR: "ERROR（仅错误）",
};

function buildWidget(item, value) {
  const t = item.type;
  if (t === "bool") {
    const on = value === true || value === "true";
    return `<label class="switch">
      <input type="checkbox" ${on ? "checked" : ""} data-kind="bool" />
      <span class="track"></span>
      <span class="stext">${on ? "已开启" : "已关闭"}</span>
    </label>`;
  }
  if (item.special === "select_provider") {
    const opts = setState.providers.map((pr) =>
      `<option value="${esc(pr.id)}" ${String(value) === pr.id ? "selected" : ""}>${esc(pr.display)}</option>`).join("");
    return `<select data-kind="str">
      <option value="" ${!value ? "selected" : ""}>（留空＝跟随当前会话模型）</option>
      ${opts}
    </select>`;
  }
  if (item.options && item.options.length) {
    return `<select data-kind="str">${item.options.map((o) =>
      `<option value="${esc(o)}" ${String(value) === String(o) ? "selected" : ""}>${esc(OPTION_LABELS[o] || o)}</option>`).join("")}</select>`;
  }
  if (t === "list") {
    const text = Array.isArray(value) ? value.join("\n") : String(value || "");
    return `<textarea data-kind="list" rows="3" spellcheck="false" placeholder="每行一个，留空＝不限">${esc(text)}</textarea>`;
  }
  if (t === "int" || t === "float") {
    const step = t === "float" ? ' step="0.01"' : ' step="1"';
    return `<input type="number" data-kind="${t}"${step} value="${esc(value === undefined || value === null ? "" : value)}" />`;
  }
  if (t === "text") {
    const rows = Math.min(6, Math.max(2, String(value || "").split("\n").length));
    return `<textarea data-kind="str" rows="${rows}" spellcheck="false">${esc(value ?? "")}</textarea>`;
  }
  return `<input type="text" data-kind="str" value="${esc(value ?? "")}" />`;
}

// ---------------- 💬 消息模板：格式拨杆 + 编辑预览 ----------------
// 格式（text/markdown/image）存在插件侧车文件里，内容仍走 AstrBot 配置，
// 所以这里的任何编辑都不会改变配置结构，关掉页面也不影响已存内容。
const TPL_FMT_LABEL = { text: "纯文本", markdown: "Markdown", image: "图片" };
function fmtLabel(f) { return TPL_FMT_LABEL[f] || "纯文本"; }

// QQ 官方 markdown 子集渲染：只实现平台文档明确支持的语法，
// 不做"猜"，避免预览比实际好看、误导用户。
function renderQQMarkdown(src) {
  const escTxt = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const escAttr = (s) => escTxt(s).replace(/"/g, "&quot;");
  const inline = (s) => {
    let x = escTxt(s);
    x = x.replace(/!\[([^\]]*?)\s*(?:#(\d+)px)?\s*(?:#(\d+)px)?\]\(([^)\s]+)\)/g, (m, alt, w, h, url) => {
      const ww = w ? parseInt(w, 10) : 208;
      const hh = h ? parseInt(h, 10) : Math.round(ww * 1.54);
      // 示例里的占位域名加载不出来时，换成虚线框，别让示例看着像坏了
      const box = `width:${ww}px;height:${hh}px;border:1px dashed #d7dbe3;border-radius:8px;display:none;align-items:center;justify-content:center;color:#98a2b3;font-size:12px;margin:6px 0`;
      const img = `<img src="${escAttr(url)}" alt="${escAttr(alt)}" style="width:${ww}px;height:${hh}px;object-fit:cover;border-radius:8px;display:block;margin:6px 0" onerror="this.style.display='none';var p=this.nextElementSibling;if(p){p.style.display='flex'}">`;
      return `${img}<span style="${box}">${escAttr(alt) || "图片预览"}</span>`;
    });
    x = x.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    x = x.replace(/__(.+?)__/g, "<b><u>$1</u></b>");
    x = x.replace(/\*\*\*(.+?)\*\*\*/g, "<b><i>$1</i></b>");
    x = x.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
    x = x.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>");
    x = x.replace(/~~(.+?)~~/g, "<del>$1</del>");
    x = x.replace(/\u200B/g, "<br>");
    return x;
  };
  const lines = String(src || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let list = null;
  const flush = () => {
    if (list) {
      out.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join("")}</${list.tag}>`);
      list = null;
    }
  };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { flush(); continue; }
    if (/^\s*\*\*\*\s*$/.test(line)) { flush(); out.push("<hr>"); continue; }
    const h = line.match(/^(#{1,2})\s+(.*)$/);
    if (h) { flush(); out.push(`<div class="pv-h${h[1].length}">${inline(h[2])}</div>`); continue; }
    const q = line.match(/^\s*>\s?(.*)$/);
    if (q) { flush(); out.push(`<blockquote>${inline(q[1])}</blockquote>`); continue; }
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) { if (!list || list.tag !== "ol") { flush(); list = { tag: "ol", items: [] }; } list.items.push(inline(ol[1])); continue; }
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    if (ul) { if (!list || list.tag !== "ul") { flush(); list = { tag: "ul", items: [] }; } list.items.push(inline(ul[1])); continue; }
    flush();
    out.push(`<p>${inline(line)}</p>`);
  }
  flush();
  return out.join("") || '<span class="pv-warn">（空内容）</span>';
}

let _tplCur = null;

function setDlgFmt(fmt) {
  if (!_tplCur) return;
  _tplCur.format = fmt;
  const seg = $("tplDlgSeg");
  if (seg) seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.fmt === fmt));
  const row = $("tplImgRow");
  if (row) row.style.display = fmt === "image" ? "" : "none";
  renderTplPreview();
}

// 在光标处插入模块占位符（不覆盖已有选择内容时替换选中段，符合常规编辑器直觉）
function insertTplModule(snippet) {
  const ta = $("tplDlgText");
  if (!ta) return;
  const start = typeof ta.selectionStart === "number" ? ta.selectionStart : ta.value.length;
  const end = typeof ta.selectionEnd === "number" ? ta.selectionEnd : start;
  ta.value = ta.value.slice(0, start) + snippet + ta.value.slice(end);
  const pos = start + snippet.length;
  ta.focus();
  try { ta.setSelectionRange(pos, pos); } catch (err) { /* 某些浏览器的老内核不支持，忽略 */ }
  renderTplPreview();
}

// 预览里的内置模块：真机会展开成 @标签 / 头像图；面板拿不到成员 openid，
// 这里按等效尺寸画占位块，让作者知道「会长在哪个位置、占多大」。
function previewModules(html) {
  return String(html)
    .replace(/\{at\}/g, '<span class="md-at">@群员</span>')
    .replace(/\{avatar(?::(\d+))?\}/g, (m, d) => {
      const s = Math.max(24, Math.min(parseInt(d, 10) || 100, 320));
      return `<span class="md-avatar" style="width:${s}px;height:${s}px">头像</span>`;
    });
}

function renderTplPreview() {
  const box = $("tplPreview");
  if (!box) return;
  const fmt = _tplCur ? _tplCur.format : "text";
  const text = $("tplDlgText").value;
  if (fmt === "image") {
    const url = $("tplImgUrl").value.trim();
    const h = parseInt($("tplImgH").value, 10) || 0;
    const cap = text.trim() ? `<div class="pv-cap">${esc(text)}</div>` : "";
    box.innerHTML = url
      ? `<img src="${esc(url)}" style="max-width:100%;border-radius:10px;${h ? `max-height:${h}px;` : ""}object-fit:cover;display:block" onerror="this.outerHTML='<div class=&quot;pv-warn&quot;>图片加载失败，请检查地址</div>'">${cap}`
      : '<div class="pv-warn">还没填图片地址</div>';
    return;
  }
  if (fmt === "text") {
    box.innerHTML = `<div class="pv-text">${previewModules(esc(text).replace(/\n/g, "<br>"))}</div>`;
    return;
  }
  box.innerHTML = `<div class="pv-md">${previewModules(renderQQMarkdown(text))}</div>`;
}

function openTplDialog(key) {
  const meta = setState.tplFormats[key] || {};
  const field = setState.fields.find((f) => f.key === "templates." + key);
  const item = field ? field.item : { description: key, default: "", hint: "" };
  _tplCur = { key, item, format: meta.format || "text" };
  $("tplDlgTitle").textContent = item.description || key;
  $("tplDlgHint").textContent = (item.hint ? item.hint + " · " : "") +
    "变量用花括号包裹，如 {user}；{at} 会 @ 该成员，{avatar} 带头像（需 Markdown 格式）" +
    (key === "msg_join_request_notify"
      ? " ⚠️ 这条消息可以带「通过/拒绝」按钮，而按钮只在 Markdown 消息里渲染：选了纯文本/图片会没有按钮（插件会自动改用 Markdown 发送；若被平台拒绝则退回纯文本并只剩文字提示）。"
      : "");
  $("tplDlgText").value = field ? field.get() : (meta.value || "");
  $("tplImgUrl").value = meta.url || "";
  $("tplImgW").value = meta.width || "";
  $("tplImgH").value = meta.height || "";
  setDlgFmt(_tplCur.format);
  const dlg = $("tplDlg");
  if (!dlg) return;
  if (typeof dlg.showModal === "function") { if (!dlg.open) dlg.showModal(); }
  else dlg.setAttribute("open", "");
}

function closeTplDialog() {
  const dlg = $("tplDlg");
  if (dlg && dlg.open) dlg.close();
  _tplCur = null;
}

async function saveTplDialog() {
  if (!_tplCur) return;
  const key = _tplCur.key;
  const payload = {
    key,
    type: _tplCur.format,
    value: $("tplDlgText").value,
    url: $("tplImgUrl").value.trim(),
    width: parseInt($("tplImgW").value, 10) || 0,
    height: parseInt($("tplImgH").value, 10) || 0,
  };
  const btn = $("tplDlgSave");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    const r = await bridge.apiPost("templates/formats/save", payload);
    if (!r.ok) throw new Error((r.errors || []).join("；") || r.message || "保存失败");
    const meta = setState.tplFormats[key] = Object.assign({}, setState.tplFormats[key] || {}, {
      format: payload.type, url: payload.url, width: payload.width, height: payload.height,
    });
    // 同步表单状态：这一项已落盘，不能再算"未保存改动"
    const field = setState.fields.find((f) => f.key === "templates." + key);
    if (field) {
      field.item.value = payload.value;
      setState.original[field.key] = payload.value;
      delete setState.dirty[field.key];
      field.set(payload.value);
      field.refresh();
    }
    updateDirtyBar();
    showToast(`已保存：${_tplCur.item.description || key}（${fmtLabel(meta.format)}）`);
    closeTplDialog();
  } catch (e) {
    showToast("保存失败: " + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "保存此项";
  }
}

function buildItem(item, group) {
  const key = setKeyOf(group.key, item.key);
  const box = document.createElement("div");
  box.className = "set-item";
  box.dataset.group = group.key;
  box.dataset.key = item.key;
  const desc = item.description;
  const hint = item.hint || "";
  box.dataset.search = (item.description + " " + desc + " " + item.key + " " + (item.hint || "") + " " + hint).toLowerCase();
  if (hint) box.title = hint;   // 开关行里放不下说明文字，悬浮看
  const needsReload = (setState.reload[group.key] || []).includes(item.key);
  box.innerHTML = `
    <div class="set-label">
      <span class="dot"></span>
      <span>${esc(desc)}</span>
      <button class="set-reset" title="恢复为默认值：${esc(fmtDefault(item.default).slice(0, 60))}">↺ 默认</button>
    </div>
    ${buildWidget(item, item.value)}
    <div class="set-hint">${hint ? esc(hint) : ""}${hint && needsReload ? " · " : ""}${needsReload ? "<b>改完需重载插件生效</b>" : ""}</div>`;
  box.dataset.needsReload = needsReload ? "1" : "";
  const ctrl = box.querySelector("[data-kind]");
  const stext = box.querySelector(".stext");
  const get = () => {
    const kind = ctrl.dataset.kind;
    if (kind === "bool") return ctrl.checked;
    if (kind === "list") return ctrl.value.split(/[\n,，;；]+/).map((x) => x.trim()).filter(Boolean);
    if (kind === "int") return ctrl.value === "" ? null : parseInt(ctrl.value, 10);
    if (kind === "float") return ctrl.value === "" ? null : parseFloat(ctrl.value);
    return ctrl.value;
  };
  const set = (v) => {
    const kind = ctrl.dataset.kind;
    if (kind === "bool") { ctrl.checked = v === true; if (stext) stext.textContent = ctrl.checked ? "已开启" : "已关闭"; }
    else if (kind === "list") ctrl.value = Array.isArray(v) ? v.join("\n") : String(v ?? "");
    else ctrl.value = v === undefined || v === null ? "" : v;
  };
  const refresh = () => {
    const changed = normVal(get()) !== normVal(setState.original[key]);
    box.classList.toggle("dirty", changed);
    if (changed) setState.dirty[key] = get(); else delete setState.dirty[key];
    updateDirtyBar();
  };
  ctrl.addEventListener("input", refresh);
  ctrl.addEventListener("change", () => {
    if (stext && ctrl.dataset.kind === "bool") stext.textContent = ctrl.checked ? "已开启" : "已关闭";
    refresh();
  });
  box.querySelector(".set-reset").addEventListener("click", () => {
    set(item.default);
    refresh();
    showToast(`已填回默认值（还没保存）：${item.description}`);
  });
  // 消息模板这一组额外挂「格式拨杆 + 编辑预览」，格式本身存侧车文件
  const fmtMeta = group.key === "templates" ? setState.tplFormats[item.key] : null;
  if (fmtMeta) {
    const bar = document.createElement("div");
    bar.className = "tpl-fmtbar";
    bar.innerHTML = `
      <div class="seg sm tpl-fmtseg">
        <button data-fmt="text" class="${fmtMeta.format === "text" ? "on" : ""}">纯文本</button>
        <button data-fmt="markdown" class="${fmtMeta.format === "markdown" ? "on" : ""}">Markdown</button>
        <button data-fmt="image" class="${fmtMeta.format === "image" ? "on" : ""}">图片</button>
      </div>
      <button class="btn ghost small tpl-editbtn">✎ 编辑与预览</button>`;
    const hintEl = box.querySelector(".set-hint");
    box.insertBefore(bar, hintEl);
    bar.querySelectorAll("[data-fmt]").forEach((b) => {
      b.addEventListener("click", async () => {
        const fmt = b.dataset.fmt;
        bar.querySelectorAll("[data-fmt]").forEach((x) => x.classList.toggle("on", x === b));
        const cur = setState.tplFormats[item.key] = Object.assign({}, fmtMeta, { format: fmt });
        try {
          const r = await bridge.apiPost("templates/formats/save", {
            key: item.key, type: fmt,
            url: cur.url || "", width: cur.width || 0, height: cur.height || 0,
          });
          if (!r.ok) throw new Error((r.errors || []).join("；") || r.message || "保存失败");
          showToast(`${item.description}：已设为${fmtLabel(fmt)}`);
          if (fmt === "image" && !cur.url) openTplDialog(item.key);  // 选图片但没地址，直接引导去填
        } catch (e) {
          showToast("格式保存失败: " + e.message);
        }
      });
    });
    bar.querySelector(".tpl-editbtn").addEventListener("click", () => openTplDialog(item.key));
  }

  setState.original[key] = item.value;
  setState.fields.push({ key, group: group.key, item, box, get, set, refresh });
  return box;
}

function renderSettings() {
  const wrap = $("setGroups");
  const frag = document.createDocumentFragment();
  setState.fields = [];
  setState.original = {};
  setState.dirty = {};

  const onlyTpl = setState.sub === "templates";
  // 「Markdown 大全」入口只在消息模板子页签出现，别的子页签不需要它占地方
  const mdBar = $("tplMdBar");
  if (mdBar) mdBar.style.display = onlyTpl ? "" : "none";
  setState.groups
    .filter((g) => (g.key === "templates") === onlyTpl)
    .forEach((group) => {
    const sec = document.createElement("div");
    sec.className = "set-group" + (setState.collapsed[group.key] ? " collapsed" : "");
    sec.dataset.group = group.key;
    const head = document.createElement("div");
    head.className = "set-group-head";
    head.innerHTML = `
      <span class="arrow">▼</span>
      <span>${SET_ICONS[group.key] || "⚙️"} ${esc(group.description)}</span>
      <span class="gcount" data-total="${group.items.length}">${group.items.length} 项<b></b></span>`;
    head.addEventListener("click", () => {
      sec.classList.toggle("collapsed");
      setState.collapsed[group.key] = sec.classList.contains("collapsed");
    });
    sec.appendChild(head);

    const body = document.createElement("div");
    body.className = "set-group-body";

    // 「消息模板」按描述前缀（通用 · 禁言 · 撤回 …）分子组，其余分组直接铺开
    const isTpl = group.key === "templates";
    let lastSub = null;
    let grid = null;
    let switchRow = null;      // 当前攒着的开关行
    group.items.forEach((item) => {
      if (isTpl) {
        const sub = String(item.description).split("·")[0].trim() || "其他";
        if (sub !== lastSub) {
          const h = document.createElement("div");
          h.className = "set-subtitle";
          h.textContent = "— " + sub + " —";
          h.dataset.sub = sub;
          body.appendChild(h);
          grid = document.createElement("div");
          grid.className = "set-items";
          grid.dataset.sub = sub;
          body.appendChild(grid);
          switchRow = null;
          lastSub = sub;
        }
      }

      // 开关（bool）单独成行：挤在输入框网格里会被高项撑开、留下大段空白，看着上下黏连
      if (item.type === "bool") {
        if (!switchRow) {
          switchRow = document.createElement("div");
          switchRow.className = "switch-row";
          (isTpl && grid ? grid : body).appendChild(switchRow);
        }
        switchRow.appendChild(buildItem(item, group));
        return;
      }
      switchRow = null;
      // 开关行打断了网格，后面的输入项要另起一个，否则 DOM 顺序会插到开关前面
      if (!grid || grid !== body.lastElementChild) {
        grid = document.createElement("div");
        grid.className = "set-items";
        body.appendChild(grid);
      }
      grid.appendChild(buildItem(item, group));
    });
    sec.appendChild(body);
    frag.appendChild(sec);
  });

  wrap.innerHTML = "";
  wrap.appendChild(frag);
  setState.loaded = true;
  updateDirtyBar();
  applyVisibility();
}

function updateDirtyBar() {
  const n = Object.keys(setState.dirty).length;
  const info = $("setDirtyInfo");
  if (info) info.innerHTML = `已改动 <b>${n}</b> 项`;
  const bar = $("setSaveBar");
  if (bar) bar.classList.toggle("show", n > 0);
  const c = $("setDirtyCount");
  if (c) c.textContent = n;
  // 分组头部的改动计数
  document.querySelectorAll(".set-group").forEach((sec) => {
    const cnt = Object.keys(setState.dirty).filter((k) => k.startsWith(sec.dataset.group + ".")).length;
    const b = sec.querySelector(".gcount b");
    if (b) b.textContent = cnt ? ` · 改动 ${cnt}` : "";
  });
  if ($("setOnlyDirty")) applyVisibility();
}

// 「只看已改动」+ 搜索的联合过滤（不改 DOM，只切显隐）
function applyVisibility() {
  const kw = setState.keyword.trim().toLowerCase();
  const onlyDirty = setState.onlyDirty;
  let totalVisible = 0;
  document.querySelectorAll(".set-group").forEach((sec) => {
    let visible = 0;
    sec.querySelectorAll(".set-item").forEach((box) => {
      const hitKw = !kw || box.dataset.search.includes(kw);
      const hitDirty = !onlyDirty || box.classList.contains("dirty");
      const show = hitKw && hitDirty;
      box.style.display = show ? "" : "none";
      if (show) { visible++; totalVisible++; }
    });
    // 开关行：里面的项全被过滤掉时整行隐藏，不然会留一条空白
    sec.querySelectorAll(".switch-row").forEach((row) => {
      const any = [...row.children].some((c) => c.style.display !== "none");
      row.style.display = any ? "" : "none";
    });
    // 子分组标题：该子组下没有可见项就一起隐藏
    sec.querySelectorAll(".set-subtitle").forEach((h) => {
      const grid = h.nextElementSibling;
      const any = grid && [...grid.children].some((c) => c.style.display !== "none");
      h.style.display = any ? "" : "none";
      if (grid) grid.style.display = any ? "" : "none";
    });
    sec.style.display = visible ? "" : "none";
    const gc = sec.querySelector(".gcount");
    if (gc) {
      const total = Number(gc.dataset.total || 0);
      gc.textContent = (kw || onlyDirty) && visible !== total
        ? `${visible} / ${total} 项`
        : `${total} 项`;
    }
  });
  const noMatch = $("setNoMatch");
  if (noMatch) noMatch.style.display = totalVisible ? "none" : "";
}
async function loadSettings(force) {
  if (setState.loaded && !force) return;
  const wrap = $("setGroups");
  wrap.innerHTML = emptyBox("load", "加载中…");
  try {
    const [r, tpl] = await Promise.all([
      bridge.apiGet("config/schema"),
      bridge.apiGet("templates/formats").catch(() => ({ templates: [] })),
    ]);
    setState.groups = r.groups || [];
    setState.providers = r.providers || [];
    setState.reload = r.reload_required || {};
    setState.tplFormats = {};
    (tpl.templates || []).forEach((t) => { setState.tplFormats[t.key] = t; });
    renderSettings();
    if (force) showToast("已重新读取当前配置");
  } catch (e) {
    wrap.innerHTML = emptyBox("warn", "配置加载失败: " + esc(e.message));
  }
}

// 收集改动 → 批量保存
async function saveSettings() {
  const values = {};
  Object.keys(setState.dirty).forEach((k) => {
    const idx = k.indexOf(".");
    const g = k.slice(0, idx), key = k.slice(idx + 1);
    (values[g] = values[g] || {})[key] = setState.dirty[k];
  });
  const n = Object.keys(setState.dirty).length;
  if (!n) { showToast("没有要保存的改动"); return; }
  const btn = $("btnSetSave");
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    const r = await bridge.apiPost("config/save", { values });
    if (!r.ok) { showToast(`保存失败: ${r.message || "未知错误"}`); return; }
    // 用后端返回的实际值回填（类型已被规范化）
    Object.keys(r.applied || {}).forEach((g) => {
      Object.keys(r.applied[g]).forEach((key) => {
        setState.original[setKeyOf(g, key)] = r.applied[g][key];
      });
    });
    setState.fields.forEach((f) => {
      const k = f.key;
      if (setState.original[k] !== undefined) f.set(setState.original[k]);
      delete setState.dirty[k];
      f.refresh();
    });
    const needReload = Object.keys(r.applied || {}).some((g) => (setState.reload[g] || []).length);
    showToast(r.message + (needReload ? "；部分项需重载插件生效" : ""));
    if (r.errors && r.errors.length) showToast("未保存：" + r.errors.join("；"));
    updateDirtyBar();
    loadStatus();
  } catch (e) {
    showToast(`保存失败: ${e.message}`);
  } finally {
    btn.disabled = false; btn.textContent = "💾 保存全部改动";
  }
}

function discardSettings() {
  setState.fields.forEach((f) => { f.set(setState.original[f.key]); delete setState.dirty[f.key]; f.refresh(); });
  updateDirtyBar();
  showToast("已放弃未保存的改动");
}

// 设置页控件绑定
const _setSearch = $("setSearch");
if (_setSearch) _setSearch.addEventListener("input", () => {
  setState.keyword = _setSearch.value;
  applyVisibility();
});
const _setOnly = $("setOnlyDirty");
if (_setOnly) _setOnly.addEventListener("change", () => {
  setState.onlyDirty = _setOnly.checked;
  applyVisibility();
});
if ($("btnSetExpand")) $("btnSetExpand").addEventListener("click", () => {
  document.querySelectorAll(".set-group").forEach((sec) => {
    sec.classList.remove("collapsed");
    setState.collapsed[sec.dataset.group] = false;
  });
});
if ($("btnSetCollapse")) $("btnSetCollapse").addEventListener("click", () => {
  document.querySelectorAll(".set-group").forEach((sec) => {
    sec.classList.add("collapsed");
    setState.collapsed[sec.dataset.group] = true;
  });
});
if ($("btnSetReload")) $("btnSetReload").addEventListener("click", async () => {
  if (Object.keys(setState.dirty).length) {
    const okGo = await showConfirm("还有未保存的改动，重新读取会丢弃它们。确定继续？");
    if (!okGo) return;
  }
  loadSettings(true);
});
if ($("btnSetSave")) $("btnSetSave").addEventListener("click", saveSettings);
if ($("btnSetDiscard")) $("btnSetDiscard").addEventListener("click", discardSettings);

// 消息模板页的「Markdown 怎么写？」按钮：直接开帮助中心的 Markdown 大全
if ($("tplMdHelp")) $("tplMdHelp").addEventListener("click", (e) => {
  e.preventDefault();
  openHelp("md", "start");
});

// 消息模板：子页签切换 + 编辑对话框
const _setSubSeg = $("setSeg");
if (_setSubSeg) _setSubSeg.addEventListener("click", (e) => {
  const b = e.target.closest("button[data-sub]");
  if (!b || b.dataset.sub === setState.sub) return;
  setState.sub = b.dataset.sub;
  _setSubSeg.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  renderSettings();
});
(function bindTplDialog() {
  const dlg = $("tplDlg");
  if (!dlg) return;
  const seg = $("tplDlgSeg");
  if (seg) seg.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-fmt]");
    if (b) setDlgFmt(b.dataset.fmt);
  });
  ["tplDlgText", "tplImgUrl", "tplImgW", "tplImgH"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", renderTplPreview);
  });
  const modRow = $("tplModRow");
  if (modRow) modRow.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mod]");
    if (b) insertTplModule(b.dataset.mod);
  });
  const save = $("tplDlgSave");
  if (save) save.addEventListener("click", saveTplDialog);
  const close = $("tplDlgClose");
  if (close) close.addEventListener("click", closeTplDialog);
  const reset = $("tplDlgReset");
  if (reset) reset.addEventListener("click", () => {
    if (!_tplCur) return;
    $("tplDlgText").value = _tplCur.item.default || "";
    renderTplPreview();
    showToast("已填回默认值（还没保存）");
  });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) closeTplDialog(); });
})();

// ---------------- 词典（词库） ----------------
// 内置词库来自公开开源词库（插件目录 lexicons/SOURCES.md），页面只管：看、开关、上传、白名单、测试。
const LEX_CATS = { ad: "广告引流", abuse: "辱骂攻击", porn: "色情低俗", illegal: "违法违规", politics: "涉政敏感", spam: "刷屏灌水", other: "其他" };
const lexState = { list: [], stats: {}, allow: [], manualAllow: [], detailId: "", detail: null, upstream: {}, fetchDirty: false };
const lexCatName = (k) => LEX_CATS[k] || k || "其他";
// 词条数上万的缩写交给 Intl：中文出「1.2万」，英文出「12K」，不用在 JS 里写死量词
const lexNum = (n) => {
  const v = Number(n) || 0;
  if (v < 10000) return String(v);
  try {
    return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(v);
  } catch (e) {
    return Math.round(v / 1000) + "K";
  }
};
const lexFind = (id) => lexState.list.find((x) => x.id === id);

async function loadLexicons(refresh) {
  let r;
  try {
    r = await bridge.apiGet("lexicons", refresh ? { refresh: 1 } : {});
  } catch (e) {
    $("lex-rows").innerHTML = `<tr><td colspan="6">${emptyBox("warn", "加载失败：" + esc(e.message))}</td></tr>`;
    return;
  }
  if (!r || r.error) {
    $("lex-rows").innerHTML = `<tr><td colspan="6">${emptyBox("warn", "加载失败：" + esc((r && r.error) || "未知错误"))}</td></tr>`;
    return;
  }
  lexState.list = r.lexicons || [];
  lexState.stats = r.stats || {};
  lexState.allow = r.allow || [];
  lexState.manualAllow = r.manual_allow || [];
  lexState.upstream = r.upstream || {};
  fillLexSelects(r.categories || []);
  if (!lexState.fetchDirty) {
    $("lex-fetch-url").value = lexState.upstream.url || "";
    $("lex-fetch-mirror").value = lexState.upstream.mirror || "";
    $("lex-fetch-force").checked = !!lexState.upstream.force_mirror;
    $("lex-fetch-auto").checked = !!lexState.upstream.auto_enable;
  }
  renderLexStats();
  renderLexRows();
  renderLexAllow();
}

function renderLexStats() {
  const s = lexState.stats || {};
  const items = [
    ["启用词库", `${s.enabled_count || 0} / ${s.lexicon_count || 0}`],
    ["生效词条", lexNum(s.pattern_count)],
    ["其中字面量", lexNum(s.literal_count)],
    ["其中正则", lexNum(s.regex_count)],
    ["自定义词库", s.custom_count || 0],
    ["白名单", s.allow_count || 0],
    ["解析告警", (s.error_count || 0) + (s.warning_count || 0)],
  ];
  $("lex-stats").innerHTML = items.map(([k, v]) =>
    `<div class="lex-stat"><b>${esc(v)}</b><span>${esc(k)}</span></div>`).join("");
  $("lex-count").textContent = `共 ${s.lexicon_count || 0} 个（内置 ${s.builtin_count || 0} / 上传 ${s.custom_count || 0}）`;
}

function renderLexRows() {
  const rows = lexState.list.map((x) => {
    const tags = [];
    tags.push(x.source === "builtin" ? `<span class="lex-tag">内置</span>` : `<span class="lex-tag ok">本地上传</span>`);
    if (x.kind === "allow") tags.push(`<span class="lex-tag ok">白名单库</span>`);
    if (x.default_enabled === false) tags.push(`<span class="lex-tag warn">默认关</span>`);
    if (x.error_count) tags.push(`<span class="lex-tag bad">${x.error_count} 条解析失败</span>`);
    if (x.warning_count) tags.push(`<span class="lex-tag warn">${x.warning_count} 条风险提示</span>`);
    const cats = Object.entries(x.cat_counts || {})
      .map(([k, v]) => `<span class="lex-tag">${esc(lexCatName(k))} ${v}</span>`).join("") || `<span class="lex-tag">未分类</span>`;
    const ops = [
      `<button class="btn small" data-act="view">查看</button>`,
      `<button class="btn small" data-act="export">导出</button>`,
    ];
    if (x.source === "custom") {
      ops.push(`<button class="btn small" data-act="rename">改名</button>`);
      ops.push(`<button class="btn small danger" data-act="del">删除</button>`);
    }
    return `<tr data-id="${esc(x.id)}" class="${x.enabled ? "" : "lex-off"}">
      <td><label class="switch">
        <input type="checkbox" data-act="toggle" ${x.enabled ? "checked" : ""} />
        <span class="track"></span><span class="stext">${x.enabled ? "启用" : "停用"}</span>
      </label></td>
      <td class="lex-name-cell">
        <div class="lex-name">${esc(x.name)}</div>
        <div class="lex-desc">${esc(x.description || "（无说明）")}</div>
        <div>${tags.join("")}</div>
      </td>
      <td>${cats}</td>
      <td>${lexNum(x.count)}<div class="lex-desc">字面量 ${lexNum(x.literal_count)}${x.regex_count ? ` · 正则 ${x.regex_count}` : ""}</div></td>
      <td>${esc(x.author || "—")}<div class="lex-desc">${esc((x.sources || [])[0] || "")}</div></td>
      <td>${ops.join("")}</td>
    </tr>`;
  });
  $("lex-rows").innerHTML = rows.join("") || `<tr><td colspan="6">${emptyBox("book", "还没有任何词库")}</td></tr>`;
}

function renderLexAllow() {
  const box = $("lex-allow-chips");
  const manual = new Set(lexState.manualAllow || []);
  if (!lexState.allow.length) {
    box.innerHTML = `<span class="lex-hint">（白名单为空）</span>`;
    return;
  }
  box.innerHTML = lexState.allow.map((w) => manual.has(w)
    ? `<span class="lex-chip">${esc(w)}<button data-act="allow-del" data-word="${esc(w)}" title="从白名单移除">✕</button></span>`
    : `<span class="lex-chip" title="来自白名单词库">${esc(w)}<span class="lex-hint">·</span></span>`).join("");
}

function fillLexSelects(categories) {
  const catSel = $("lex-up-cat");
  const keep = catSel.value;
  const opts = (categories.length ? categories : Object.entries(LEX_CATS).map(([key, name]) => ({ key, name })))
    .map((c) => `<option value="${esc(c.key)}">${esc(c.name)}（${esc(c.key)}）</option>`).join("");
  catSel.innerHTML = opts;
  catSel.value = keep && catSel.querySelector(`option[value="${CSS.escape(keep)}"]`) ? keep : "ad";
  const fetchCat = $("lex-fetch-cat");
  const keepF = fetchCat.value || (lexState.upstream && lexState.upstream.category) || "other";
  fetchCat.innerHTML = opts;
  fetchCat.value = fetchCat.querySelector(`option[value="${CSS.escape(keepF)}"]`) ? keepF : "other";
  const tgt = $("lex-up-target");
  const keepT = tgt.value;
  tgt.innerHTML = lexState.list.filter((x) => x.source === "custom")
    .map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("") ||
    `<option value="">（还没有上传过词库）</option>`;
  if (keepT) tgt.value = keepT;
}

// —— 列表操作 ——
$("lex-rows").addEventListener("change", async (e) => {
  const cb = e.target.closest('input[data-act="toggle"]');
  if (!cb) return;
  const id = cb.closest("tr").dataset.id;
  cb.disabled = true;
  try {
    const r = await bridge.apiPost("lexicons/toggle", { id, enabled: cb.checked });
    if (r && r.ok) {
      const item = lexFind(id);
      if (item) item.enabled = cb.checked;
      lexState.stats = r.stats || lexState.stats;
      renderLexStats();
      renderLexRows();
      showToast(cb.checked ? "已启用" : "已停用");
    } else {
      showToast("操作失败：" + ((r && r.error) || "未知错误"));
    }
  } catch (err) {
    showToast("操作失败：" + err.message);
  } finally {
    cb.disabled = false;
  }
});

$("lex-rows").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const tr = btn.closest("tr");
  const id = tr.dataset.id;
  const act = btn.dataset.act;
  const item = lexFind(id);
  if (act === "view") return lexShowDetail(id);
  if (act === "export") return lexExport(id);
  if (act === "del") {
    if (!item) return;
    const okGo = await showConfirm(`删除词库「${item.name}」？该词库的 ${item.count} 条规则会一起消失（内置词库请用「停用」而不是删除）。`);
    if (!okGo) return;
    const r = await bridge.apiPost("lexicons/delete", { id });
    if (r && r.ok) { showToast("已删除"); loadLexicons(); } else showToast("删除失败：" + ((r && r.error) || "未知"));
    return;
  }
  if (act === "rename") {
    tr.querySelector(".lex-name-cell").innerHTML =
      `<input type="text" class="lex-rename-input" maxlength="40" value="${esc(item ? item.name : "")}" />
       <div style="margin-top:6px">
         <button class="btn small btn-primary" data-act="rename-save">保存</button>
         <button class="btn small btn-ghost" data-act="rename-cancel">取消</button>
       </div>`;
    return;
  }
  if (act === "rename-cancel") return renderLexRows();
  if (act === "rename-save") {
    const name = (tr.querySelector(".lex-rename-input").value || "").trim();
    if (!name) return showToast("名称不能为空");
    const r = await bridge.apiPost("lexicons/rename", { id, name });
    if (r && r.ok) { showToast("已改名"); loadLexicons(); } else showToast("改名失败：" + ((r && r.error) || "未知"));
    return;
  }
  if (act === "allow-del") {
    const w = btn.dataset.word;
    const r = await bridge.apiPost("lexicons/allow", { action: "remove", words: [w] });
    if (r && r.ok) { await loadLexicons(); showToast(`已移出白名单：${w}`); }
  }
});

// —— 批量开关 ——
$("btn-lex-refresh").addEventListener("click", () => { loadLexicons(true); showToast("已重新扫描词库目录"); });
$("btn-lex-on").addEventListener("click", async () => {
  const ids = lexState.list.map((x) => x.id);
  const okGo = await showConfirm(`启用全部 ${ids.length} 个词库？词条越多误伤概率越高，建议先做命中测试。`);
  if (!okGo) return;
  const r = await bridge.apiPost("lexicons/apply", { ids });
  if (r && r.ok) { loadLexicons(); showToast("已全部启用"); } else showToast("操作失败：" + ((r && r.error) || "未知"));
});
$("btn-lex-default").addEventListener("click", async () => {
  const ids = lexState.list.filter((x) => x.source === "builtin" && x.default_enabled !== false).map((x) => x.id);
  const r = await bridge.apiPost("lexicons/apply", { ids });
  if (r && r.ok) { loadLexicons(); showToast(`已恢复内置默认（${ids.length} 个）`); } else showToast("操作失败：" + ((r && r.error) || "未知"));
});
$("btn-lex-off").addEventListener("click", async () => {
  const okGo = await showConfirm("停用全部词库？关键词维度之后只靠代码内置套路正则和本群自定义关键词。");
  if (!okGo) return;
  const r = await bridge.apiPost("lexicons/apply", { ids: [] });
  if (r && r.ok) { loadLexicons(); showToast("已全部停用"); } else showToast("操作失败：" + ((r && r.error) || "未知"));
});

// —— 详情 / 预览 ——
async function lexShowDetail(id) {
  let r;
  try {
    r = await bridge.apiGet("lexicons/detail", { id, preview: 300 });
  } catch (e) {
    return showToast("打开失败：" + e.message);
  }
  if (!r || r.error) return showToast("打开失败：" + ((r && r.error) || "未知错误"));
  const x = r.lexicon;
  lexState.detailId = id;
  lexState.detail = x;
  $("lex-detail-title").textContent = `${x.name} · 词库统计（不展示词条内容）`;
  const head = [
    `<div class="lex-desc" style="max-width:none;margin-bottom:8px">${esc(x.description || "（无说明）")}</div>`,
    `<div><span class="lex-tag">共 ${lexNum(x.count)} 条</span><span class="lex-tag">字面量 ${lexNum(x.literal_count)}</span>`,
    `<span class="lex-tag">正则 ${lexNum(x.regex_count)}</span>`,
    x.source === "builtin" ? `<span class="lex-tag">内置（不可删/改名）</span>` : `<span class="lex-tag ok">本地上传</span>`,
    `<span class="lex-tag">${x.enabled ? "当前启用" : "当前停用"}</span></div>`,
    (x.errors || []).map((e2) => `<div class="lex-tag bad">${esc(e2)}</div>`).join(""),
    (x.warnings || []).map((w) => `<div class="lex-tag warn">${esc(w)}</div>`).join(""),
  ].join("");
  // 词条内容一律不下发也不渲染：只按「类型 + 条数 + 词长分布」给统计，
  // 需要逐条核对时用「导出」把词库文件下载到本地查看。
  const catHtml = Object.entries(x.cat_counts || {}).map(([cat, n]) => `<div class="lex-detail-cat">
      <h4>${esc(lexCatName(cat))}（${lexNum(n)} 条）</h4>
      <div class="lex-desc">词条内容已隐藏，不在面板展示。</div>
    </div>`).join("");
  const dist = Object.entries(x.length_dist || {});
  const distHtml = dist.length ? `<div class="lex-detail-cat">
      <h4>词长分布（只给长度，不列词条）</h4>
      <div class="lex-words">${dist.map(([len, n]) => `<span class="lex-word">${esc(len)} 字 · ${lexNum(n)} 条</span>`).join("")}</div>
    </div>` : "";
  $("lex-detail-body").innerHTML = head
    + (x.notice ? `<div class="lex-hint" style="margin:8px 0">📄 用途声明：${esc(x.notice)}</div>` : "")
    + `<div class="lex-hint" style="margin:8px 0">🔒 为避免在界面上摊开敏感词，面板不展示任何词条内容；需要逐条核对请点「导出」下载文件后本地查看。</div>`
    + catHtml + distHtml;
  $("lex-detail-card").hidden = false;
  $("lex-detail-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
}
$("btn-lex-detail-close").addEventListener("click", () => { $("lex-detail-card").hidden = true; });
$("btn-lex-detail-export").addEventListener("click", () => lexState.detailId && lexExport(lexState.detailId));

async function lexExport(id) {
  const r = await bridge.apiGet("lexicons/export", { id });
  if (!r || !r.ok) return showToast("导出失败：" + ((r && r.error) || "未知错误"));
  const name = r.filename || "lexicon.json";
  try {
    const blob = new Blob([r.content], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
    showToast(`已导出 ${name}`);
  } catch (e) {
    // 面板不做内容回显：下载被拦截时只提示去服务器取文件
    showToast("下载被浏览器拦截，请到插件目录 lexicons/ 取 " + name);
  }
}

// —— 上传 ——
$("lex-up-mode").addEventListener("change", () => {
  const m = $("lex-up-mode").value;
  $("lex-up-target-wrap").hidden = m === "new";
});
const lexReadFile = (file) => new Promise((res, rej) => {
  const fr = new FileReader();
  fr.onload = () => res(String(fr.result || ""));
  fr.onerror = () => rej(fr.error);
  fr.readAsText(file, "utf-8");
});
async function lexUploadText() {
  const file = $("lex-up-file").files[0];
  let text = $("lex-up-text").value;
  if (!text.trim() && file) text = await lexReadFile(file);
  return text;
}
$("btn-lex-parse").addEventListener("click", async () => {
  const msg = $("lex-up-msg");
  const text = await lexUploadText();
  if (!text.trim()) { msg.textContent = "先选文件或粘贴内容"; return; }
  const t = text.trim();
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      const d = JSON.parse(t);
      let n = 0;
      let cats = 0;
      const cnt = (o) => Array.isArray(o) ? o.length : 0;
      if (Array.isArray(d)) n = d.length;
      else if (d && typeof d === "object") {
        const dict = d.categories || d.分类;
        if (dict && typeof dict === "object") { Object.values(dict).forEach((v) => { n += cnt(v); }); cats = Object.keys(dict).length; }
        const rx = d.regexes || d.正则;
        if (rx && typeof rx === "object" && !Array.isArray(rx)) Object.values(rx).forEach((v) => { n += cnt(v); });
        else n += cnt(rx);
        n += cnt(d.words || d.patterns || d.keywords || d.rules);
      }
      msg.textContent = `看起来是 JSON：${n} 条规则${cats ? `，${cats} 个类型` : ""}（以服务端解析为准）`;
    } catch (e) {
      msg.textContent = "⚠ JSON 有问题：" + e.message;
    }
    return;
  }
  const lines = t.split(/\r?\n/).map((s) => s.trim()).filter((s) => s && !s.startsWith("#") && !s.startsWith("//"));
  const isCsv = (lines[0] || "").includes(",");
  msg.textContent = `看起来是${isCsv ? " CSV" : " TXT"}：${lines.length} 行${isCsv ? "（每行取逗号后的部分）" : ""}（以服务端解析为准）`;
});
$("btn-lex-fetch-jsd").addEventListener("click", () => {
  const box = $("lex-fetch-url");
  const u = box.value.trim();
  const raw = u.match(/^https?:\/\/raw\.githubusercontent\.com\/([^/]+)\/([^/]+)\/([^/]+)\/(.+)$/i);
  const gh = u.match(/^https?:\/\/github\.com\/([^/]+)\/([^/]+)\/(?:raw|blob)\/([^/]+)\/(.+)$/i);
  if (!raw && !gh) {
    showToast("这个地址不是 GitHub raw / blob 形式，改不了；直接填能下载的地址就行");
    return;
  }
  const [, user, repo, branch, path] = raw || gh;
  box.value = (JSDELIVR_TPL || "{u}:{r}:{b}:{p}").replace("{u}",user).replace("{r}",repo).replace("{b}",branch).replace("{p}",path);
  lexState.fetchDirty = true;
  showToast("已换成 jsDelivr 地址，点「一键获取」即可");
});

["lex-fetch-url", "lex-fetch-mirror"].forEach((id) => {
  $(id).addEventListener("input", () => { lexState.fetchDirty = true; });
});

$("btn-lex-fetch").addEventListener("click", async () => {
  const btn = $("btn-lex-fetch");
  const msg = $("lex-fetch-msg");
  const url = $("lex-fetch-url").value.trim();
  if (!url) {
    msg.textContent = "先填词表地址：插件不内置下载链接，自己找一份一行一条的纯文本词表填进来（或直接在左边「上传词库」）";
    return;
  }
  btn.disabled = true;
  msg.textContent = "下载并解析中…";
  try {
    const r = await bridge.apiPost("lexicons/fetch_upstream", {
      url,
      mirror: $("lex-fetch-mirror").value.trim(),
      force_mirror: $("lex-fetch-force").checked,
      category: $("lex-fetch-cat").value,
      auto_enable: $("lex-fetch-auto").checked,
    });
    if (r && r.ok) {
      const how = r.mode === "overwrite" ? "覆盖更新" : "新建";
      const delta = r.mode === "overwrite" && r.prev_count ? `（旧 ${lexNum(r.prev_count)} → 新 ${lexNum(r.count)}）` : "";
      msg.textContent = `${how}「${r.name}」：${lexNum(r.count)} 条${delta}，${r.enabled ? "已启用" : "未启用"}｜${r.source}（${r.elapsed_ms} ms）`;
      if (r.note) msg.textContent += `｜${r.note}`;
      await loadLexicons(true);
      showToast(`已获取 ${lexNum(r.count)} 条词条${r.enabled ? "" : "（未启用，去列表里打开）"}`);
    } else {
      msg.textContent = "❌ " + ((r && r.error) || "获取失败");
      showToast("获取失败：" + ((r && r.error) || "未知错误"));
    }
  } catch (e) {
    msg.textContent = "❌ " + e.message;
  } finally {
    btn.disabled = false;
  }
});

$("btn-lex-upload").addEventListener("click", async () => {
  const btn = $("btn-lex-upload");
  const msg = $("lex-up-msg");
  const text = await lexUploadText();
  if (!text.trim()) { msg.textContent = "先选文件或粘贴内容"; return; }
  const mode = $("lex-up-mode").value;
  btn.disabled = true;
  msg.textContent = "上传中…";
  try {
    const r = await bridge.apiPost("lexicons/upload", {
      name: $("lex-up-name").value.trim(),
      content: text,
      category: $("lex-up-cat").value,
      match: $("lex-up-match").value,
      mode,
      target_id: mode === "new" ? "" : $("lex-up-target").value,
    });
    if (r && r.ok) {
      msg.textContent = `已写入「${r.name}」：${r.count} 条${r.added ? `（新增 ${r.added}）` : ""}`;
      $("lex-up-text").value = "";
      $("lex-up-file").value = "";
      await loadLexicons();
      const extra = [];
      if (r.errors && r.errors.length) extra.push(`${r.errors.length} 条没解析成功`);
      if (r.warnings && r.warnings.length) extra.push(`${r.warnings.length} 条有误伤风险`);
      showToast(`上传成功：${r.count} 条${extra.length ? `（${extra.join("，")}，点「查看」看详情）` : ""}`);
    } else {
      msg.textContent = "❌ " + ((r && r.error) || "上传失败");
      showToast("上传失败：" + ((r && r.error) || "未知错误"));
    }
  } catch (e) {
    msg.textContent = "❌ " + e.message;
  } finally {
    btn.disabled = false;
  }
});

// —— 白名单 ——
function lexAllowWordsFromInput() {
  return $("lex-allow-input").value.split(/[\n,，、;；]+/).map((s) => s.trim()).filter(Boolean);
}
$("btn-lex-allow-add").addEventListener("click", async () => {
  const words = lexAllowWordsFromInput();
  if (!words.length) return showToast("先输入要放行的词");
  const r = await bridge.apiPost("lexicons/allow", { action: "add", words });
  if (r && r.ok) {
    $("lex-allow-input").value = "";
    await loadLexicons();
    $("lex-allow-msg").textContent = `白名单 +${(r.added || []).length}，当前共 ${r.count} 个`;
    showToast(`白名单 +${(r.added || []).length}（共 ${r.count}）`);
  } else showToast("操作失败：" + ((r && r.error) || "未知"));
});
$("btn-lex-allow-remove").addEventListener("click", async () => {
  const words = lexAllowWordsFromInput();
  if (!words.length) return showToast("先输入要移除的词");
  const r = await bridge.apiPost("lexicons/allow", { action: "remove", words });
  if (r && r.ok) {
    $("lex-allow-input").value = "";
    await loadLexicons();
    $("lex-allow-msg").textContent = `白名单 -${(r.removed || []).length}，当前共 ${r.count} 个`;
    showToast(`白名单 -${(r.removed || []).length}（共 ${r.count}）`);
  } else showToast("操作失败：" + ((r && r.error) || "未知"));
});
$("btn-lex-allow-clear").addEventListener("click", async () => {
  if (!lexState.manualAllow.length) return showToast("没有手动加的词");
  const okGo = await showConfirm(`清空手动加的 ${lexState.manualAllow.length} 个白名单词？（来自白名单词库的不受影响）`);
  if (!okGo) return;
  const r = await bridge.apiPost("lexicons/allow", { action: "clear" });
  if (r && r.ok) { await loadLexicons(); $("lex-allow-msg").textContent = "已清空手动词条"; showToast("已清空"); } else showToast("操作失败：" + ((r && r.error) || "未知"));
});

// —— 命中测试 ——
$("btn-lex-test").addEventListener("click", async () => {
  const text = $("lex-test-text").value;
  const box = $("lex-test-result");
  if (!text.trim()) return showToast("先输入一句话");
  box.innerHTML = emptyBox("load", "测试中…");
  let r;
  try {
    r = await bridge.apiPost("lexicons/test", { text, include_disabled: $("lex-test-all").checked });
  } catch (e) {
    box.innerHTML = emptyBox("warn", "测试失败：" + esc(e.message));
    return;
  }
  if (!r || r.error) {
    box.innerHTML = emptyBox("warn", "测试失败：" + esc((r && r.error) || "未知错误"));
    return;
  }
  if (!r.count) {
    box.innerHTML = emptyBox("ok", "没有命中任何规则 → 关键词维度不会判违规")
      + (r.blocked_count ? `<div class="lex-hint">另有 ${r.blocked_count} 条命中被白名单拦下了。</div>` : "");
    return;
  }
  const rows = r.hits.map((h) => `<tr>
      <td>${esc(lexCatName(h.category))}</td>
      <td>${esc(h.source || "—")}</td>
      <td class="lex-desc">${h.is_regex ? "正则规则" : "词长 " + h.length + " 字"}</td>
      <td class="lex-desc">位置 ${Number.isFinite(h.start) ? h.start : "-"}~${Number.isFinite(h.end) ? h.end : "-"}</td>
    </tr>`).join("");
  box.innerHTML = `<p class="desc">命中 <b>${r.count}</b> 条规则，第一条命中就会按「根据关键词」直接判违规并禁言。</p>
    <div class="lex-hint" style="margin-bottom:8px">🔒 命中词本身不显示（避免在界面摊开敏感词）；下表只给类型、来源、词长与位置，用来判断是否误伤。</div>
    <div class="table-wrap"><table class="lex-tbl">
      <thead><tr><th>类型</th><th>来源词库</th><th>规则</th><th>位置</th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    ${r.blocked_count ? `<div class="lex-hint">另有 ${r.blocked_count} 条命中被白名单拦下。</div>` : ""}`;
});

// ---------------- 初始化 ----------------
(async function init() {
  initTheme();
  try {
    // 宿主注入 bridge 可能稍晚于本模块执行，先等一下再判定（晚注入也能自愈）
    if (!(await waitBridge(5000))) {
      throw new Error("宿主页未注入 AstrBotPluginPage（刷新页面即可重试）");
    }
    await bridge.ready();
  } catch (e) {
    // bridge 不可用时降级提示
    $("platformList").innerHTML = emptyBox("warn", "bridge 初始化失败: " + esc(e.message));
    return;
  }
  await loadStatus();
  applyTimeZone(state.status && state.status.timezone);
  syncTop("status");
  loadLogHistory();
  connectSSE();
  // 定时刷新状态徽标
  setInterval(loadStatus, 30000);
})();


/* ═══════════ 定位锚点 / 外链 ═══════════
   锚点不再只是切页签，而是落到具体位置：设置页会清筛选 → 展开分组 → 滚到配置项 → 高亮闪一下。
   key 用 _conf_schema.json 里的字段名，与 .set-item[data-key] 对齐。
   这段刻意自己动手（#toast / 页签按钮 / 抽屉 class），不依赖主逻辑作用域里的函数，
   免得加载顺序一变化就蹦 "xxx is not defined"。 */
const GOTO_TARGET = {
  kpis:     { tab:"status",   sel:"#kpiRow",                             label:"概览 · 运行数据" },
  groups:   { tab:"status",   sel:"#groupList",                          label:"概览 · 已知群" },
  switches: { tab:"settings", group:"basic",                             label:"设置 · 基础功能" },
  judge:    { tab:"settings", group:"admin",                             label:"设置 · 权限判定" },
  welcome:  { tab:"settings", group:"basic", key:"enable_welcome",       label:"设置 · 成员入群欢迎" },
  global:   { tab:"settings", group:"basic", key:"target_group_openids", label:"设置 · 生效群范围" },
  mute:     { tab:"settings", group:"basic", key:"enable_mute",          label:"设置 · 禁言指令" },
  approve:  { tab:"settings", group:"approval",                          label:"设置 · 入群审批" },
  recall:   { tab:"settings", group:"recall",                            label:"设置 · 撤回功能" },
  review:   { tab:"moderation", sel:".mod-app",                          label:"群规审核工作台" },
};

const gg = (id) => document.getElementById(id);

/* 轻量提示：复用 #toast 的样式，但自己建元素，不依赖别处的 toast/showToast */
function uiToast(msg, isErr) {
  let t = gg("toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; document.body.appendChild(t); }
  t.textContent = msg;
  t.style.background = isErr ? "var(--danger)" : "var(--tx)";
  t.classList.add("show");
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove("show"), 3600);
}

function flash(el) {
  if (!el) return;
  el.classList.remove("focus-flash");
  void el.offsetWidth;                       /* 强制重排，动画才能重放 */
  el.classList.add("focus-flash");
  setTimeout(() => el.classList.remove("focus-flash"), 2600);
}
function scrollToCenter(el) {
  if (!el) return;
  try { el.scrollIntoView({ block:"center", behavior:"smooth" }); }
  catch (e) { el.scrollIntoView(); }
}
function closeHelpDrawer() {
  const d = gg("drawer"), sc = gg("scrim");
  if (d) d.classList.remove("on");
  if (sc) sc.classList.remove("on");
}
function switchTabSafe(name) {
  const b = document.querySelector('.tab[data-tab="' + name + '"]');
  if (b) b.click();
}

/* 设置页精确定位：第一次点「设置」才异步拉 schema，所以要等 .set-item 出现；
   找到后清掉筛选条件 → 展开分组 → 滚到中间 → 高亮 */
function focusSetting(key, group) {
  const t0 = Date.now();
  (function attempt() {
    const box = key ? document.querySelector('.set-item[data-key="' + key + '"]') : null;
    const sec = box ? box.closest(".set-group")
                    : (group ? document.querySelector('.set-group[data-group="' + group + '"]') : null);
    if (!sec && Date.now() - t0 < 2600) return setTimeout(attempt, 120);

    const kw = gg("setSearch");                    /* 触发原生监听，顺带同步内部状态 */
    if (kw && kw.value) { kw.value = ""; kw.dispatchEvent(new Event("input", { bubbles:true })); }
    const od = gg("setOnlyDirty");
    if (od && od.checked) { od.checked = false; od.dispatchEvent(new Event("change", { bubbles:true })); }

    if (sec && sec.classList.contains("collapsed")) {
      const h = sec.querySelector(".set-group-head");
      if (h) h.click();                            /* 走原生点击，折叠状态不会乱 */
    }
    const target = box || sec;
    if (target) { scrollToCenter(target); flash(target); }
    else uiToast("已切到「设置」页，但没找到对应配置项", true);
  })();
}

function gotoAnchor(id) {
  const t = GOTO_TARGET[id] || { tab:id };
  closeHelpDrawer();
  switchTabSafe(t.tab);
  if (t.tab === "settings") { focusSetting(t.key, t.group); return; }
  setTimeout(() => {
    const el = t.sel ? document.querySelector(t.sel) : null;
    const box = (el && el.closest(".card")) || el;
    scrollToCenter(box);
    flash(box);
  }, 80);
}

/* 复制文本：沙箱 iframe 里 Clipboard API 常被 permissions policy 拦掉（还是异步报错），
   所以先用同步的 execCommand 试，失败再退到 Clipboard API。 */
function copyText(txt) {
  try {
    const ta = document.createElement("textarea");
    ta.value = txt; ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    if (ok) return true;
  } catch (e) { /* 退到 Clipboard API */ }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).catch(() => {});   /* 静默失败，避免未处理的 rejection */
      return true;
    }
  } catch (e) { /* 彻底失败 */ }
  return false;
}

/* 打开外链：AstrBot 插件页 iframe 的 sandbox 只有 allow-scripts / allow-forms / allow-downloads，
   没有 allow-popups，所以 target=_blank 会被浏览器静默拦掉——点了没反应就是这个原因。
   先试 window.open，被拦就复制链接并提示。 */
function openDoc(url) {
  let w = null;
  try { w = window.open(url, "_blank", "noopener"); } catch (e) { w = null; }
  if (w) return true;
  const ok = copyText(url);
  uiToast(ok ? "面板沙箱不允许开新窗口 → 链接已复制，粘到浏览器打开就行"
             : "面板沙箱不允许开新窗口，请手动复制条目下方的链接：" + url, !ok);
  return false;
}

document.addEventListener("click", (e) => {
  const d = e.target.closest("[data-doc]");
  if (d) { e.preventDefault(); openDoc(d.dataset.doc); return; }
  const c = e.target.closest("[data-copy]");
  if (c) {
    e.preventDefault();
    const ok = copyText(c.dataset.copy);
    uiToast(ok ? "已复制：" + c.dataset.copy : "复制失败，请手动选中复制", !ok);
    return;
  }
  const g = e.target.closest("[data-goto]");
  if (g) { gotoAnchor(g.dataset.goto); return; }
  const b = e.target.closest(".termbadge");
  if (b) {                                   /* 角标：开抽屉并直接展开该术语 */
    if (typeof openHelp === "function") openHelp("terms", b.dataset.term);
    else if (gg("helpBtn")) gg("helpBtn").click();
  }
});

/* ═══════════ 帮助中心（数据见上：HELP / DOC） ═══════════ */
const DOC = (() => {
  const el = document.getElementById("docUrls");
  const g = (k) => (el && el.dataset[k]) || "";
  return { msg: g("msg"), overview: g("overview"), remove: g("remove"), addrobot: g("addrobot"),
           memberadd: g("memberadd"), md: g("md"), text: g("text") };
})();
const JSDELIVR_TPL = (document.getElementById("docUrls") || {}).dataset?.jsdelivr || "";
const HELP = {
  /* ── 术语：平台类来自 QQ 官方文档核对，插件类整理自插件 docs/GUIDE.md ── */
  terms: [
    { id:"timezone", term:"时区与时间口径（跟 AstrBot 一致）", go:["kpis","概览"],
      def:"面板上所有时间——事件流、今日统计、轮询上次时间——都按 AstrBot 全局配置 timezone 计算，默认 Asia/Shanghai（UTC+8），不跟随浏览器时区。时长类配置后台一律以秒为单位：default_mute_seconds 600、max_mute_seconds 86400（＝1 天）、recall_max_age_seconds 110，界面只是换算成分钟 / 天显示。", doc:null },
    { id:"11251", term:"11251 · 主动消息被拒", go:["switches","功能开关"],
      def:"机器人“主动”发消息时，QQ 要求该群 24 小时内有人 @ 过机器人，否则直接拒绝下发。欢迎语这类主动消息会中招；指令回复属于被动消息，不受影响。", doc:["发送群聊消息", DOC.msg] },
    { id:"11253", term:"11253 · 无权限拉取群名", go:["groups","群列表"],
      def:"群信息接口只对白名单机器人开放，未开放就返回 11253。拉不到群名不等于插件坏了——面板上有「备注」可以手动起名，备注优先于接口群名。", doc:["消息收发概述", DOC.overview] },
    { id:"window", term:"24 小时窗口（主动消息限额）", go:["switches","成员入群欢迎"],
      def:"每个群的主动消息有次数上限，且以最近一次用户交互为起算点；超限即被拒。", doc:["消息收发概述", DOC.overview] },
    { id:"eventid", term:"event_id 支持白名单", go:["welcome","成员入群欢迎"],
      def:"只有 3 个事件的 event_id 能拿来回复消息：INTERACTION_CREATE、GROUP_ADD_ROBOT、GROUP_MSG_RECEIVE。其余事件只能发主动消息。", doc:["发送群聊消息", DOC.msg] },
    { id:"40034027", term:"40034027 · 该事件不支持回复消息", go:["welcome","成员入群欢迎"],
      def:"用 event_id 去回复不在白名单里的事件就会报这个码，改成主动消息即可（但要过 24 小时窗口）。", doc:["发送群聊消息", DOC.msg] },
    { id:"memberadd", term:"GROUP_MEMBER_ADD · 成员入群事件", go:["switches","成员入群欢迎"],
      def:"intent 1<<24。事件体只有 timestamp / group_openid / member_openid（<b>没有</b>昵称、没有 user_openid），而且它不在 event_id 被动回复白名单里，所以入群欢迎只能发主动消息：受主动消息频控，群里没给机器人开主动推送时会被拒。插件收到这个事件就按「消息模板 · 成员入群 · 欢迎语」发一条，同一成员 10 分钟内只欢迎一次（审批通过时已欢迎过的不会再重复）。", doc:["群成员加入", DOC.memberadd] },
    { id:"joinreq", term:"GROUP_JOIN_REQUEST · 入群申请事件", go:["approve","入群审批"],
      def:"intent 1<<24（与成员加入同一位）。1.4.0 起这是入群申请通知的<b>主通道</b>：用户一提交申请事件就到，不用等轮询。事件体比 join_request_list 的单条更全（带 username / verify_info / apply_source / auto_approved）。前置条件：机器人必须是群管理员，否则平台根本不推这个事件。它不在 event_id 被动回复白名单里，所以申请通知仍走主动消息。", doc:null },
    { id:"joinmode", term:"申请获取方式 event_first / interval / off", go:["approve","入群审批"],
      def:"event_first（默认）＝事件为主 + 低频轮询兜底（默认 300 秒），既快又省接口；interval＝纯轮询 30 秒一次（1.4.0 之前的旧行为）；off＝只收事件、完全不轮询，申请仍可用 /群管 审批 即时拉取。同一个群 2 秒内到达的多条申请会合并成一条通知，不会刷屏。", doc:null },
    { id:"botauth", term:"审批按钮（消息内联键盘）怎么工作", go:["approve","入群审批"],
      def:"申请通知消息下方挂「通过 / 拒绝」按钮：点一下直接审批，不用打指令。同一申请的两个按钮共用 group_id，点完一个另一个会变灰（官方内联键盘的互斥机制），避免「通过完又点拒绝」。按钮通过 action.type=1（回调）把 data 回传给插件，data 里只放「动作 + 申请人 openid + 申请时间」，真实数据以插件本地待审批列表为准，所以伪造 data 无效。", doc:null },
    { id:"interaction", term:"INTERACTION_CREATE · 按钮点击事件", go:["approve","入群审批"],
      def:"intent 1<<26。AstrBot 的 qqofficial 适配器收到点击后，会先 PUT /interactions/{id} 返回 code=0（客户端停止转圈），再把它转成一条普通事件交给插件，消息链里是一个按钮组件。所以插件<b>不能</b>重复回应同一个 interaction_id（会报错），处理结果改用 event_id=互动事件 id 发被动消息回执。", doc:null },
    { id:"40034029", term:"40034029 · 内联键盘行/列超限", go:["approve","入群审批"],
      def:"官方内联键盘上限是 5 行 × 5 个按钮。插件按申请数自动限制行数（默认每条通知最多 3 个申请，可在「每条通知最多带几个申请」调整）。真被拒了会自动去掉按钮重发一次，保住正文。", doc:null },
    { id:"openid", term:"openid / user_openid 到底有什么区别", go:["groups","群列表"],
      def:"openid 按「机器人 + 用户」分配：同一个人在不同群、不同机器人下 ID 都不同，不能跨群复用，换机器人会重新分配。插件靠 platform_id 区分不同机器人，所以面板是「机器人 → 群名 → 设置」三级。", doc:["消息收发概述", DOC.overview] },
    { id:"intent", term:"Intent 位（1<<24 / 1<<25）", go:["switches","功能开关"],
      def:"成员变动事件要勾 1<<24，群内 @ 消息要勾 1<<25；漏勾就收不到对应事件，表现为「功能没反应」。", doc:["消息收发概述", DOC.overview] },
    { id:"addrobot", term:"GROUP_ADD_ROBOT 机器人入群事件", go:["welcome","机器人入群自我介绍"],
      def:"机器人被拉进群时推送，入群瞬间就能拿到 group_openid。用事件的 event_id 发被动消息可绕开 11251；发送成功会自动撤销旧的「首条消息欢迎」，失败或事件没收到则由兜底路径补发。", doc:["机器人入群事件", DOC.addrobot] },
    { id:"memberevent", term:"群成员事件（GROUP_MEMBER_ADD / REMOVE）", go:["groups","群列表"],
      def:"官方 v2 有这两个事件（autogen/event 下），走 intent 1<<24（GROUP_MEMBER_EVENT），事件体只有 timestamp / group_openid / member_openid / user_openid（可能为空），没有昵称、也没有事件 id。插件用运行时注入把处理器挂到 qq_official 的 botpy client 上，所以开启后需要重启或等适配器重连一次；一直收不到推送通常是平台侧没给这个机器人推该事件。", doc:null },
    { id:"membername", term:"为什么退群通知只能显示「某位成员」", go:["members","成员档案"],
      def:"取名顺序：本群档案（人工）→ 用事件里的 user_openid 去其它群借名字 → 群成员信息接口。前两条能用是因为档案是本地维护的；接口（GET /v2/groups/{g}/members）仍是内邀能力，普通机器人返回 11253。所以最省事的做法是给常在群里说话的人补一次名字：/群管 成员 改名 <序号> 昵称，或面板「成员档案」页签，补过就不会再被覆盖。", doc:null },
    { id:"msgseq", term:"40054005 / msg_seq 消息去重", go:["switches","功能开关"],
      def:"同一条消息用重复的 msg_seq 重发会被判为重复下发；重发必须换序号。", doc:["发送群聊消息", DOC.msg] },
    { id:"40064004", term:"40064004 · 撤回超 2 分钟", go:["recall","撤回"],
      def:"消息发出超过 2 分钟就撤不了，这是官方硬限制，和机器人是不是管理员无关。", doc:null },
    { id:"40062003", term:"40062003 · 撤回无权限", go:["recall","撤回"],
      def:"机器人得是群管理员才能撤回普通成员的消息；否则只能撤回自己发的。", doc:null },
    { id:"judge", term:"判定模式（任一 / 全部）", go:["judge","管理员判定"],
      def:"「任一条件满足」＝命中一条就算管理员，更宽松；「全部条件满足」＝要求同时满足。它决定谁能触发管理指令，也影响违规处置的触发面。", doc:null },

    { id:"confidence", term:"置信度（模型自评的把握程度）", go:["review","群规审核"],
      def:"不是插件算出来的，是审核模型在返回 JSON 里自己填的 0~1 自评分。插件只做一件事——拿它和阈值比：≥ 阈值就执行（撤回 + 禁言）；低于阈值只写记录、不执行，记录「动作」列会写「置信度 0.72 < 阈值 0.75」。命中关键词层时置信度固定记为 1.0，必然执行，所以不会出现「把握不足」。不同模型的分数口径不同，不可横向比较。字段名 confidence_threshold，按群配置。", doc:null },
    { id:"layer3", term:"四维度判定 / 三层开关", go:["review","群规审核"],
      def:"面板上显示四个维度（关键词 / 群规 / 语义 / 图片），实现其实只有三层：关键词层、模型层（群规与语义在同一次调用里出结果）、图片层。为什么不能只关「语义」留下「群规」——两者是同一次模型输出，拆开要付两倍 token 和延迟，还会让两个结论互相矛盾，所以如实说拆不了。", doc:null },
    { id:"dryrun", term:"试运行（dry_run）", go:["review","群规审核"],
      def:"只判定、只回话术，不撤回也不禁言。拿不准的群先开着跑几天，翻记录看命中情况再决定要不要真处置。", doc:null },
    { id:"exempt", term:"豁免名单 vs 词库白名单", go:["review","群规审核"],
      def:"两个别混：豁免名单（whitelist）是按群的成员名单，名单里的人不做处理；词库白名单是词汇级，命中白名单词的那条关键词规则直接跳过，用来消「电影」「淘宝」这类误伤，且只影响关键词维度。", doc:null },
    { id:"duration", term:"禁言时长是怎么算出来的", go:["review","群规审核"],
      def:"模型无权指定秒数，它只输出违规类型和严重度。命中本群某条群规 → 取该规则配的时长；没命中 → 按严重度 低＝10 分钟 / 中＝1 小时 / 高＝1 天；结果低于 60 秒 → 用兜底禁言时长（默认 600 秒）；超过 max_mute_seconds（默认 86400 秒＝1 天）→ 截断。", doc:null },
    { id:"freq", term:"举报频控 / 撤回尝试时限", go:["review","群规审核"],
      def:"举报频控 report_limit_per_hour 默认每人每小时 5 次；撤回尝试时限 recall_max_age_seconds 默认 110 秒，填 0＝不限、总是尝试。", doc:null },
    { id:"autoscan", term:"本群自动巡检（auto_scan）", go:["review","群规审核"],
      def:"默认关。开启后只对本群生效，默认只跑关键词层（零成本）；要让 AI 也参与，得再开「巡检时调用模型」，并受「每日模型调用上限」约束。", doc:null },
    { id:"commonban", term:"通用禁止项（common_ban）", go:["review","群规审核"],
      def:"跨群共享的一段禁止说明，会追加在每群的群规之前。内容越长越省 token（同一段前缀可复用）。", doc:null },
    { id:"verdict", term:"判定记录 / 试跑", go:["review","群规审核"],
      def:"每条判定都会落「类型 + 严重度 + 置信度」，方便回看时长是怎么来的。面板「记录 → 判定记录」可以按群筛、单条删除、标记误判。/群管 审核 测试 和面板试跑是诊断入口，不受维度开关限制，关着也能跑。", doc:null },
    { id:"forwardreport", term:"引用「合并转发」的消息举报", go:["review","群规审核"],
      def:"被引用的是合并转发时，QQ 官方下发的并不是转发正文，而是一份摘要清单（<b>[关联消息]</b> + 每条 <b>[消息内容]</b>），格式和转发原文不一样。插件会把摘要里的子消息正文抽出来跟缓存里的转发原文比对，命中后按缓存里的完整内容送审。处置对象是<b>转发这条消息的人</b>：谁发的处理谁，撤回也只撤这条转发；一条都对不上就跳过，不会乱猜人。", doc:null },
    { id:"matchmode", term:"词 vs 正则（match）", go:["review","群规审核"],
      def:"词库规则默认按字面量匹配（literal），公开词库都是这种；写正则要显式声明（文件级 regex、或单条加 re: 前缀）。TXT/CSV 上传默认 auto：看得出正则特征（\\d、.*、[..]、^$）才当正则，否则按字面量处理，这样 C++、1.5折 不会被误解释。", doc:null },
    { id:"variant", term:"变体检测（谐音 / 形近 / 插符号）", go:["review","群规审核"],
      def:"针对「把违禁词写成袋刷单、加·微·信」这类绕过写法。原理是三层：先把符号空格去掉、全角转半角（处理插符号），再把文字转成拼音音节序列与词条比对（处理谐音，同音字音节相同），最后比字形的相似度（处理末/未、己/已这类形近字）。定位上它只是<b>疑似信号</b>：单次命中<b>只撤回 + 记录 + 进待办，不会自动禁言</b>，所以误报的代价最多是多看一眼，不会误踢人。唯一的例外是「<b>变体累计升级</b>」的 L2（默认关，需另行开启），见该词条。默认关闭，先在试运行下翻记录看误报率。默认只做三字以上的谐音匹配——双音节中文同音词太多，拿两字词去匹配等于碰运气（实测正常群聊误报率会到两成以上）。<br /><b>状态：测试中（实验特性）</b>——这组开关<b>没有通用推荐值</b>：误报率取决于本群的说话风格（越口语化、越爱玩梗越容易撞音），请按本群情况自行取舍；面板里已把这组单独框出。拿不准就只开总开关 + 保持试运行，翻几天记录再决定是否开自动撤回。", doc:null },
    { id:"variantstrike", term:"变体累计升级（累计 N 次 → 提醒 / 可选禁言）", go:["review","群规审核"],
      def:"单次变体命中是噪声，同一人窗口内连续多次命中才是信号，所以按「累计」升级。L1（默认开）：窗口内（默认 7 天）疑似变体达到阈值（默认 3 次，之后每满阈值再提醒一次）→ 该条记录进待办并高亮，并发一条<b>被动消息 @ 已登记管理员</b>。L2（默认关）：达更高阈值（默认 5 次）才自动禁言，且必须同时满足「窗口内至少 1 次人工确认过的变体记录」+「不在冷却期内（默认 6 小时）」+ 非试运行；被人工标为误判的记录一律不计入累计。三条门槛是有意叠加的——变体匹配是启发式，误禁的代价远大于晚禁一分钟，默认不替群管做这个决定。", doc:null },
    { id:"adminregister", term:"管理员登记（/群管 管理登记）", go:["review","群规审核"],
      def:"为什么不能自动识别管理员：QQ 官方通道的群消息里拿不到稳定的角色字段（_get_qq_role 恒为 None），而且「谁在线、谁愿意兜底」本来就是群自己的事。所以由已通过管理员门槛的人（AstrBot 管理员 / 配置名单 / 群角色任一命中）主动发「<b>/群管 管理登记</b>」把自己写进本群提醒名单，「/群管 管理登记 取消」退出，「/群管 管理登记 列表」查看。变体累计提醒只 @ 名单内的人；<b>本群无人登记就不发提醒</b>——不刷群，也不消耗主动消息配额。", doc:null },
    { id:"todo", term:"待办（人工兜底队列）", go:["review","群规审核"],
      def:"机器筛、人确认的中间层。两类记录会进待办：① 置信度没到阈值、因此没自动执行的判定；② 变体检测的疑似命中。每条都能一键 撤回 / 禁言 / 忽略——撤回和禁言是真动作，会写进禁言历史与撤回历史；忽略只是把它从队列里划掉。这是刻意的设计取向：判不准的情况不猜，摆到人面前，比「犹豫着处理了」更安全。", doc:null },
    { id:"escalate", term:"复犯累进（重复违规加时长）", go:["review","群规审核"],
      def:"统计窗口内（默认 30 天）同一成员<b>已被实际执行</b>的违规次数，第 n 次违规的时长 = 基础时长 × min(1 + 前科次数 × 倍率, 倍率上限)，默认每次翻倍、最多 4 倍，且仍受单次禁言上限截断。只认真正执行过的记录：试运行的、被人工标为误判的都不算前科。默认关闭——它会改变实际处罚力度，建议先开试运行观察一阵。", doc:null },
    { id:"nowords", term:"面板为什么不显示词条内容", go:["review","群规审核"],
      def:"这是刻意设计不是 bug：词库详情只给类型、条数、词长分布；命中测试只给类型、来源、词长、位置；判定理由与日志也不写入命中的具体词。唯一例外是白名单——它是正常词汇且需要随时增删，所以照常显示。", doc:null }
  ],

  /* ── 指令手册：整理自 docs/GUIDE.md「指令手册」── */
  cmd: [
    { id:"common", term:"通用规则（先看这条）", go:null,
      def:"指令一律写成「/群管 <子指令>」，参数用空格分隔；不写 / 也行，但必须在消息里 @ 机器人，否则按普通聊天处理（消息里的 @机器人 匹配时会被自动忽略）。<br /><b>只发 /群管 不带子指令时，会回一句「参数不足」并自动列出指令树</b>——这是预期行为，不是报错，照着树里选一条再发即可。权限不足会收到提示。", doc:null },
    { id:"c_help", term:"/群管 · 查看帮助", go:null,
      def:"机器人回复本插件的指令帮助。全体成员可用。", doc:null },
    { id:"c_report", term:"/群管 举报 · 举报违规消息", go:["review","群规审核"],
      def:"用法：长按违规消息 → 回复它 → 发 /群管 举报（可附一句说明）。全体成员可用。核心约定：只有收到 /群管 举报 才进入审核，平时不扫描、不判定、不发言。", doc:null },
    { id:"c_rules", term:"/群管 群规 · 查看与维护本群群规", go:["review","群规审核"],
      def:"列表：/群管 群规 或 /群管 群规 列表。添加：/群管 群规 添加 规则名｜判定说明｜时长。批量：多条之间用 ; 分隔。删除：/群管 群规 删除 序号。清空：/群管 群规 清空。命令行添加的群规类型固定「其他违规」、严重度「中」，要精确设置请到面板配置。写操作需管理员。", doc:null },
    { id:"c_groupname", term:"/群管 状态 群名 ·（旧：群名，1.6.0 移除）刷新本群群名", go:["groups","群列表"],
      def:"调官方群信息接口查询并存入插件数据，面板优先显示它（优先级：手动备注 > 接口群名）。面板打开时不会自动请求。接口为白名单权限，无权限会返回 11253，此时用面板「备注」手动起名。管理员指令。", doc:null },
    { id:"c_mute", term:"/群管 禁言 @用户 时长 [理由]", go:["mute","禁言"],
      def:"时长写法：30秒 / 5分钟 / 2小时 / 1天（秒、分、时、天都支持简写 s/m/h/d），纯数字＝秒，也认「半小时」这类中文数字。必须 @ 目标成员；不能禁言自己；不写时长会提示格式错误；同时 @ 多人只处理第一个。不写理由就用默认禁言理由（默认「违反群规」）。机器人需在该群有群主或管理员权限。管理员指令。", doc:null },
    { id:"c_unmute", term:"/群管 禁言 解除 @用户 ·（旧：解禁 @用户，1.6.0 移除）", go:["mute","禁言"],
      def:"解除指定成员的禁言；支持一次 @ 多人，逐个处理并分别回复。等价写法 /群管 禁言 解除 @用户。管理员指令。", doc:null },
    { id:"c_listmute", term:"/群管 禁言 名单 ·（旧：查禁言，1.6.0 移除）当前禁言成员", go:["mute","禁言"],
      def:"列出本群当前被禁言的成员及到期时间（数据来自插件本地缓存）。等价写法 /群管 禁言 名单。管理员指令。", doc:null },
    { id:"c_history", term:"/群管 禁言 历史 [条数|汇总] [@用户|OpenID前缀] ·（旧：禁言历史，1.6.0 移除）", go:["mute","禁言"],
      def:"默认最近 10 条（范围 1~50）；加「汇总」按成员汇总；可指定成员；也能直接填 OpenID 或其前 8 位以上前缀，用来查已退群用户。QQ 接口不提供禁言历史查询，这些记录由插件本地保存。管理员指令。", doc:null },
    { id:"c_recall", term:"/群管 撤回 · 撤回消息", go:["recall","撤回"],
      def:"推荐做法：引用目标消息后发 /群管 撤回（普通人看不到消息 ID）；也可以 /群管 撤回 <消息ID>，ID 在面板「记录 → 撤回历史」里看。官方限制：超过 2 分钟不可撤回（40064004）；机器人是群管理员时可撤回普通成员消息，否则只能撤自己发的（40062003）。管理员指令。", doc:null },
    { id:"c_requests", term:"/群管 审批 · 待处理入群申请", go:["approve","入群审批"],
      def:"列出本群待处理申请（序号、昵称、来源、时间、验证消息）；缓存为空时会自动拉取最新列表。管理员指令。", doc:null },
    { id:"c_pass", term:"/群管 审批 通过 序号|昵称关键字 ·（旧：通过，1.6.0 移除）", go:["approve","入群审批"],
      def:"例：/群管 审批 通过 2 或 /群管 审批 通过 小明（匹配列表中第一个昵称含「小明」的申请）；旧的 /群管 通过 2 仍然可用。批准后自动回复结果；开启「成员入群欢迎」时会向群内发欢迎语。管理员指令。", doc:null },
    { id:"c_reject", term:"/群管 审批 拒绝 序号|昵称关键字 [理由] ·（旧：拒绝，1.6.0 移除）", go:["approve","入群审批"],
      def:"例：/群管 审批 拒绝 3 广告号；旧的 /群管 拒绝 3 广告号 仍然可用。理由可省略；填了会透传给官方接口作为拒绝原因。管理员指令。", doc:null },
    { id:"c_register", term:"/群管 管理登记 [列表|取消]", go:["review","群规审核"],
      def:"把自己加入本群「变体提醒名单」——变体累计到阈值时只 @ 名单内的人。不带参数＝登记（回执带当前人数）；加「列表/查看」看已登记的人；加「取消/移除」把自己移出。管理员指令。", doc:null },
    { id:"c_audit", term:"/群管 审核 · 状态 / 试跑 / 巡检 / 测试", go:["review","群规审核"],
      def:"不带参数看本群审核状态（开关、试运行、巡检、群规条数、阈值、上限、累计次数、缓存命中率）；/群管 审核 试跑 开|关 切试运行；/群管 审核 巡检 开|关 切关键词巡检；/群管 审核 测试 <内容> 用当前配置试判一条；/群管 审核 自我介绍 手动重发群管功能介绍。管理员指令。", doc:null },
    { id:"c_status", term:"/群管 状态 ·（旧：汇报 / 体检 / 自检 / 群名，1.6.0 移除）", go:["review","群规审核"],
      def:"所有「看一眼现状」的指令收在这一个入口，不带参数＝群管日报（待处理申请 + 禁言人数）。<br/>· <code>/群管 状态 群 [清理]</code>＝原来的体检：机器人还在不在已记录的群里（加「清理」才删离群群数据）<br/>· <code>/群管 状态 自检</code>＝原来的自检：审核链路配置 + 今日用量 + 并发闸门 + 最近一次模型调用<br/>· <code>/群管 状态 群名</code>＝原来的群名：刷新本群群名<br/>旧写法（/群管 汇报、/群管 体检、/群管 自检、/群管 群名）全部继续可用，只是文档只推这一套。管理员指令。", doc:null }
  ],

  /* ── Markdown 大全：写法全部按 QQ 官方「Markdown 消息」文档核对 ──
     每条给 sample（真实可复制的写法），面板里用同一个渲染器出效果，
     所以「看到什么 = 群里发出来大致是什么」。 */
  md: [
    { id:"start", term:"先决条件：格式要选 Markdown", sample:"",
      def:"「设置 → 消息模板」里每条模板都能单独选<code>纯文本</code> / <code>Markdown</code> / <code>图片</code>。选 Markdown 时插件按 msg_type=2 发自定义 Markdown；选纯文本就原样显示，井号和星号都会露出来。官方 2026/04/23 起，单聊、群聊的自定义 Markdown 已对所有机器人开放，不需要单独申请模板（频道场景仍需内邀）。",
      doc:["Markdown 消息", DOC.md] },
    { id:"title", term:"标题", sample:"# 一号标题\n## 二号标题\n正文写在这里",
      def:"一级用 1 个 <code>#</code>、二级用 2 个，最多 6 级。<code>#</code> 后面必须有空格，否则会被当正文。" },
    { id:"bold", term:"加粗 / 斜体 / 删除线", sample:"**加粗**　_斜体_　*星号斜体*　***加粗斜体***\n~~删除线~~　__下划线加粗__",
      def:"QQ 的 Markdown 认这 6 种文字样式，标点都要成对出现。" },
    { id:"link", term:"链接（群里能点）", sample:"官方文档：[🔗Markdown 消息](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html)",
      def:"写成 <code>[文字](https://…)</code>，协议必须是 http(s)，否则只显示成普通文字。别把网址光秃秃写在正文里，QQ 不会自动识别。" },
    { id:"image", term:"图片", sample:"![群规图 #208px #320px](https://example.com/img/rule.png)",
      def:"把地址换成<strong>你自己的公网图片</strong>（平台会下载转存，所以要能直接访问），<code>#宽px #高px</code> 可省略。本地图片先用「素材管理」上传拿 <code>qqbot-media://</code> 引用。插件里的 <code>{avatar}</code> 模块就是这条语法，不用自己拼 URL。<br>注意：别拿头像接口或别人的图站地址凑数，域名被判定异常可能连消息一起发不出去。",
      doc:["素材管理", DOC.md] },
    { id:"list", term:"有序 / 无序列表", sample:"# 管理员待办\n1. 更新群规\n2. 清理广告\n\n- 禁言 10 分钟\n- 撤回并记录",
      def:"有序用 <code>1.</code>、无序用 <code>-</code>。列表前面若是普通段落，中间要空一行才认；二级列表缩进 4 个空格。" },
    { id:"quote", term:"块引用", sample:"> 本群禁止刷屏\n> 违者禁言 10 分钟",
      def:"每行开头加 <code>&gt;</code>，适合放群规摘要、免责声明。" },
    { id:"hr", term:"分割线", sample:"上面一段话\n***\n下面一段话",
      def:"单独一行写 <code>***</code>，用来分段（插件帮助页的来源区就是它）。" },
    { id:"newline", term:"换行 / 连续空行", sample:"第一行\n第二行\n\u200B\n\u200B\n第三行",
      def:"Markdown 里单个换行会被合并成一行，想分段就空一行。要在同一段里连续空出几行（比如列表后接正文），官方给的办法是零宽空格 <code>\\u200B</code>，写一个多一行。" },
    { id:"at", term:"@某位成员 / 头像", sample:"{at} 欢迎加入！\n{avatar:160}\n有问题 @ 管理员",
      def:"官方标签是 <code>&lt;qqbot-at-user id=\"openid\" /&gt;</code>，旧写法 <code>&lt;@openid&gt;</code> 官方已标注即将弃用。模板里直接写 <code>{at}</code> 最省事：插件会自动换成「这条消息相关的成员」，头像用 <code>{avatar:40}</code> 小图 / <code>{avatar}</code> 100px / <code>{avatar:160}</code> 大图（仅 Markdown 格式生效，源图按尺寸自动取档）。上面预览里的 @群员 是示意，真实发送才展开成真人。" },
    { id:"no", term:"哪些写了不生效", sample:"| 表格 | 不支持 |\n```\n代码块 不支持\n```",
      def:"官方「支持格式」里没有<strong>表格</strong>和<strong>代码块</strong>（三个反引号），写了会原样显示；也不建议塞 HTML 标签。表情请直接打 emoji，不要用 <code>:smile:</code> 这种简码。" },
    { id:"var", term:"和 {变量} 混用", sample:"## 欢迎 {username}\n本群已开群管：[指令大全](https://q.qq.com)",
      def:"模板渲染顺序是「先替换 {变量}，再按 Markdown 解析」，所以变量值里不要带 <code>*</code> <code>#</code> <code>[</code> 这类符号（昵称里真有也没事，只是偶尔会歪）。Markdown 里要写花括号原文，请用 <code>{{</code> 转义成一对。" },
    { id:"err", term:"Markdown 相关报错码", sample:"",
      def:"<code>40034124</code> markdown 消息参数错误：正文为空，或 <code>content</code> 里塞了控制字符 / 过长；<code>40034127</code> 无 markdown 模板权限：说明走成了「模板消息」路径，自定义 Markdown 已不需要申请；<code>40034101</code> 主动消息超频控：欢迎语这类主动消息按分钟限流，等一会儿再发。",
      doc:["发送群聊消息", DOC.msg] }
  ],

  /* ── 常见问题：整理自 docs/GUIDE.md「常见问题（FAQ）」── */
  faq: [
    { id:"q1", term:"插件加载失败 / WebUI 里看不到插件", go:["global","全局作用域"],
      def:"原因：目录层级不对、metadata.yaml 不完整、或 AstrBot 版本不满足。确认目录是 plugins/astrbot_plugin_qq_group_manager/（别多套一层），确认 metadata.yaml 的 name 为 astrbot_plugin_qq_group_manager 且 astrbot_version 满足当前版本，再看 AstrBot 日志里的插件报错。", doc:null },
    { id:"q2", term:"发送指令没反应 / 提示无权限", go:["judge","管理员判定"],
      def:"逐项确认：管理员条件是否满足、群是否在生效群范围里、指令格式对不对（如 /群管 禁言 @某人 10分钟）。不带 / 的写法必须 @ 机器人；禁言、解禁必须 @ 目标成员；「启用禁言指令」得是开着的。", doc:null },
    { id:"q3", term:"禁言 / 解禁报错（HTTP 错误 / code）", go:["mute","禁言"],
      def:"机器人不是群主或管理员 → 去群里把它设为管理员。code 11251 → 这是主动消息受限，影响欢迎语这类主动推送，不影响指令回复。其他 code 连 HTTP 状态码一起反馈。", doc:["发送群聊消息", DOC.msg] },
    { id:"q4", term:"入群申请没有自动通知", go:["approve","入群审批"],
      def:"按顺序查：①「新申请群内通知」是否开着；②机器人是不是群管理员——不是的话平台不推申请事件（11703），轮询也拉不到，日志会提示一次然后退避；③看面板概览的「申请获取方式」：event_first 下通知靠事件即时到达，事件没来就等兜底轮询（默认 300 秒）；④确认群在「生效群范围」里；⑤主动消息被限（11251）时通知发不出去，可先用 /群管 审批 看列表。", doc:null },
    { id:"q9", term:"审批按钮点了没反应 / 变灰了", go:["approve","入群审批"],
      def:"先分清三种情况：①按钮直接点不动——「按钮可点范围」设的是 admin，只有群主/管理员能点；②点了没反馈——多半是当前 AstrBot 版本不支持按钮事件，启动日志会有「按钮自动禁用，退回纯文本通知」的告警，升级 AstrBot 即可；③提示「这条申请已处理或已失效」——别人先处理了，或申请已被自动审批策略放行，属于正常保护。", doc:null },
    { id:"q10", term:"同一批入群申请收到好几条通知", go:["approve","入群审批"],
      def:"1.4.0 起事件与轮询共用一张通知去重表（同一条申请 10 分钟内只推一次），且同一群 2 秒内的多条申请会合并成一条。如果仍重复：检查是不是开了多个 AstrBot 实例连同一个机器人（去重表在内存里，不跨实例）；或频繁重启导致内存表清空——重启后原来就在队列里的申请会再推一次。", doc:null },
    { id:"q5", term:"退群通知不生效 / 只显示「某位成员」", go:["members","成员档案"],
      def:"分两种情况：① 完全不发——确认「群成员事件 → 退群通知」已开，然后重启一次 AstrBot（或等下一条群消息触发重连注入），日志里出现「未找到平台名为 qq_official 的适配器实例」就是这个原因；② 发了但没名字——正常，事件里只有 openid，官方成员接口是内邀能力（11253），去「成员档案」给这个人补一次名字即可。通知默认走主动消息，因为 GROUP_MEMBER_REMOVE 不支持 event_id 被动回复（40034027）。", doc:null },
    { id:"q6", term:"设置改了没生效", go:["global","全局作用域"],
      def:"多数配置保存后即时生效（含轮询间隔）。个别没生效时，先在 WebUI 重新保存一次；仍不行就重启 AstrBot。改动「生效群范围」「管理员判定」这类配置后，建议重启一次。", doc:null },
    { id:"q7", term:"明明违规了却没执行（记录里写「置信度 xx < 阈值 0.75」）", go:["review","群规审核"],
      def:"这是有意设计的保护：模型自评置信度低于阈值时只记录、不执行，避免误杀。通常调低阈值（0.75 → 0.65~0.7），或把该违规类型写成一条明确的群规条目（命中规则维度比语义维度稳）。也可以先在面板试跑一遍这条内容，直接看到分数和理由。", doc:null },
    { id:"q8", term:"举报后回「判定把握不足」，人工审核在哪审？", go:["review","群规审核"],
      def:"结论先放这儿：本插件没有人工审核环节，也没有待审队列。这句话只表示「本次没自动处理」，判定和原因已经写进本地记录，不会推送给任何人，也不需要谁去点「通过」。三种触发情况：① 模型判违规但置信度低于阈值（记录写「置信度 0.72 < 阈值 0.75」）；② 引用消息过旧或无法定位（写「引用消息过旧或无法定位」，群里会提示重新引用再举报）；③ 执行环节失败（撤回/群管 禁言接口报错、无可用客户端、定位不到发送者，记录写具体原因）。记录都在面板「记录 → 判定记录」里，日志里同步有 [审核] 行——要不要处理，决策权一直在人手里。", doc:null },
    { id:"q9", term:"审核误判了怎么办？", go:["review","群规审核"],
      def:"按性价比来：先到「词典 → 白名单」把被误伤的词汇加进去（只影响关键词维度）；再把容易误伤的成员加进豁免名单；然后把这类违规写成一条判定边界清楚的群规；阈值最后才动——调低会少漏判但误杀变多。判定记录里可以标记误判，用来回看。拿不准就先开试运行观察几天。", doc:null },
    { id:"q10", term:"遇到 Bug 怎么反馈", go:null,
      def:"请提供：AstrBot 版本与插件版本、平台及适配器版本、复现步骤（指令原文可打码、发生的群、期望与实际结果）、对应时间点的插件日志（面板「日志」或 data/plugin_data/astrbot_plugin_qq_group_manager/ 下的文件日志）、错误信息里的 HTTP 状态码与 code（如 HTTP 400 code=11251）。", doc:null }
  ],

  /* ── 文档索引：插件本地文档 + QQ 官方文档 ── */
  docs: [
    { term:"GUIDE.md · 指令手册", def:"13 条指令的语法、示例与权限", file:"plugins/astrbot_plugin_qq_group_manager/docs/GUIDE.md → 指令手册", doc:null },
    { term:"GUIDE.md · 成员档案（openid 认不出人怎么办）", def:"为什么只有 openid、档案怎么攒、退群通知机制、官方接口 11253、局限", file:"docs/GUIDE.md → 成员档案（openid 认不出人怎么办）", doc:null },
    { term:"GUIDE.md · 群规审核（/群管 举报）", def:"四维度判定、禁言时长怎么算、置信度怎么来的、维度开关", file:"docs/GUIDE.md → 群规审核（/群管 举报）", doc:null },
    { term:"GUIDE.md · 词典（关键词词库）", def:"内置词库一览、白名单、词 vs 正则、文件格式、合规与隐私", file:"docs/GUIDE.md → 词典（关键词词库）", doc:null },
    { term:"GUIDE.md · 插件配置说明", def:"基础功能 / 权限判定 / 撤回 / 入群审批 / 群规审核 / 消息模板 / 日志", file:"docs/GUIDE.md → 插件配置说明", doc:null },
    { term:"GUIDE.md · 常见问题（FAQ）", def:"本页签里的 FAQ 就是从这里整理的，含原始排错细节", file:"docs/GUIDE.md → 常见问题（FAQ）", doc:null },
    { term:"GUIDE.md · 数据与日志位置", def:"配置文件、known_groups、mute_history、mod_*.json、日志保留天数", file:"docs/GUIDE.md → 数据与日志位置", doc:null },
    { term:"lexicons/SOURCES.md · 词库来源与协议", def:"三个上游开源项目、协议、词条数与生成脚本", file:"plugins/astrbot_plugin_qq_group_manager/lexicons/SOURCES.md", doc:null },
    { term:"发送群聊消息（事件白名单 / 频控 / 错误码）", def:"含 event_id 白名单、msg_seq 去重、主动消息限制", doc:["查看原文", DOC.msg] },
    { term:"消息收发概述（主动 / 被动消息）", def:"理解 24 小时窗口与限频的入口文档", doc:["查看原文", DOC.overview] },
    { term:"机器人入群事件", def:"GROUP_ADD_ROBOT 事件体字段说明", doc:["查看原文", DOC.addrobot] },
    { term:"群成员退出事件", def:"GROUP_MEMBER_REMOVE 事件体字段（官方当前未推送该事件）", doc:["查看原文", DOC.remove] },
    { term:"文本交互（@ 能力）", def:"群内 @ 机器人的消息链结构与解析", doc:["查看原文", DOC.text] }
  ]
};

/* ═══════════ 抽屉 / Tab / 搜索 ═══════════ */
const drawer=document.getElementById('drawer'), scrim=document.getElementById('scrim'),
      dlist=document.getElementById('dlist'), dq=document.getElementById('dq');
let curTab='terms';

const openKeys=new Set();                 /* 已展开的卡片（key = 页签:条目id） */
/* 「去这里」按钮文案：设置类写「去设置：xxx」，其余写「去这里：xxx」 */
function gotoLabel(gid){
  const t=GOTO_TARGET[gid]||{};
  const short=(t.label||'').split('·').pop().trim()||gid;
  return t.tab==='settings'?`去设置：${short}`:`去这里：${short}`;
}
function card(t){
  const gid=t.go?t.go[0]:null, gname=t.go?t.go[1]:'';
  const term=t.term, body=t.def;
  const m=term.match(/^(\d+)\s*·\s*(.+)$/);
  const code=m?`<span class="code">${m[1]}</span>`:'', title=m?m[2]:term;
  const k=curTab+':'+(t.id||t.term);
  const open=!!dq.value.trim()||openKeys.has(k);   /* 搜索时全部展开，否则点击才展开 */
  return `<div class="tcard ${open?'on':''}" data-k="${k}">
    <button class="tq" aria-expanded="${open}">${code}<span class="q">${title}</span>
      <svg class="ico s chev"><use href="#i-chev"/></svg></button>
    <div class="tans">
      <p>${body}</p>
      <div class="acts">
        ${gid?`<button class="mini" data-goto="${gid}" title="跳到对应位置并高亮"><svg class="ico s"><use href="#i-arrow"/></svg>${gotoLabel(gid)}</button>`:''}
        ${t.doc?`<a class="mini ghost" href="${t.doc[1]}" target="_blank" rel="noopener" data-doc="${t.doc[1]}" title="${t.doc[1]}"><svg class="ico s"><use href="#i-doc"/></svg>去官方文档</a>`:''}
      </div>
    </div></div>`;
}
dlist.onclick=e=>{                        /* 复制 Markdown 写法 / 点问题条展开收起 */
  const cp=e.target.closest("[data-mdcopy]");
  if(cp){
    const it=HELP.md[Number(cp.dataset.mdcopy)];
    if(it){const ok=copyText(String(it.sample||""));uiToast(ok?"已复制写法":"复制失败，请手动选中复制",!ok);}
    return;
  }
  const q=e.target.closest('.tq'); if(!q)return;
  const c=q.closest('.tcard'), on=c.classList.toggle('on');
  q.setAttribute('aria-expanded',on);
  on?openKeys.add(c.dataset.k):openKeys.delete(c.dataset.k);
};
const TABMETA={
  terms:['术语词典','平台术语已核对 QQ 官方文档，插件术语整理自插件 docs/GUIDE.md'],
  cmd:['指令手册','群内发送；标「管理员指令」的需要权限'],
  md:['Markdown 大全','按 QQ 官方「Markdown 消息」核对：每条给可复制的写法，并演示渲染效果'],
  faq:['常见问题','整理自插件 docs/GUIDE.md 的 FAQ'],
  docs:['文档索引','插件文档是本地文件，QQ 文档在开放平台']
};
/* 搜索排序：搜索串命中「标题/指令名」的排在只有正文提到关键词的前面。
   否则搜「禁言」会先冒出正文里捎带提到它的置信度说明，要滑半天才找到指令本身。 */
function helpRank(t,q){
  if(!q) return 0;
  const term=String(t.term||"").toLowerCase(), id=String(t.id||"").toLowerCase(), body=String(t.def||"").toLowerCase();
  if(term===q) return 0;
  if(id===q) return 1;
  if(term.startsWith(q)) return 2;
  if(term.includes(q)) return 3;
  if(id.includes(q)) return 4;
  if(body.includes(q)) return 5;
  return 9;
}
/* 抽屉一次只搜当前分类，但「禁言」这类词在「指令」分类里才是本体，
   所以搜到别处有时要提示一句，省得用户以为没收录。 */
function tabHint(q){
  if(!q) return "";
  const others=Object.keys(HELP).filter(t=>t!==curTab).map(t=>({
    label:(TABMETA[t]&&TABMETA[t][0])||t, n:HELP[t].filter(x=>helpRank(x,q)<9).length
  })).filter(o=>o.n>0);
  if(!others.length) return "";
  return `<div class="dgroup">其它分类也有匹配：${others.map(o=>`${esc(o.label)} ${o.n} 条`).join(" · ")}，点上方分类标签切换</div>`;
}
/* Markdown 大全的卡片：左边「写法」可复制，右边用同一个渲染器出「效果」，
   这样用户看到的就是群里大概会呈现的样子，不用自己脑补。 */
function mdCard(t, i){
  const src=String(t.sample||"");
  const shown=esc(src).replace(/\u200B/g,'<span class="zwsp">␦</span>');
  const grid=src?`<div class="mdsec-grid">
      <div>
        <div class="mdsec-lab"><span>写法</span>
          <button class="mini ghost" data-mdcopy="${i}" title="复制这段写法">复制</button></div>
        <pre class="mdcode">${shown}</pre>
      </div>
      <div>
        <div class="mdsec-lab"><span>渲染效果（示意）</span></div>
        <div class="mdprev pv-md">${previewModules(renderQQMarkdown(src))}</div>
      </div>
    </div>`:'';
  return `<div class="mdsec" data-k="md:${esc(t.id||t.term)}">
    <div class="mdsec-head"><b>${esc(t.term)}</b>
      ${t.doc?`<a class="mini ghost" href="${t.doc[1]}" target="_blank" rel="noopener" data-doc="${t.doc[1]}" title="${t.doc[1]}"><svg class="ico s"><use href="#i-doc"/></svg>${esc(t.doc[0])}</a>`:''}
    </div>
    <div class="mdsec-desc">${t.def}</div>
    ${grid}
  </div>`;
}

function render(){
  const q=dq.value.trim().toLowerCase();
  const list=HELP[curTab].map((t,i)=>[t,i]).filter(([t])=>helpRank(t,q)<9)
    .sort((a,b)=>helpRank(a[0],q)-helpRank(b[0],q)||a[1]-b[1]).map(([t])=>t);
  if(curTab==='md'){
    const idx=HELP.md.map((t,i)=>[t,i]).filter(([t])=>helpRank(t,q)<9).map(([,i])=>i);
    dlist.innerHTML = (idx.length
      ? `<div class="dgroup">${TABMETA.md[0]} · ${TABMETA.md[1]}</div>`+idx.map(i=>mdCard(HELP.md[i],i)).join('')
      : emptyBox("search", "没有匹配项，试试「标题」「图片」「链接」「列表」「@」")) + tabHint(q);
    return;
  }
  if(curTab==='docs'){
    const local=list.filter(d=>!d.doc), ext=list.filter(d=>d.doc);
    const rowL=d=>`<div class="doclink flat"><svg class="ico"><use href="#i-doc"/></svg>
        <div style="flex:1"><b>${d.term}</b><small>${d.def}</small><small class="mono">${d.file}</small></div>
        <button class="mini ghost" data-copy="${d.file}" title="复制文件路径">复制路径</button></div>`;
    dlist.innerHTML = `<div class="dgroup">提示：面板嵌在沙箱 iframe 里，若浏览器拦截新窗口，「打开」会自动把链接复制到剪贴板</div>` + (
      (local.length?`<div class="dgroup">插件文档（随插件发布的本地文件，面板里打不开，去插件目录看）</div>`+local.map(rowL).join(''):'')
      +(ext.length?`<div class="dgroup">QQ 机器人开放平台官方文档</div>`+ext.map(d=>
        `<div class="doclink">`+
        `<svg class="ico"><use href="#i-doc"/></svg>`+
        `<div style="flex:1"><b>${d.term}</b><small>${d.def}</small><small>${d.doc[1].replace(/^https?:\/\//i,'')}</small></div>`+
        `<a class="mini" href="${d.doc[1]}" target="_blank" rel="noopener" data-doc="${d.doc[1]}" title="${d.doc[1]}">打开</a>`+
        `<button class="mini ghost" data-copy="${d.doc[1]}" title="复制链接"><svg class="ico s"><use href="#i-copy"/></svg></button>`+
        `</div>`).join(''):'')
      || emptyBox("search", "没有匹配项")) + tabHint(q);
  }else{
    dlist.innerHTML = (list.length
      ? `<div class="dgroup">${TABMETA[curTab][0]} · ${TABMETA[curTab][1]}</div>`+list.map(card).join('')
      : emptyBox("search", "本分类没有匹配项，试试错误码数字（如 11251）或指令名（如 禁言）")) + tabHint(q);
  }
}
function openHelp(tab,id){
  if(tab){curTab=tab;[...document.querySelectorAll('#seg button')].forEach(b=>b.classList.toggle('on',b.dataset.tab===tab));}
  if(id){dq.value='';openKeys.add(curTab+':'+id);}
  render(); drawer.classList.add('on'); scrim.classList.add('on'); setTimeout(()=>dq.focus(),180);
  if(id){const el=dlist.querySelector(`[data-k="${curTab}:${id}"]`);
         if(el){const q=el.querySelector('.tq');
                if(q){el.classList.add('on');q.setAttribute('aria-expanded','true');}
                el.scrollIntoView({block:'center'}); flash(el);}}
}
function closeHelp(){drawer.classList.remove('on');scrim.classList.remove('on');}
document.getElementById('helpBtn').onclick=()=>openHelp('terms');
document.getElementById('closeBtn').onclick=closeHelp;
scrim.onclick=closeHelp;
document.getElementById('seg').onclick=e=>{if(e.target.dataset.tab){curTab=e.target.dataset.tab;
  [...e.currentTarget.children].forEach(b=>b.classList.toggle('on',b===e.target));render();}};
dq.oninput=render;
document.addEventListener('keydown',e=>{
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();openHelp('terms');}
  if(e.key==='Escape')closeHelp();});

/* ================= 成员档案（openid <-> 昵称/备注） ================= */
(function () {
  // 官方只给 member_openid，没有昵称；名字、备注都在这里维护。
  // 群列表复用「群规审核」的 mod/groups（带备注名/群名）。
  const bridge = window.AstrBotPluginPage;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  let groupsLoaded = false;
  let current = "";
  let page = 1;
  let total = 0;

  function toast(msg, isErr) {
    const el = $("modToast");
    if (!el) { console.log(msg); return; }
    el.textContent = msg;
    el.style.background = isErr ? "var(--danger)" : "var(--text)";
    el.classList.add("show");
    clearTimeout(toast._t);
    // 出错信息（含接口返回的长句）留久一点，别还没看清就没了
    toast._t = setTimeout(() => el.classList.remove("show"), isErr ? 6000 : 2600);
  }

  async function ensureGroups() {
    if (groupsLoaded) return;
    try {
      const res = await bridge.apiGet("mod/groups");
      const sel = $("memGroup");
      const seen = new Set();
      const opt = (g, label) => `<option value="${esc(g)}">${esc(label)}</option>`;
      let html = '<option value="">选择群…</option>';
      for (const b of (res.bots || [])) {
        for (const g of (b.groups || [])) {
          if (seen.has(g.group)) continue;
          seen.add(g.group);
          const label = (g.display || g.alias || g.name || ("未命名群 " + String(g.group).slice(-6)));
          html += opt(g.group, b.name ? `${label}（${b.name}）` : label);
        }
      }
      for (const g of (res.items || [])) {
        if (seen.has(g.group)) continue;
        seen.add(g.group);
        html += opt(g.group, g.display || g.alias || g.name || ("未命名群 " + String(g.group).slice(-6)));
      }
      sel.innerHTML = html;
      groupsLoaded = true;
      if (!current && sel.value) current = sel.value;
    } catch (e) {
      toast("群列表加载失败：" + e, true);
    }
  }

  function fmtTs(ts) {
    if (!ts) return "-";
    const d = new Date(Number(ts) * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function renderStats(st) {
    if (!st) { $("memStats").textContent = ""; return; }
    $("memStats").innerHTML =
      `共 <b>${st.total}</b> 条档案 · 在群 <b>${st.active}</b> · 已退 <b>${st.left}</b> · ` +
      `已命名 <b>${st.named}</b> / 未命名 <b>${st.unnamed}</b> · 累计发言 <b>${st.messages}</b> 条`;
  }

  async function load() {
    await ensureGroups();
    if (!current) {
      $("memList").innerHTML = '<div class="empty"><span>先在上面选一个群</span></div>';
      return;
    }
    const params = {
      group: current,
      status: $("memStatus").value,
      sort: $("memSort").value,
      keyword: ($("memSearch").value || "").trim(),
      page,
      size: 30,
    };
    let res;
    try {
      res = await bridge.apiGet("members", params);
    } catch (e) {
      $("memList").innerHTML = `<div class="empty"><span>加载失败：${esc(e)}</span></div>`;
      return;
    }
    if (res.error) {
      $("memList").innerHTML = `<div class="empty"><span>${esc(res.error)}</span></div>`;
      return;
    }
    total = res.total || 0;
    renderStats(res.stats);
    const pages = Math.max(1, Math.ceil(total / (res.size || 30)));
    $("memPageInfo").textContent = total ? `第 ${res.page}/${pages} 页 · 共 ${total} 条` : "";
    $("memPrev").disabled = page <= 1;
    $("memNext").disabled = page >= pages;

    const items = res.items || [];
    if (!items.length) {
      $("memList").innerHTML =
        '<div class="empty"><span>没有记录。档案是随群消息自动攒的：机器人没收到过这个群的消息，这里就是空的。</span></div>';
      return;
    }
    const rows = items.map((it, i) => {
      const name = (it.nickname || "").trim();
      const named = !!name;
      const src = { manual: "人工", api: "接口", msg: "消息", cross: "借名" }[it.name_source] || "";
      const badge = it.status === "left"
        ? '<span class="tag bad">已退群</span>'
        : '<span class="tag">在群</span>';
      const joins = Number(it.join_count || 1);
      const joinTag = joins > 1 ? ` <span class="hint">第 ${joins} 次进群</span>` : "";
      return `<tr>
        <td>${i + 1}</td>
        <td>
          <div style="font-weight:600">${named ? esc(name) : '<span style="color:var(--dim)">未命名</span>'}
            ${src ? `<span class="hint" style="margin-left:6px">${src}</span>` : ""}</div>
          <div class="hint oid" title="${esc(it.openid || "")}">${esc(it.openid || "")}</div>
        </td>
        <td>${esc(it.note || "")}</td>
        <td>${it.msg_count || 0}</td>
        <td>${it.status === "left" ? '<span class="hint">-</span>' : esc(it.last_seen_text || fmtTs(it.last_seen))}</td>
        <td>${badge}${joinTag}</td>
        <td style="white-space:nowrap">
          <button class="btn ghost small" data-act="name" data-oid="${esc(it.openid)}" data-name="${esc(name)}">改昵称</button>
          <button class="btn ghost small" data-act="note" data-oid="${esc(it.openid)}" data-note="${esc(it.note || "")}">备注</button>
          <button class="btn ghost small" data-act="del" data-oid="${esc(it.openid)}">删</button>
        </td>
      </tr>`;
    }).join("");
    $("memList").innerHTML = `<div class="table-wrap"><table class="group-table">
      <thead><tr><th>#</th><th>昵称 / openid</th><th>备注</th><th>发言</th><th>最近发言</th><th>状态</th><th>操作</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  async function update(openid, patch) {
    try {
      const r = await bridge.apiPost("members/update", Object.assign({ group: current, openid }, patch));
      if (r.error) { toast(r.error, true); return; }
      toast("已保存");
      await load();
    } catch (e) {
      toast("保存失败：" + e, true);
    }
  }

  $("memGroup").addEventListener("change", (e) => {
    current = e.target.value;
    page = 1;
    load();
  });
  ["memStatus", "memSort"].forEach((id) => $(id).addEventListener("change", () => { page = 1; load(); }));
  let searchTimer = null;
  $("memSearch").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { page = 1; load(); }, 300);
  });
  $("btnMemReload").addEventListener("click", () => load());
  $("memPrev").addEventListener("click", () => { if (page > 1) { page--; load(); } });
  $("memNext").addEventListener("click", () => { page++; load(); });

  $("memList").addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const openid = btn.dataset.oid;
    const act = btn.dataset.act;
    // 注意：这里一律走 showPrompt / showConfirm。
    // 面板跑在 sandbox iframe（无 allow-modals）里，原生 prompt/confirm
    // 会被浏览器拦掉并返回 null，表现就是"按钮点了没反应"。
    if (act === "name") {
      const v = await showPrompt({
        title: "改昵称",
        msg: "给这个 openid 起个名字（留空 = 清空）" + (openid ? "\n" + openid : ""),
        value: btn.dataset.name || "",
        placeholder: "例如：小明",
        maxlength: 40,
      });
      if (v === null) return;
      await update(openid, { nickname: v });
    } else if (act === "note") {
      const v = await showPrompt({
        title: "备注",
        msg: "备注只在本面板显示（留空 = 清空）",
        value: btn.dataset.note || "",
        placeholder: "例如：活跃老群友 / 广告号",
        maxlength: 200,
      });
      if (v === null) return;
      await update(openid, { note: v });
    } else if (act === "del") {
      const ok = await showConfirm("从档案里删掉这一条？删掉后这个人再退群就只能显示短标识了。\n" + openid);
      if (!ok) return;
      const r = await bridge.apiPost("members/delete", { group: current, openid });
      if (r.error) { toast(r.error, true); return; }
      toast("已删除");
      await load();
    }
  });

  // 「官方同步」必须有明确反馈：按钮进入同步中状态（禁用 + 文案变化），
  // 结束无论成败都在工具条下方留一行结果（不只是一闪而过的 toast），
  // 失败时按错误码给一句能照做的建议。
  $("btnMemSync").addEventListener("click", async () => {
    if (!current) { toast("先在左上角选一个群", true); return; }
    const btn = $("btnMemSync");
    const hint = $("memSyncHint");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "同步中…";
    if (hint) { hint.textContent = "正在调官方「群成员列表」接口…"; hint.style.color = ""; }
    const t0 = Date.now();
    try {
      const r = await bridge.apiPost("members/sync", { group: current });
      const ms = Date.now() - t0;
      if (r && r.error) {
        const msg = r.message || r.error;
        const tip = /11253/.test(String(msg))
          ? "该接口是内邀白名单能力，普通机器人用不了（不是插件坏了）。想补名字：用「批量导入」或群里发 /群管 成员 改名。"
          : msg;
        if (hint) { hint.textContent = `同步失败：${tip}`; hint.style.color = "var(--danger)"; }
        toast("同步失败：" + tip, true);
        return;
      }
      const added = Number(r.added || 0), updated = Number(r.updated || 0);
      const total = Number(r.count || added + updated);
      const line = total
        ? `同步完成：拉到 ${total} 人，新增 ${added}，更新 ${updated}（${ms} ms）`
        : `同步完成：接口没有返回任何成员（${ms} ms）`;
      if (hint) { hint.textContent = line; hint.style.color = "var(--ok)"; }
      toast(line);
      await load();
    } catch (e) {
      if (hint) { hint.textContent = "同步失败：" + e; hint.style.color = "var(--danger)"; }
      toast("同步失败：" + e, true);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  });

  $("btnMemClearLeft").addEventListener("click", async () => {
    if (!current) { toast("先在左上角选一个群", true); return; }
    const ok = await showConfirm("删掉这个群所有「已退群」的档案？昵称和备注会一起消失，之后这些人退群只能显示短标识。");
    if (!ok) return;
    const r = await bridge.apiPost("members/clear_left", { group: current });
    if (r.error) { toast(r.error, true); return; }
    toast(`已清理 ${r.removed} 条`);
    await load();
  });

  $("btnMemImport").addEventListener("click", () => {
    $("memImportBox").hidden = !$("memImportBox").hidden;
  });
  $("memImportCancel").addEventListener("click", () => { $("memImportBox").hidden = true; });
  $("memImportDo").addEventListener("click", async () => {
    if (!current) { toast("先选群", true); return; }
    const text = $("memImportText").value || "";
    if (!text.trim()) { toast("内容为空", true); return; }
    const r = await bridge.apiPost("members/import", { group: current, text });
    if (r.error) { toast(r.error, true); return; }
    toast(`导入完成：新增 ${r.added}，更新 ${r.updated}`);
    $("memImportText").value = "";
    $("memImportBox").hidden = true;
    await load();
  });

  window.__qqgmMembersLoad = () => {
    if (!$("memGroup").value) groupsLoaded = false; // 群列表可能在别处新增了群
    load();
  };
})();

/* ---------------- 入群自动审批（QQ 官方 /v2/groups/join_approval_strategy） ---------------- */
(function () {
  const S = { list: [], platforms: [], platformId: "", editing: "", wlTarget: "", busy: false, pendingCreate: null, pendingCreateTimer: 0 };

  const box = () => $("appr-rows");
  const apiGet = (p, q) => bridge.apiGet(p, q || {});
  const apiPost = (p, b) => bridge.apiPost(p, b || {});

  function rowsMsg(msg, icon) {
    const tb = box();
    if (tb) tb.innerHTML = '<tr><td colspan="7">' + emptyBox(icon || "warn", msg) + "</td></tr>";
  }

  function fmtLeft(d) {
    if (d === null || d === undefined) return { txt: "—", warn: false };
    if (d < 0) return { txt: "已过期", warn: true };
    if (d === 0) return { txt: "今天到期", warn: true };
    return { txt: "剩 " + d + " 天", warn: d <= 7 };
  }

  function toRFC(v) {
    if (!v) return "";
    const d = new Date(v);
    return isNaN(d.getTime()) ? "" : d.toISOString();
  }

  /* 官方策略接口是"写完过一会儿列表才变"，只 reload 一次会像没生效。
     做法：本地状态先重绘（乐观更新），再延迟两次拉官方列表对账。 */
  let syncTimers = [];
  const SYNC_DELAYS = [800, 2000, 4500];

  /* 官方列表有 1~3s 延迟：写完先用 pending 兜住"我刚改过"的视觉状态，
     等官方数据跟上了再自动交还，避免刚点完又弹回旧值的鬼畜现象 */
  const PENDING_MS = 6000;
  let pendingDel = new Set();
  let pendingState = Object.create(null);
  let pendingTimer = 0;

  function holdPending(fn) {
    fn();
    clearTimeout(pendingTimer);
    pendingTimer = setTimeout(() => {
      pendingDel = new Set();
      pendingState = Object.create(null);
      syncLater();               // 兜底时间到，以官方为准再对一次账
    }, PENDING_MS);
  }

  function applyPending(list) {
    const out = [];
    list.forEach((s) => {
      const id = s.strategy_id;
      if (pendingDel.has(id)) return;
      if (id in pendingState) {
        if (!!s.is_enable === pendingState[id]) delete pendingState[id];  // 官方跟上了，解除兜底
        else s = Object.assign({}, s, { is_enable: pendingState[id] });
      }
      out.push(s);
    });
    pendingDel.forEach((id) => {
      if (!list.some((s) => s.strategy_id === id)) pendingDel.delete(id);
    });
    return out;
  }
  function syncLater() {
    syncTimers.forEach(clearTimeout);
    syncTimers = SYNC_DELAYS.map((ms) =>
      setTimeout(() => loadApprovalStrategies(true), ms));
  }

  /* 所有按钮统一：点下去立刻有"处理中"反馈，异常也一定会弹提示（不再静默失败） */
  async function runAct(btn, fn) {
    if (S.busy) return;                     // 乐观重绘会换掉按钮节点，靠按钮 disabled 拦不住连点
    if (!btn) {
      S.busy = true;
      try { await fn(); } catch (e) { toast("操作失败：" + (e.message || e), true); }
      finally { S.busy = false; }
      return;
    }
    if (btn.dataset.busy === "1") return;
    const orig = btn.innerHTML;
    btn.dataset.busy = "1";
    btn.disabled = true;
    btn.textContent = btn.dataset.busyText || "处理中…";
    S.busy = true;
    try {
      await fn();
    } catch (e) {
      toast("操作失败：" + (e.message || e), true);
    } finally {
      S.busy = false;
      btn.dataset.busy = "";
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }

  async function loadApprovalStrategies(silent) {
    const q = { _t: Date.now() };            // 顺手破掉可能存在的 GET 缓存
    if (S.platformId) q.platform_id = S.platformId;
    let r;
    try { r = await apiGet("approval/strategies", q); }
    catch (e) { if (!silent) rowsMsg("加载失败：" + (e.message || e)); return; }
    if (!r || r.error) { if (!silent) rowsMsg("加载失败：" + ((r && r.error) || "未知错误")); return; }
    S.platforms = r.platforms || [];
    S.list = applyPending(r.strategies || []);
    S.platformId = r.platform_id || S.platformId;
    if (S.pendingCreate) {
      const pc = S.pendingCreate;
      const appeared = pc.id
        ? S.list.some((s) => s.strategy_id === pc.id)                    // 知道 id 就认 id
        : S.list.some((s) => !pc.knownIds.has(s.strategy_id));           // 不知道就看有没有新面孔
      if (appeared) {
        S.pendingCreate = null;
        clearTimeout(S.pendingCreateTimer);
      }
    }
    renderPlatforms();
    renderStrategies();
  }
  window.loadApprovalStrategies = loadApprovalStrategies;

  function renderPlatforms() {
    const sel = $("appr-platform");
    if (!sel) return;
    if (!S.platforms.length) {
      sel.innerHTML = "<option>无可用机器人实例</option>";
      return;
    }
    const ids = S.platforms.map((p) => p.id);
    const keep = ids.includes(S.platformId) ? S.platformId : ids[0];
    // 优先显示机器人名称；同名（比如两个号都叫"小助手"）时用 AppID 掩码消歧
    const seen = {};
    S.platforms.forEach((p) => {
      const n = p.name || "";
      if (n) seen[n] = (seen[n] || 0) + 1;
    });
    sel.innerHTML = S.platforms.map((p, i) => {
      // 没名字的（昵称还没拉取到）显示「机器人 #序号 + AppID」，比光秃秃一串掩码好认
      const main = p.name || `机器人 #${i + 1} · ${p.appid_masked || p.id}`;
      const tag = seen[main] > 1 ? `（${p.appid_masked || p.id}）` : "";
      const off = p.enable ? "" : "（未启用）";
      const tip = `AppID ${p.appid_masked || p.id}`;
      return `<option value="${esc(p.id)}" title="${esc(tip)}">${esc(main + tag + off)}</option>`;
    }).join("");
    sel.value = keep;
    S.platformId = keep;
  }

  /* 官方列表还没同步出来的新策略，先在表里占一行，别让人以为没创建成功 */
  function pendingRowHtml() {
    const p = S.pendingCreate;
    return `<tr class="appr-pending">
      <td><b class="mono">同步中…</b><div class="sub">刚创建，官方列表还没返回</div></td>
      <td><div class="appr-groups">${(p.groupsText || []).map((g) => `<span class="lex-tag mono">${esc(g)}</span>`).join("")}</div><div class="sub">${esc(p.mode)} · ${p.count} 个</div></td>
      <td>—</td>
      <td>${p.enable ? '<span class="lex-tag ok">启用</span>' : '<span class="lex-tag">停用</span>'}</td>
      <td>—</td>
      <td>${esc(p.remark || "—")}</td>
      <td><div class="appr-ops"><span class="sub">等 1~3s 或点「刷新」</span></div></td>
    </tr>`;
  }

  function renderStrategies() {
    const tb = box();
    if (!tb) return;
    // 策略被删掉 / 已不在列表里，就把挂着的白名单面板一起关掉，别留个孤儿
    if (S.wlTarget && !S.list.some((s) => s.strategy_id === S.wlTarget)) closeWl();
    const extra = S.pendingCreate ? 1 : 0;
    if ($("appr-count")) $("appr-count").textContent = `共 ${S.list.length + extra} 条（上限 20）`;
    if (!S.list.length && !extra) {
      rowsMsg("还没有策略，点右上角「＋ 新建策略」加一条", "warn");
      return;
    }
    const rows = S.list.map((s) => {
      const gs = (s.group_ids && s.group_ids.length) ? s.group_ids : (s.group_openids || []);
      const chips = gs.slice(0, 3).map((g) => `<span class="lex-tag mono">${esc(String(g))}</span>`).join("")
        + (gs.length > 3 ? `<span class="lex-tag">+${gs.length - 3}</span>` : "")
        + (gs.length ? "" : `<span class="lex-tag warn">未关联群</span>`);
      const mode = s.group_mode === "id" ? "群号" : "openid";
      const left = fmtLeft(s.expire_in_days);
      return `<tr>
        <td><b class="mono">${esc(s.strategy_id)}</b><div class="sub">创建 ${esc(s.created_at || "—")}</div></td>
        <td><div class="appr-groups">${chips}</div><div class="sub">${esc(mode)} · ${gs.length} 个</div></td>
        <td><b>${s.whitelist_user_count || 0}</b></td>
        <td>${s.is_enable ? '<span class="lex-tag ok">启用</span>' : '<span class="lex-tag">停用</span>'}</td>
        <td class="${left.warn ? "appr-warn" : ""}">${esc(s.expire_at || "—")}<div class="sub">${esc(left.txt)}</div></td>
        <td>${esc(s.remark || "—")}</td>
        <td><div class="appr-ops">
          <button class="btn small" data-act="edit" data-id="${esc(s.strategy_id)}">编辑</button>
          <button class="btn small btn-ghost" data-act="wl" data-id="${esc(s.strategy_id)}">白名单</button>
          <button class="btn small btn-ghost" data-act="toggle" data-id="${esc(s.strategy_id)}">${s.is_enable ? "停用" : "启用"}</button>
          <button class="btn small btn-ghost" data-act="exec" data-id="${esc(s.strategy_id)}">扫描</button>
          <button class="btn small btn-ghost" data-act="del" data-id="${esc(s.strategy_id)}">删除</button>
        </div></td>
      </tr>`;
    });
    if (S.pendingCreate) rows.unshift(pendingRowHtml());
    tb.innerHTML = rows.join("");
  }

  function findSid(id) { return S.list.find((x) => x.strategy_id === id); }

  function openEditor(id) {
    const s = id ? findSid(id) : null;
    S.editing = id || "";
    $("appr-editor").hidden = false;
    $("appr-editor-title").textContent = s ? `编辑策略 ${s.strategy_id}` : "新建策略";
    $("appr-op-wrap").hidden = !s;
    $("appr-group-op").value = "";
    $("appr-mode").value = s ? (s.group_mode || "id") : "id";
    $("appr-groups").value = "";
    $("appr-groups").placeholder = s
      ? "留空＝不修改关联群；要增删请先在上面选操作，再填群"
      : "123456789, 987654321";
    $("appr-expire").value = "";
    $("appr-remark").value = s ? (s.remark || "") : "";
    $("appr-enable").checked = s ? !!s.is_enable : true;
    $("appr-editor-hint").textContent = s
      ? "到期时间留空＝保持原样；群标识形式须与创建时一致。"
      : "到期时间留空＝一年后自动过期；建完记得加白名单并点「扫描」。";
    $("appr-editor").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function closeWl() {
    S.wlTarget = "";
    clearTimeout(S.wlHintTimer);
    if ($("appr-wl")) $("appr-wl").hidden = true;
    if ($("appr-wl-users")) $("appr-wl-users").value = "";
    if ($("appr-wl-hint")) $("appr-wl-hint").textContent = "";
  }

  function openWl(id) {
    const s = findSid(id);
    S.wlTarget = id;
    $("appr-wl").hidden = false;
    $("appr-wl-title").textContent = `白名单 · ${id}`;
    $("appr-wl-users").value = "";
    $("appr-wl-hint").textContent = s ? `当前约 ${s.whitelist_user_count || 0} 个号码` : "";
    $("appr-wl").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  async function reload() {
    await loadApprovalStrategies();
    syncLater();
  }

  /* --- 事件绑定（脚本在 body 末尾，DOM 已就绪） --- */
  const psel = $("appr-platform");
  if (psel) psel.addEventListener("change", () => {
    S.platformId = psel.value;
    rowsMsg("加载中…", "refresh");
    loadApprovalStrategies();
  });
  if ($("btn-appr-refresh")) $("btn-appr-refresh").addEventListener("click", () => {
    rowsMsg("加载中…", "refresh");
    loadApprovalStrategies();
  });
  if ($("btn-appr-new")) $("btn-appr-new").addEventListener("click", () => openEditor(""));
  if ($("btn-appr-editor-close")) $("btn-appr-editor-close").addEventListener("click", () => {
    $("appr-editor").hidden = true;
  });
  if ($("btn-appr-cancel")) $("btn-appr-cancel").addEventListener("click", () => {
    $("appr-editor").hidden = true;
  });
  if ($("btn-appr-wl-close")) $("btn-appr-wl-close").addEventListener("click", () => {
    closeWl();
  });

  if (box()) box().addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id, act = btn.dataset.act;
    const s = findSid(id);
    if (act === "edit") { openEditor(id); return; }
    if (act === "wl") { openWl(id); return; }

    if (act === "toggle") {
      const next = !(s && s.is_enable);
      const prev = s ? s.is_enable : null;
      btn.dataset.busyText = next ? "启用中…" : "停用中…";
      await runAct(btn, async () => {
        if (s) { s.is_enable = next; renderStrategies(); }   // 先本地翻转，状态立刻变
        const r = await apiPost("approval/toggle", { platform_id: S.platformId, strategy_id: id, enabled: next });
        if (r.error) {
          if (s) { s.is_enable = prev; renderStrategies(); }
          toast(r.error, true);
          return;
        }
        holdPending(() => { pendingState[id] = next; });
        toast(next ? "已启用" : "已停用");
        await reload();
      });
      return;
    }
    if (act === "exec") {
      btn.dataset.busyText = "扫描中…";
      await runAct(btn, async () => {
        const r = await apiPost("approval/execute", { platform_id: S.platformId, strategy_id: id });
        if (r.error) { toast(r.error, true); return; }
        toast(r.message || "已提交扫描，官方异步执行约 10 分钟", false, 4000);
      });
      return;
    }
    if (act === "del") {
      const ok = await showConfirm(`删除策略 ${id}？关联群和白名单会一起失效，且不可恢复。`);
      if (!ok) return;
      btn.dataset.busyText = "删除中…";
      await runAct(btn, async () => {
        const idx = S.list.findIndex((x) => x.strategy_id === id);
        const removed = idx >= 0 ? S.list.splice(idx, 1)[0] : null;
        if (removed) renderStrategies();                    // 先摘掉这一行
        const r = await apiPost("approval/delete", { platform_id: S.platformId, strategy_id: id });
        if (r.error) {
          if (removed) { S.list.splice(idx, 0, removed); renderStrategies(); }
          toast(r.error, true);
          return;
        }
        holdPending(() => { pendingDel.add(id); });
        if (S.wlTarget === id) closeWl();      // 白名单面板绑的是这条策略，跟着一起关
        toast("已删除");
        await reload();
      });
    }
  });

  if ($("btn-appr-save")) $("btn-appr-save").addEventListener("click", async () => {
    const groups = ($("appr-groups").value || "").trim();
    if (!S.editing && !groups) { toast("先填至少一个群", true); return; }
    const body = {
      platform_id: S.platformId,
      strategy_id: S.editing || "",
      group_mode: $("appr-mode").value,
      groups,
      group_op: S.editing ? ($("appr-group-op").value || "") : "",
      is_enable: !!$("appr-enable").checked,
      expire_at: toRFC($("appr-expire").value),
      remark: ($("appr-remark").value || "").trim(),
    };
    const btn = $("btn-appr-save");
    const isNew = !S.editing;
    btn.dataset.busyText = "保存中…";
    await runAct(btn, async () => {
      const r = await apiPost("approval/save", body);
      if (r.error) { toast(r.error, true); return; }
      toast(r.message || "已保存", false, isNew ? 4000 : 2200);
      $("appr-editor").hidden = true;
      S.editing = "";
      if (isNew) {
        const newId = String((r.result && r.result.strategy_id) || "");
        S.pendingCreate = {
          id: newId,
          groupsText: (body.groups || []).slice(0, 3).map(String),
          count: (body.groups || []).length,
          mode: body.group_mode === "openid" ? "openid" : "群号",
          enable: !!body.is_enable,
          remark: body.remark || "",
          knownIds: new Set(S.list.map((x) => x.strategy_id)),
        };
        renderStrategies();
        clearTimeout(S.pendingCreateTimer);
        S.pendingCreateTimer = setTimeout(() => {
          if (!S.pendingCreate) return;
          S.pendingCreate = null;
          renderStrategies();
          toast("官方列表还没同步出来，点右上角「↻ 刷新」再看一次", true, 6000);
        }, 12000);
      }
      await reload();          // reload 内部带延迟对账，新建的策略会自己冒出来
    });
  });

  const wlSend = async (op, btn) => {
    const users = ($("appr-wl-users").value || "").trim();
    if (!S.wlTarget) { toast("先选一条策略", true); return; }
    if (!users) { toast("先填号码", true); return; }
    btn.dataset.busyText = op === "add" ? "加入中…" : "移出中…";
    await runAct(btn, async () => {
      const r = await apiPost("approval/whitelist", {
        platform_id: S.platformId, strategy_id: S.wlTarget, op, users,
      });
      if (r.error) { toast(r.error, true); return; }
      toast(r.message || "已提交", false, 3200);
      $("appr-wl-users").value = "";
      await reload();
    });
  };
  if ($("btn-appr-wl-add")) $("btn-appr-wl-add").addEventListener("click", (e) => wlSend("add", e.currentTarget));
  if ($("btn-appr-wl-del")) $("btn-appr-wl-del").addEventListener("click", (e) => wlSend("del", e.currentTarget));
  if ($("btn-appr-wl-exec")) $("btn-appr-wl-exec").addEventListener("click", async (e) => {
    if (!S.wlTarget) { toast("先选一条策略", true); return; }
    const btn = e.currentTarget;
    btn.dataset.busyText = "扫描中…";
    await runAct(btn, async () => {
      const r = await apiPost("approval/execute", { platform_id: S.platformId, strategy_id: S.wlTarget });
      if (r.error) { toast(r.error, true); return; }
      toast(r.message || "已提交扫描，官方异步执行约 10 分钟", false, 4000);
    });
  });
})();
