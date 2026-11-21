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
    const ops = [
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
      <td>${lexNum(x.count)}<div class="lex-desc">字面量 ${lexNum(x.literal_count)}${x.regex_count ? ` · 正则 ${x.regex_count}` : ""}</div></td>
      <td>${esc(x.author || "—")}<div class="lex-desc">${esc((x.sources || [])[0] || "")}</div></td>
      <td>${ops.join("")}</td>
    </tr>`;
  });
  $("lex-rows").innerHTML = rows.join("") || `<tr><td colspan="5">${emptyBox("book", "还没有任何词库")}</td></tr>`;
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
    { id:"c_groupname", term:"/群管 状态 群名 ·刷新本群群名", go:["groups","群列表"],
      def:"调官方群信息接口查询并存入插件数据，面板优先显示它（优先级：手动备注 > 接口群名）。面板打开时不会自动请求。接口为白名单权限，无权限会返回 11253，此时用面板「备注」手动起名。管理员指令。", doc:null },
    { id:"c_mute", term:"/群管 禁言 @用户 时长 [理由]", go:["mute","禁言"],
      def:"时长写法：30秒 / 5分钟 / 2小时 / 1天（秒、分、时、天都支持简写 s/m/h/d），纯数字＝秒，也认「半小时」这类中文数字。必须 @ 目标成员；不能禁言自己；不写时长会提示格式错误；同时 @ 多人只处理第一个。不写理由就用默认禁言理由（默认「违反群规」）。机器人需在该群有群主或管理员权限。管理员指令。", doc:null },
    { id:"c_unmute", term:"/群管 禁言 解除 @用户 ·", go:["mute","禁言"],
      def:"解除指定成员的禁言；支持一次 @ 多人，逐个处理并分别回复。等价写法 /群管 禁言 解除 @用户。管理员指令。", doc:null },
    { id:"c_listmute", term:"/群管 禁言 名单 ·当前禁言成员", go:["mute","禁言"],
      def:"列出本群当前被禁言的成员及到期时间（数据来自插件本地缓存）。等价写法 /群管 禁言 名单。管理员指令。", doc:null },
    { id:"c_history", term:"/群管 禁言 历史 [条数|汇总] [@用户|OpenID前缀] ·", go:["mute","禁言"],
      def:"默认最近 10 条（范围 1~50）；加「汇总」按成员汇总；可指定成员；也能直接填 OpenID 或其前 8 位以上前缀，用来查已退群用户。QQ 接口不提供禁言历史查询，这些记录由插件本地保存。管理员指令。", doc:null },
    { id:"c_recall", term:"/群管 撤回 · 撤回消息", go:["recall","撤回"],
      def:"推荐做法：引用目标消息后发 /群管 撤回（普通人看不到消息 ID）；也可以 /群管 撤回 <消息ID>，ID 在面板「记录 → 撤回历史」里看。官方限制：超过 2 分钟不可撤回（40064004）；机器人是群管理员时可撤回普通成员消息，否则只能撤自己发的（40062003）。管理员指令。", doc:null },
    { id:"c_requests", term:"/群管 审批 · 待处理入群申请", go:["approve","入群审批"],
      def:"列出本群待处理申请（序号、昵称、来源、时间、验证消息）；缓存为空时会自动拉取最新列表。管理员指令。", doc:null },
    { id:"c_pass", term:"/群管 审批 通过 序号|昵称关键字 ·", go:["approve","入群审批"],
      def:"例：/群管 审批 通过 2 或 /群管 审批 通过 小明（匹配列表中第一个昵称含「小明」的申请）；旧的 /群管 通过 2 仍然可用。批准后自动回复结果；开启「成员入群欢迎」时会向群内发欢迎语。管理员指令。", doc:null },
    { id:"c_reject", term:"/群管 审批 拒绝 序号|昵称关键字 [理由] ·", go:["approve","入群审批"],
      def:"例：/群管 审批 拒绝 3 广告号；旧的 /群管 拒绝 3 广告号 仍然可用。理由可省略；填了会透传给官方接口作为拒绝原因。管理员指令。", doc:null },
    { id:"c_register", term:"/群管 管理登记 [列表|取消]", go:["review","群规审核"],
      def:"把自己加入本群「变体提醒名单」——变体累计到阈值时只 @ 名单内的人。不带参数＝登记（回执带当前人数）；加「列表/查看」看已登记的人；加「取消/移除」把自己移出。管理员指令。", doc:null },
    { id:"c_audit", term:"/群管 审核 · 状态 / 试跑 / 巡检 / 测试", go:["review","群规审核"],
      def:"不带参数看本群审核状态（开关、试运行、巡检、群规条数、阈值、上限、累计与今日次数）；/群管 审核 试跑 开|关 切试运行；/群管 审核 巡检 开|关 切关键词巡检；/群管 审核 测试 <内容> 用当前配置试判一条；/群管 审核 自我介绍 手动重发群管功能介绍。管理员指令。", doc:null },
    { id:"c_status", term:"/群管 状态 ·", go:["review","群规审核"],
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

  /* ── 模板变量：纯文本 / Markdown 差异与用法 ── */
  tplvars: [
    { id:"start", term:"模板变量怎么填（先看这条）", go:[],
      def:"模板里用 <code>{变量名}</code> 占位，发送前由插件替换。<br><b>纯文本模式</b>：变量按普通文字替换，不解析 Markdown；<code>{at}</code> 只能显示成 @昵称，部分平台不会真的提醒。<br><b>Markdown 模式</b>：变量替换后再按 Markdown 解析；<code>{at}</code> 会生成官方 <code>&lt;qqbot-at-user id=\"member_openid\" /&gt;</code>，<code>{avatar}</code> 才会生效。<br><b>空值</b>：变量为空时替换为空字符串；整行是否保留由模板决定。<br><b>不支持</b>：模板里不能写 <code>{</code> 原文之外的运算/条件；未知变量会原样出现，请在面板变量提示里核对。" },
    { id:"vars_all", term:"全量变量清单（按场景）", go:[],
      def:"<b>没有“所有模板通用”的变量</b>：每个模板只认识自己场景的变量，未列出的变量在该模板会原样显示。<br><br>" +
          "<b>审核 / 处罚</b>：<code>{basis_detail}</code> <code>{category}</code> <code>{category_public}</code> <code>{severity_label}</code> <code>{duration}</code> <code>{punish}</code> <code>{rule}</code> <code>{rule_name}</code> <code>{reason}</code> <code>{reason_line}</code> <code>{vid}</code> <code>{verdict}</code> <code>{method}</code> <code>{days}</code> <code>{mentions}</code> <code>{next}</code><br>" +
          "<b>踢人</b>：<code>{names}</code> <code>{id}</code> <code>{error}</code> <code>{max}</code> <code>{count}</code> <code>{lines}</code><br>" +
          "<b>禁言 / 撤回</b>：<code>{username}</code> <code>{duration_text}</code> <code>{openid}</code> <code>{list}</code> <code>{count}</code> <code>{error}</code><br>" +
          "<b>审批 / 欢迎</b>：<code>{username}</code> <code>{strategy}</code> <code>{bot_name}</code> <code>{list}</code><br>" +
          "<b>成员 / 管理员 / 日报</b>：<code>{user}</code> <code>{admins}</code> <code>{short}</code> <code>{extra}</code> <code>{date}</code> <code>{time}</code> <code>{muted_count}</code> <code>{pending_count}</code><br>" +
          "<b>Markdown 专用</b>：<code>{at}</code>（真 @）、<code>{avatar}</code> / <code>{avatar:宽度}</code>（头像）；纯文本下这些变量不生成标签/图片。" },
    { id:"at", term:"{at} · @ 某位成员", go:[],
      def:"纯文本：显示为 <code>@昵称</code>（不保证真 @）。<br>Markdown：生成 <code>&lt;qqbot-at-user id=\"member_openid\" /&gt;</code>，群里才会真正提醒。<br>适用：警告、撤销致歉、转人工提醒。" },
    { id:"avatar", term:"{avatar} / {avatar:宽度} · 头像", go:[],
      def:"仅 Markdown 生效。官方事件头像 URL 优先；旧 AstrBot 未透传时回退 qlogo。示例：<code>{avatar:40}</code> 小图、<code>{avatar}</code> 100px、<code>{avatar:160}</code> 大图。" },
    { id:"punish", term:"{punish} / {duration} · 处罚描述", go:[],
      def:"<code>{duration}</code>：纯时长（10分钟 / 1天）。<br><code>{punish}</code>：完整动作（禁言 10分钟 / 撤回并警告），纯文本与 Markdown 都可能出现，适合直接放进句子。" },
    { id:"category", term:"{category} / {category_public} · 违规类别", go:[],
      def:"<code>{category}</code>：群规分类名（如「其他违规」）。<br><code>{category_public}</code>：公开类别（如「C6 提示词注入／越权指令（…）」），语义判定使用；群规命中时仍用规则名。" },
    { id:"basis", term:"{basis} / {basis_detail} / {rule_name} · 判定依据", go:[],
      def:"<code>{basis}</code>：维度名（语义分析 / 群规匹配）。<br><code>{basis_detail}</code>：完整依据句（「群规第2条「引人不适」」/「语义分析」）。<br><code>{rule_name}</code>：命中的规则名；无规则时为空。" },
    { id:"who", term:"{user} / {operator} · 成员与操作人", go:[],
      def:"<code>{user}</code>：被处罚/被操作的成员显示名（群昵称优先）。<br><code>{operator}</code>：执行操作的管理员名。<br>纯文本与 Markdown 都可用；Markdown 下如需 @ 请用 <code>{at}</code>，不要手拼标签。" },
    { id:"reason", term:"{reason} / {reason_line} · 理由", go:[],
      def:"<code>{reason}</code>：简短理由（一般一段）。<br><code>{reason_line}</code>：已格式化的「，理由：…」片段，适合接在句尾。<br>两者都会截断过长内容；纯文本会原样显示，Markdown 下注意里面的符号可能被解析。" },
    { id:"empty", term:"变量为空 / 未知变量", go:[],
      def:"空值替换为空字符串；如果整句依赖它，建议在模板里保留固定措辞。<br>未知变量不会报错，会原样显示 <code>{foo}</code>——保存前请对照本页清单。" }
  ],

  /* ── 常见问题：整理自 docs/GUIDE.md「常见问题（FAQ）」── */
  faq: [
    { id:"q_tplvars", term:"消息模板变量怎么填？", go:[],
      def:"打开右上角「帮助中心 → 模板变量」页：里面有每个变量的含义、纯文本与 Markdown 的差异、空值/未知变量行为。模板页右上角的「消息模板变量怎么填？」按钮会直接跳过去。" },
    { id:"q1", term:"插件加载失败 / WebUI 里看不到插件", go:["global","全局作用域"],
      def:"原因：目录层级不对、metadata.yaml 不完整、或 AstrBot 版本不满足。确认目录是 plugins/astrbot_plugin_qq_group_manager/（别多套一层），确认 metadata.yaml 的 name 为 astrbot_plugin_qq_group_manager 且 astrbot_version 满足当前版本，再看 AstrBot 日志里的插件报错。", doc:null },
    { id:"q2", term:"发送指令没反应 / 提示无权限", go:["judge","管理员判定"],
      def:"逐项确认：管理员条件是否满足、群是否在生效群范围里、指令格式对不对（如 /群管 禁言 @某人 10分钟）。不带 / 的写法必须 @ 机器人；禁言、解禁必须 @ 目标成员；「启用禁言指令」得是开着的。", doc:null },
    { id:"q_kick", term:"踢人提示 40103012/40103011 / 移除群成员失败", go:[],
      def:"两处权限都要开：①手机 QQ 群聊 → 机器人头像 → 右上角齿轮 → 机器人管理员权限设置 → 勾选「移除群成员」；②q.qq.com → 开发设置 →「允许机器人使用的群管理能力」→ 勾选「移除群成员」。机器人需为群管理员，只能移除普通成员。错误码：40103012=群主关闭（手机 QQ 开）；40103011=机器人侧关闭（Web 管理端/开放平台开）；失败会在「记录 → 踢人记录」留档。", doc:null },
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