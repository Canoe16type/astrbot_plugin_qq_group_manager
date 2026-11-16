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
    $("lex-rows").innerHTML = `<tr><td colspan="5">${emptyBox("warn", "加载失败：" + esc(e.message))}</td></tr>`;
    return;
  }
  if (!r || r.error) {
    $("lex-rows").innerHTML = `<tr><td colspan="5">${emptyBox("warn", "加载失败：" + esc((r && r.error) || "未知错误"))}</td></tr>`;
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
