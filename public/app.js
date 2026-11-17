// TaskBoard frontend — self-written vanilla JS (MIT). All numbers come from
// /api/summary, which the server aggregates live from task details.
const STATUS_ZH = {todo:"待办", in_progress:"进行中", pending_acceptance:"待验收", done:"已完成",
  awaiting_user:"待用户拍板", blocked:"被卡住", shelved:"已搁置", cancelled:"已取消"};
const PERIOD_ZH = {day:"日", week:"周", month:"月", year:"年"};
const $ = id => document.getElementById(id);
// Escape ALL user/Bot-written text before it goes into innerHTML (titles,
// evidence, history notes, project names...). Stored data stays raw; the
// render layer is the single escaping point (fix for TESTREPORT-01 P2 XSS).
function esc(v){
  return String(v==null?"":v).replace(/[&<>"']/g, ch =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
}
let state = {summary:null, tasks:[], selected:null, period:"day"};

function todayStr(){ const d=new Date(); return d.toISOString().slice(0,10); }
function qs(){
  const p = new URLSearchParams();
  // CHANGE-03: statistics period + anchor date. The server derives the
  // [range_start, range_end] window from these two; project/status/q stack.
  p.set("period", state.period);
  p.set("anchor", $("f-date").value || todayStr());
  if ($("f-project").value) p.set("project", $("f-project").value);
  if ($("f-status").value) p.set("status", $("f-status").value);
  if ($("f-q").value.trim()) p.set("q", $("f-q").value.trim());
  return p.toString();
}
async function load(){
  const [s, t] = await Promise.all([
    fetch("/api/summary?" + qs()).then(r=>r.json()),
    fetch("/api/tasks?" + qs()).then(r=>r.json()),
  ]);
  state.summary = s; state.tasks = t.tasks;
  render();
}
function ringSVG(rate){
  const r=52, c=2*Math.PI*r, pct = rate==null?0:rate;
  const off = c*(1-pct/100);
  return `<svg width="130" height="130" viewBox="0 0 130 130">
    <circle cx="65" cy="65" r="${r}" fill="none" stroke="#e8edf2" stroke-width="13"/>
    <circle cx="65" cy="65" r="${r}" fill="none" stroke="#0E7BD0" stroke-width="13"
      stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${off}"
      transform="rotate(-90 65 65)"/>
    <text x="65" y="72" text-anchor="middle" font-size="26" font-weight="700">${rate==null?"—":Math.round(rate)+"%"}</text></svg>`;
}
function render(){
  const s = state.summary;
  $("ring").innerHTML = ringSVG(s.total_rate);
  $("ring-frac").textContent = `${s.n_done} / ${s.n_total}`;
  $("assistant-rate").textContent = s.assistant_rate==null ? "—" : Math.round(s.assistant_rate)+"%";
  $("assistant-detail").textContent =
    `${s.assistant_numerator} / ${s.assistant_denominator}（剔除 ${s.n_excluded} 项：待拍板/外部卡住/已搁置）`;
  $("n-done").textContent = s.n_done; $("n-total").textContent = s.n_total;
  $("status-split").textContent = Object.entries(s.by_status)
    .filter(([,v])=>v>0).map(([k,v])=>`${STATUS_ZH[k]} ${v}`).join(" · ");
  $("pending-my").textContent = s.pending_my_action;
  $("n-blocked").textContent = s.by_status.blocked;
  $("inbox-badge").textContent = "待我处理 " + s.pending_my_action;
  $("range-label").textContent = s.range_start
    ? `${PERIOD_ZH[s.period]||"日"}周期 · ${s.range_start} ~ ${s.range_end}` : "全部日期";
  $("filter-note").textContent = `当前周期：${s.range_start||"—"} ~ ${s.range_end||"—"}，${s.n_done} 已完成 / ${s.n_total} 有效总数（待我处理为当前时点数，不随周期变）`;
  // project select (fill once)
  const sel = $("f-project");
  if (sel.options.length <= 1) s.by_project.forEach(p=>{
    const o=document.createElement("option"); o.value=p.project; o.textContent=p.project; sel.appendChild(o); });
  // bars
  $("bars").innerHTML = s.by_project.map(p=>`
    <div class="bar-row"><div>${esc(p.project)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${p.rate||0}%"></div></div>
      <div>${p.done}/${p.total} · ${p.rate==null?"—":Math.round(p.rate)+"%"}</div></div>`).join("")
    || '<p class="sub">该筛选下暂无任务</p>';
  renderGantt(); renderList(); renderSelfCheck(); renderLicense();
  $("excluded-list").innerHTML = s.excluded.map(e=>
    `<div>• [${esc(e.project)}] ${esc(e.title)} — ${esc(STATUS_ZH[e.status]||e.status)}${e.reason?("："+esc(e.reason)):""}</div>`).join("")
    || "无剔除项";
}
function renderGantt(){
  const sched = state.tasks.filter(t=>t.started_at && t.due_at && t.status!=="cancelled");
  const unsched = state.tasks.filter(t=>!(t.started_at && t.due_at) && t.status!=="cancelled");
  $("unscheduled").innerHTML = unsched.map(t=>
    `<li>[${esc(t.project)}] ${esc(t.title)} — ${esc(STATUS_ZH[t.status]||t.status)} ${t.progress}%</li>`).join("") || "<li>无</li>";
  // Axis ALWAYS covers the full selected PERIOD (CHANGE-03 §3; the
  // TESTREPORT-01 P1 rule generalised: never shrink the axis to the
  // scheduled tasks' span, or the today line falls outside it).
  // Boundaries are UTC, matching the Z-suffixed timestamps in the data.
  const s = state.summary || {};
  const period = s.period || "day";
  const rs = s.range_start, re = s.range_end;
  const anchorSel = $("f-date").value || todayStr();
  const t0 = Date.parse((rs || anchorSel) + "T00:00:00Z");
  const t1 = Date.parse((re || anchorSel) + "T00:00:00Z") + 24*3600e3;
  const span = t1 - t0;
  const days = Math.round(span / 86400e3);
  const today = todayStr();
  const hasToday = rs ? (rs <= today && today <= re) : (anchorSel === today);
  const nowMs = Date.now();
  const W = period==="month" ? Math.max(760, days*36) : period==="year" ? 900 : 760;
  const rowH=26, labelW=300;
  const X = ms => labelW + (ms - t0)/span*(W-labelW);
  // Tick granularity per spec §3: day -> hours, week/month -> days, year -> months.
  let ticks = "";
  if (period === "day"){
    for (const h of [0,6,12,18,24]){
      const tx = X(t0 + h*3600e3);
      ticks += `<line x1="${tx}" y1="18" x2="${tx}" y2="22" stroke="#999"/><text x="${tx-8}" y="14" font-size="10" fill="#666">${String(h).padStart(2,"0")}:00</text>`;
    }
  } else if (period === "year"){
    const y0 = new Date(t0).getUTCFullYear();
    for (let m = 0; m < 12; m++){
      const tx = X(Date.UTC(y0, m, 1));
      ticks += `<line x1="${tx}" y1="18" x2="${tx}" y2="22" stroke="#999"/><text x="${tx-8}" y="14" font-size="10" fill="#666">${m+1}月</text>`;
    }
  } else { // week / month: one tick per day
    for (let d = 0; d < days; d++){
      const tx = X(t0 + d*86400e3);
      const label = period === "week"
        ? new Date(t0 + d*86400e3).toISOString().slice(5,10)
        : String(d + 1);
      ticks += `<line x1="${tx}" y1="18" x2="${tx}" y2="22" stroke="#999"/><text x="${tx-8}" y="14" font-size="10" fill="#666">${label}</text>`;
    }
  }
  const todayLineSVG = hasToday
    ? `<line class="today-line" x1="${X(nowMs)}" y1="0" x2="${X(nowMs)}" y2="__H__" stroke="#c0392b" stroke-width="1.5" stroke-dasharray="4 3"/><text x="${X(nowMs)+3}" y="12" font-size="11" fill="#c0392b">今日线</text>` : "";
  $("gantt-note").textContent =
    `时间轴为所选${PERIOD_ZH[period]||"日"}周期全程 ${rs||anchorSel} ~ ${re||anchorSel}（UTC），` +
    (period==="day" ? "小时刻度。" : period==="year" ? "月刻度（年档只做概览，不标日精度）。" : "日刻度。") +
    (hasToday
      ? "所选周期包含今天：红色虚线为今日线，位置与当前时刻对应。"
      : "所选周期不包含今天：不绘制今日线。");
  if (!sched.length){
    $("gantt").innerHTML = `<svg width="${W}" height="40">${ticks}${todayLineSVG.replace("__H__", 40)}</svg>`
      + '<p class="sub">本周期暂无带起止时间的任务（其余都在下方未排期区，未伪造时间）。</p>';
    return;
  }
  const groups = {};
  sched.forEach(t=>{ (groups[t.project]=groups[t.project]||[]).push(t); });
  let y=24, rows=ticks;
  for (const [proj, list] of Object.entries(groups)){
    rows += `<text x="4" y="${y+16}" font-size="13" font-weight="700">${esc(proj)}</text>`; y+=rowH;
    for (const t of list){
      const bx = Math.max(X(Math.max(Date.parse(t.started_at), t0)), labelW);
      const bw = Math.min(Math.max(X(Math.min(Date.parse(t.due_at), t1)) - bx, 8), W - bx);
      const color = t.status==="done" ? "#2e9e4f" : t.status==="blocked" ? "#8e44ad" : "#0E7BD0";
      rows += `<text x="10" y="${y+15}" font-size="12">${esc(t.title.slice(0,26))}</text>
        <rect x="${bx}" y="${y+3}" width="${bw}" height="14" rx="4" fill="${color}" opacity="0.3"/>
        <rect x="${bx}" y="${y+3}" width="${bw*t.progress/100}" height="14" rx="4" fill="${color}"/>
        <text x="${bx+bw+5}" y="${y+15}" font-size="11">${t.progress}%</text>`;
      y+=rowH;
    }
  }
  // Today line (CHANGE-03 §3): drawn ONLY when today falls inside the
  // selected period, positioned at the current moment on the period axis.
  const todayLine = todayLineSVG.replace("__H__", y);
  $("gantt").innerHTML = `<svg width="${W}" height="${y+6}">${rows}${todayLine}</svg>`;
}
function renderList(){
  $("task-list").innerHTML = state.tasks.map(t=>`
    <div class="task" data-id="${esc(t.id)}">
      <span class="pill ${esc(t.status)}">${esc(STATUS_ZH[t.status]||t.status)}</span>
      <b>${esc(t.title)}</b>
      <span class="prog"><i style="width:${t.progress}%"></i></span> ${t.progress}%
      <div class="meta">${esc(t.id)} · ${esc(t.project)} · ${esc(t.assignee_bot)} · 更新 ${esc(t.updated_at.slice(0,16).replace("T"," "))}</div>
    </div>`).join("") || '<p class="sub">暂无任务</p>';
  document.querySelectorAll(".task").forEach(el=>
    el.addEventListener("click", ()=>showDetail(el.dataset.id)));
}
async function showDetail(id){
  const t = await fetch("/api/tasks/"+id).then(r=>r.json());
  state.selected = t;
  const canAct = t.status==="pending_acceptance";
  $("detail").innerHTML = `
    <p><b>${esc(t.title)}</b></p>
    <div class="kv">编号 ${esc(t.id)} · 项目 ${esc(t.project)} · 状态 ${esc(STATUS_ZH[t.status]||t.status)} · 进度 ${t.progress}%<br>
    负责人 ${esc(t.assignee_bot)} · 归属日 ${esc(t.date)}${t.carried_over?"（结转）":""}<br>
    开始 ${esc(t.started_at||"—")} · 截止 ${esc(t.due_at||"—")} · 完成 ${esc(t.completed_at||"—")}</div>
    ${t.description?`<p class="kv">${esc(t.description)}</p>`:""}
    ${t.blocker_reason?`<p class="kv">卡点（${esc(t.blocker_type||"—")}）：${esc(t.blocker_reason)}</p>`:""}
    <p class="kv">证据：</p><ul>${(t.evidence||[]).map(e=>`<li>${esc(e)}</li>`).join("")||"<li>无</li>"}</ul>
    ${canAct?`<button class="secondary" id="btn-accept">验收通过</button>
      <button class="danger" id="btn-reject">打回</button>`:""}
    <p class="kv">历史：</p><ul>${t.history.map(h=>
      `<li>${esc(h.at.slice(0,16).replace("T"," "))} ${esc(h.by)}：${h.from?esc(STATUS_ZH[h.from]||h.from):"新建"} → ${esc(STATUS_ZH[h.to]||h.to)} ${esc(h.note||"")}</li>`).join("")}</ul>`;
  if (canAct){
    $("btn-accept").addEventListener("click", ()=>act(id,"accept",{}));
    $("btn-reject").addEventListener("click", ()=>{
      const reason = prompt("打回原因（必填）："); if (reason) act(id,"reject",{reason}); });
  }
}
async function act(id, action, body){
  const token = $("f-token").value.trim();
  if (!token){ alert("请先在顶栏填入用户令牌（见 README 的 tokens.json）"); return; }
  const r = await fetch(`/api/tasks/${id}/${action}`, {method:"POST",
    headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},
    body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok){ alert("操作失败：" + (j.detail||j.error)); return; }
  await load(); showDetail(id);
}
function renderSelfCheck(){
  const s = state.summary, el = $("selfcheck");
  const ok = s.self_check.consistent;
  el.className = "card selfcheck" + (ok?"":" bad");
  el.textContent = ok
    ? `数据自检 ✓ 明细 ${s.self_check.detail_count} 条 = 各状态计数之和 ${s.self_check.status_sum}；百分比均由明细实时聚合，可逐条复算。`
    : `数据自检 ✗ 明细 ${s.self_check.detail_count} 条 ≠ 状态计数之和 ${s.self_check.status_sum}，百分比可能不可信！`;
}
// ---- Enterprise license (CHANGE-02 reservation layer) -------------------
// Unified gate entry for enterprise features: the UI (and any future
// enterprise view block) reads ONLY window.TB_ENTERPRISE / licenseStatus()
// — never a local boolean of its own. Entitlement comes from a short-lived,
// remotely signed token verified server-side; the Community edition does
// not depend on it in any way.
async function fetchLicenseStatus(){
  try {
    const s = await fetch("/api/license/status").then(r=>r.json());
    window.TB_ENTERPRISE = {entitled: !!s.enterprise_entitled, features: s.features||[], status: s};
    return s;
  } catch(e){
    window.TB_ENTERPRISE = {entitled:false, features:[], status:null};
    return null;
  }
}
async function renderLicense(){
  const el = $("lic-status"); if (!el) return;
  const s = await fetchLicenseStatus();
  if (!s){ el.textContent = "企业版权益：状态读取失败（免费版不受影响）"; return; }
  el.textContent = s.enterprise_entitled
    ? `企业版权益：已激活（${(s.features||[]).join(", ")||"enterprise"}，到期 ${s.expires_at||"—"}，远程签发、本地验签）`
    : `企业版权益：未激活（状态 ${s.state}${s.license_server_configured?"":"，授权服务器未配置"}）— 免费版全部功能不受影响`;
}
async function licenseAct(action){
  const token = $("f-token").value.trim();
  if (!token){ alert("请先在顶栏填入用户令牌"); return; }
  const body = action==="activate" ? {license_key: $("lic-key").value.trim()} : {};
  if (action==="activate" && !body.license_key){ alert("请填入注册码"); return; }
  const r = await fetch(`/api/license/${action}`, {method:"POST",
    headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},
    body: JSON.stringify(body)});
  const j = await r.json().catch(()=>({}));
  if (!r.ok) alert("企业版" + action + "失败：" + (j.detail||j.error||r.status) + "（免费版不受影响）");
  renderLicense();
}
$("btn-today").addEventListener("click", ()=>{ $("f-date").value = todayStr(); load(); });
document.querySelectorAll("#f-period button").forEach(b=>
  b.addEventListener("click", ()=>{
    state.period = b.dataset.period; // anchor unchanged: only the period math changes (spec §1)
    document.querySelectorAll("#f-period button").forEach(x=>x.classList.toggle("active", x===b));
    load();
  }));
$("btn-excluded").addEventListener("click", ()=>$("excluded-list").classList.toggle("hidden"));
["f-date","f-project","f-status"].forEach(id=>$(id).addEventListener("change", load));
$("f-q").addEventListener("input", ()=>{ clearTimeout(window._qt); window._qt=setTimeout(load,300); });
$("f-token").value = localStorage.getItem("tb_token") || "";
$("f-token").addEventListener("change", ()=>localStorage.setItem("tb_token", $("f-token").value.trim()));
$("f-date").value = "2026-11-17"; // seed day; use 今天 button for current day
["btn-lic-activate","btn-lic-verify","btn-lic-deactivate"].forEach(id=>{
  const b = $(id); if (b) b.addEventListener("click", ()=>licenseAct(id.replace("btn-lic-","")));
});
load().catch(e=>{ $("selfcheck").textContent = "数据自检 ✗ 加载失败：" + e; $("selfcheck").classList.add("bad"); });
