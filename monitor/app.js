(() => {
  "use strict";
  const ASTRA = "gpt-6-astra", SOL = "gpt-5.6-sol";
  const names = { [ASTRA]: "Astra", [SOL]: "Sol" };
  const $ = (id) => document.getElementById(id);
  let paused = false, busy = false, last = null;
  const fmt = (value) => value ? new Date(typeof value === "number" ? value * 1000 : value)
    .toLocaleString("zh-CN", { hour12: false }) : "—";
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function cell(row, text, cls) {
    const td = el("td");
    td.append(el("span", text, cls));
    row.append(td);
    return td;
  }
  function badge(text, tone) { return el("span", text, "badge " + tone); }
  function until(expires, now) {
    const seconds = Math.max(0, Math.floor(expires - now));
    return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
  }
  function hasTicket(account, model, now) {
    const detail = account.models?.[model] || {};
    const expires = detail.ticket_expires_at ?? detail.expires_at;
    return !!expires && expires > now;
  }
  function passed(event) {
    const model = event.model || ASTRA;
    return !!(event.renewed || event.captured || (event.event === "attempt_finished" &&
      event.completed && event.actual_model === model &&
      (event.phase === "verify" || [292, 332].includes(event.length))));
  }
  function failed(event) {
    return (event.event === "attempt_finished" && !passed(event)) ||
      (event.event === "account_finished" && !event.renewed);
  }
  function outcome(event, model) {
    if (!event) return "尚未探测";
    if (event.event === "account_enrolled") return "自动入队 · 等待有效票据";
    if (event.event === "scheduling_changed") return event.business_schedulable ? "恢复调度" : "停止调度";
    if (event.event === "attempt_started") return "请求中";
    if (event.event === "paused") return event.auth_block ? "认证暂停" : "等待冷却";
    if (event.event === "candidate_queued") return "携票复验成功 · 待入库";
    if (event.renewed) return "已入库";
    if (event.event === "account_finished") return event.captured ? "已采到 · 待确认入库" : "本轮未取得有效票据";
    if (event.transport_error) return `网络错误 · curl ${event.transport_error}`;
    if (event.error || event.error_code) return event.error_code || event.error;
    if (event.http !== 200) return event.http ? `HTTP ${event.http}` : "未收到响应";
    if (!event.completed) return "响应未完整结束";
    if (event.actual_model !== (model || event.model || ASTRA)) return `返回 ${event.actual_model || "未知模型"}`;
    if (event.phase === "verify") return "携票复验通过";
    return [292, 332].includes(event.length) ? "获得候选票据" : `票据未达标 · ${event.length || 0} 字符`;
  }
  const stages = { attempt_started: "开始请求", attempt_finished: "请求完成", candidate_queued: "候选交接",
    account_finished: "本轮完成", paused: "暂停 / 冷却", scheduling_changed: "调度变更", account_enrolled: "自动入队" };

  function modelCell(account, model, now, automationPaused) {
    const td = el("td", undefined, "model-cell");
    td.dataset.model = model;
    const detail = account.models?.[model] || {};
    const expires = detail.ticket_expires_at ?? detail.expires_at;
    const valid = hasTicket(account, model, now);
    const blocked = detail.status === "paused" || detail.auth_block;
    const label = valid ? "有效票据" : automationPaused ? "自动化暂停" : blocked ? "账号暂停" :
      detail.status === "model_not_enabled" ? "账号未启用此模型" : detail.status === "pending" ? "等待首轮探测" : "等待有效票据";
    td.append(badge(label, valid ? "good" : blocked ? "bad" : "warn"));
    if (valid) {
      td.append(el("span", `${detail.length || "—"} 字符 · 剩余 ${until(expires, now)}`, "sub"));
      const expiry = el("span", `到期 ${fmt(expires)}`, "sub");
      expiry.title = "票据有效期与模型能力无直接等价关系";
      td.append(expiry);
      const meter = el("div", undefined, "meter"), bar = el("i");
      bar.style.width = `${Math.max(0, Math.min(100, (expires - now) / 240 * 100))}%`;
      meter.append(bar); td.append(meter);
    }
    const probe = detail.last_probe;
    if (probe) {
      td.append(el("span", `最近：${outcome(probe, model)}`, "sub"));
      td.append(el("span", fmt(probe.at), "sub"));
    }
    const next = valid ? (detail.issued_at ? detail.issued_at + 150 : expires - 90) : detail.next_attempt;
    if (!automationPaused && !blocked && detail.status !== "model_not_enabled") {
      td.append(el("span", next > now ? `${valid ? "续采" : "下次尝试"} ${fmt(next)}` : "下一轮检查", "sub"));
    }
    return td;
  }

  function emptyRow(target, columns, message) {
    const row = el("tr"), td = el("td", message, "empty");
    td.colSpan = columns; row.append(td); target.append(row);
  }
  function render(data) {
    last = data;
    const now = data.server_time, accounts = data.accounts || [], automationPaused = data.automation?.enabled === false;
    const ready = accounts.filter((a) => a.business_schedulable);
    const waiting = accounts.filter((a) => a.scheduling_reason === "waiting_for_ticket");
    $("ready").textContent = automationPaused ? "已暂停" : `${ready.length} / ${accounts.length}`;
    $("astra-ready").textContent = `${accounts.filter((a) => hasTicket(a, ASTRA, now)).length} / ${accounts.length}`;
    $("sol-ready").textContent = `${accounts.filter((a) => hasTicket(a, SOL, now)).length} / ${accounts.length}`;
    $("missing").textContent = waiting.length;
    $("paused").textContent = `账号暂停：${accounts.filter((a) => a.scheduling_reason === "account_paused").length}`;
    $("checked").textContent = `最近采集检查：${fmt(data.updated_at)}`;
    const required = data.policy?.required_models || [ASTRA];
    $("policy").textContent = `调度条件：${required.map((m) => names[m] || m).join(" + ")} 票据有效 · 每 20 秒检查`;
    const query = $("account-search").value.toLowerCase().trim(), state = $("state-filter").value;
    const visible = accounts.filter((a) => (!query || `${a.account_id} ${a.account_name || ""}`.toLowerCase().includes(query)) &&
      (!state || (state === "ready" && a.business_schedulable) || (state === "waiting" && a.scheduling_reason === "waiting_for_ticket") ||
       (state === "paused" && a.scheduling_reason === "account_paused")));
    $("account-count").textContent = `${visible.length} / ${accounts.length}`;
    $("accounts").replaceChildren();
    for (const account of visible) {
      const row = el("tr"); row.dataset.accountId = account.account_id;
      const identity = cell(row, `#${account.account_id}`);
      identity.append(el("span", account.account_name || "", "sub"));
      if (account.plan) identity.append(el("span", account.plan.toUpperCase(), "sub"));
      const enabled = account.business_schedulable;
      const label = automationPaused ? "自动化已暂停" : enabled ? "可调度" :
        account.scheduling_reason === "waiting_for_ticket" ? "停调 · 刷票中" : "停调 · 等待恢复";
      const scheduling = cell(row, label, "badge " + (automationPaused ? "warn" : enabled ? "good" : "warn"));
      scheduling.append(el("span", enabled ? "Astra 已满足调度条件" :
        (account.missing_models || []).length ? `缺少 ${(account.missing_models || []).map((m) => names[m] || m).join("、")}` : "", "sub"));
      row.append(modelCell(account, ASTRA, now, automationPaused), modelCell(account, SOL, now, automationPaused));
      $("accounts").append(row);
    }
    if (!visible.length) emptyRow($("accounts"), 4, query || state ? "没有匹配的账号，请调整筛选。" : "等待自动入队和首轮采集。");

    const selected = $("account-filter").value;
    const ids = [...new Set([...accounts.map((a) => a.account_id), ...(data.events || []).map((e) => e.account_id)])].sort((a, b) => a - b);
    $("account-filter").replaceChildren(new Option("全部", ""));
    for (const id of ids) $("account-filter").add(new Option(`#${id}`, String(id)));
    $("account-filter").value = selected;
    const model = $("model-filter").value, kind = $("result-filter").value;
    let events = (data.events || []).filter((event) => (!selected || String(event.account_id) === selected) && (!model || event.model === model));
    if (kind === "success") events = events.filter(passed);
    if (kind === "failure") events = events.filter(failed);
    $("events").replaceChildren();
    for (const event of events.slice().reverse()) {
      const row = el("tr");
      cell(row, fmt(event.at)); cell(row, `#${event.account_id}`);
      cell(row, names[event.model] || event.model || "账号", event.model === SOL ? "attention" : "");
      cell(row, (stages[event.event] || event.event) + (event.phase === "verify" ? " · 携票复验" : ""));
      cell(row, event.source || "—");
      cell(row, outcome(event), "badge " + (passed(event) ? "good" : failed(event) ? "warn" : ""));
      cell(row, [event.length ? `${event.length} 字符` : "", event.actual_model].filter(Boolean).join(" / ") || "—");
      $("events").append(row);
    }
    if (!events.length) emptyRow($("events"), 7, "暂无匹配事件。可切换模型、账号或结果筛选。");
    $("updated").textContent = `页面更新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 服务端 ${fmt(now)} · ${events.length} 条事件`;
    const age = data.updated_at ? now - Date.parse(data.updated_at) / 1000 : Infinity;
    const unavailable = data.automation?.plugin_state === "unavailable";
    $("notice").textContent = automationPaused
      ? unavailable ? "无法读取插件状态，自动化已暂停。请检查管理密钥和主站连接。" : "插件未启用，自动化已暂停。启用后会自动恢复。"
      : age > 180 ? "采集状态超过 3 分钟未更新，请检查定时任务。" : "";
  }
  function clearPrivateData() {
    $("accounts").replaceChildren(); $("events").replaceChildren();
    for (const id of ["ready", "astra-ready", "sol-ready", "missing"]) $(id).textContent = "—";
    $("account-filter").replaceChildren(new Option("全部", ""));
    $("account-count").textContent = ""; $("checked").textContent = "最近采集检查：—";
    $("paused").textContent = "账号暂停：—"; last = null;
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const token = localStorage.getItem("auth_token");
      if (!token) { clearPrivateData(); throw new Error("请先登录主站管理员账号，再打开本页。"); }
      const response = await fetch("/fn-state/api", { headers: { Authorization: "Bearer " + token }, cache: "no-store" });
      const data = await response.json();
      if (!response.ok) {
        if ([401, 403].includes(response.status)) clearPrivateData();
        throw new Error(data.error || "读取失败");
      }
      render(data); $("live").textContent = paused ? "已暂停刷新" : "● 每 3 秒刷新";
    } catch (error) {
      $("notice").textContent = error.message; $("live").textContent = "连接异常";
    } finally { busy = false; }
  }
  $("toggle").onclick = () => {
    paused = !paused; $("toggle").textContent = paused ? "继续刷新" : "暂停刷新";
    $("live").textContent = paused ? "已暂停刷新" : "● 每 3 秒刷新";
    if (!paused) refresh();
  };
  $("refresh").onclick = refresh;
  for (const id of ["account-filter", "model-filter", "result-filter", "state-filter"])
    $(id).onchange = () => { if (last) render(last); };
  $("account-search").oninput = () => { if (last) render(last); };
  setInterval(() => { if (!paused && !document.hidden) refresh(); }, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !paused) refresh(); });
  refresh();
})();
