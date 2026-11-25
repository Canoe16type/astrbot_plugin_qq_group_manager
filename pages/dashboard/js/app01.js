
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
  records: () => { if (window.__qgmUnifiedRecordsLoad) window.__qgmUnifiedRecordsLoad(); },
  logs: () => loadLogHistory(),
  settings: () => loadSettings(),
  approval: () => loadApprovalStrategies(),
};

/* ---------------- 顶部标题（跟页签联动，页面里不再重复写 h2） ---------------- */
const TAB_HEAD = {
  status: ["概览", "机器人平台、功能开关与全局设置一览"],
  settings: ["插件设置", "表单由 _conf_schema.json 自动生成，与 AstrBot 配置面板读写同一份配置"],
  ops: ["群管", "入群申请与当前禁言状态一览；同意 / 拒绝请在群内用管理员指令处理"],
  moderation: ["群规审核", "左侧选群，右侧按「群规 / 举报设置 / 判定与试跑」分栏配置，按群独立保存；话术统一在设置 → 消息模板"],
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