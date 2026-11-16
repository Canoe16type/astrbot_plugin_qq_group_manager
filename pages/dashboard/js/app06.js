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
