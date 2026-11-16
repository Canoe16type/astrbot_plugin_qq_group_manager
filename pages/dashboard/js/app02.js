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