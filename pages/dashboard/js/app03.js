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
  ["msg_mod_unattributed", "无法定位发送者时的回执"],
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
  "todo_enabled", "report_ack_enabled", "smart_penalty", "rule_floor_high", "smart_tier_bump_at", "warn_confidence_threshold", "smart_force_mute_priors", "smart_max_multiplier",
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
  out.warn_confidence_threshold = Math.max(0, Math.min(parseFloat(out.warn_confidence_threshold || 0.6), 1));
  out.smart_force_mute_priors = Math.max(0, Math.min(parseInt(out.smart_force_mute_priors || 0, 10), 99));
  out.smart_max_multiplier = Math.max(1, Math.min(parseFloat(out.smart_max_multiplier || 3), 10));
  return out;
}

/* ---------------- 话术模板 ---------------- */
function renderTemplates() {
  const list = $("tpl-list");
  if (list) {
    list.innerHTML = TPL_FIELDS.map(([key, label]) =>
      `<div class="row-item"><div class="left"><span class="tag">${esc(label)}</span>` +
      `<span class="hint">${esc(key)} · 已迁移到设置 → 消息模板</span></div></div>`).join("");
    return;
  }
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
    ["今日违规", s.today_hit || 0],
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
    tr.style.cursor = "pointer";
    tr.title = "点击查看完整判定详情";
    tr.onclick = () => showVerdictDetail(v);
    const dim = (detail && detail.dim_names) || DIM_LABELS;
    const cats = (detail && detail.cat_names) || CAT_LABELS;
    tr.innerHTML =
      `<td>#${v.id == null ? "-" : v.id}</td>` +
      `<td>${ts2str(v.created_at)}</td>` +
      `<td>${esc((v.target_name || v.target || "-").slice(0, 12))}</td>` +
      `<td class="vtext" title="${esc(v.text || "")}">${esc((v.text || "—").slice(0, 24))}${(v.text || "").length > 24 ? "…" : ""}` +
      (v.images ? ` <span class="hint">🖼${v.images}</span>` : "") + `</td>` +
      `<td>${esc(dim[v.basis] || v.basis || "-")}</td>` +
      `<td><span class="tag ${v.violation ? "hit" : "clean"}">${v.violation ? esc(cats[v.category] || v.category || "违规") : "未违规"}</span>` +
      (v.variant_hit ? ` <span class="tag" title="疑似变体写法（${esc((v.variant || {}).method || "")}）${v.variant_recalled ? "，已按变体撤回" : ""}">变体</span>` : "") + `</td>` +
      `<td>${v.confidence == null ? "-" : Number(v.confidence).toFixed(2)}</td>` +
      `<td>${(() => {
        if (v.revoked) return '<span class="tag" title="' + esc(v.revoked.reason || "已撤销") + '">已撤销</span>';
        const act = v.action || (v.executed ? "mute" : "none");
        if (act === "warn") return "警告";
        if (act === "recall") return "已撤回";
        if (act === "mute") return "已禁言 " + fmtDur(v.ban_seconds);
        if (act === "todo") return "转人工待办";
        return esc(v.skipped_reason || (v.violation ? "未执行" : "-"));
      })()}</td>` +
      `<td>${v.from_cache ? "是" : "否"}</td>` +
      `<td style="white-space:nowrap"><button class="btn" data-fb="${v.id}">误判</button> ` +
      `<button class="btn" data-ok="${v.id}" title="判定正确，加入提示词正例">正确</button> ` +
      (v.executed && !v.revoked ? `<button class="btn" data-rv="${v.id}" title="撤销这条处罚并致歉">撤销</button> ` : "") +
      `<button class="btn danger" data-dv="${v.id}" title="删除这条本地记录">✕</button></td>`;
    tr.querySelector("[data-fb]").onclick = (e) => { e.stopPropagation(); markFeedback(v.id, "false_positive"); };
    tr.querySelector("[data-ok]").onclick = (e) => { e.stopPropagation(); markFeedback(v.id, "true_positive"); };
    tr.querySelector("[data-dv]").onclick = (e) => { e.stopPropagation(); delVerdict(v.id); };
    const rv = tr.querySelector("[data-rv]");
    if (rv) rv.onclick = (e) => { e.stopPropagation(); revokeVerdict(v); };
    rows.appendChild(tr);
  });
  if (!(res.items || []).length) rows.innerHTML = '<tr><td colspan="10" style="color:var(--sub)">暂无记录</td></tr>';
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
    ["判定依据", (v.policy_trace && v.policy_trace.source)
      ? `${v.policy_trace.source === "rule" ? "群规" : v.policy_trace.source === "smart" ? "智能分级" : "通用阶梯"}`
        + (v.policy_trace.rule_name ? `「${v.policy_trace.rule_name}」` : "")
        + `｜匹配方式 ${v.policy_trace.match_by || "none"}｜前科 ${v.policy_trace.prior ?? 0}｜倍率 ×${v.policy_trace.escalation ?? 1}`
        + (v.policy_trace.capped ? "（已达上限）" : "")
      : "-"],
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
    `<pre style="white-space:pre-wrap;word-break:break-all;background:rgba(127,127,127,.14);padding:10px;border-radius:8px;margin:6px 0;font-size:12px">${esc(v.text || "（这条是旧版记录，当时只留了 200 字以内，且可能为空）")}</pre></div>`;
  wrap.onclick = (e) => { if (e.target === wrap) wrap.remove(); };
  document.body.appendChild(wrap);
  document.getElementById("vd-close").onclick = () => wrap.remove();
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
        const capped = r.action && r.action.escalation_capped ? "（已达单次上限）" : "";
        const src = r.action && r.action.source && r.action.source !== "none"
          ? `｜依据 ${r.action.source === "rule" ? "群规" : r.action.source === "smart" ? "智能分级" : "通用阶梯"}`
            + (r.action.rule_name ? `「${r.action.rule_name}」` : "")
            + (r.action.match_by && r.action.match_by !== "none" ? `（${r.action.match_by}）` : "")
          : "";
        return base + prior + capped + src + (r.action && r.action.smart ? "（智能分级已开启）" : "");
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
  const _saveTpl = $("btn-save-templates");
  if (_saveTpl) _saveTpl.onclick = saveTemplates;
  const _clearTpl = $("btn-clear-templates");
  if (_clearTpl) _clearTpl.onclick = clearTemplates;
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