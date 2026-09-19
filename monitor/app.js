(() => {
  "use strict";
  let paused = false,
    busy = false,
    last = null;
  const $ = (id) => document.getElementById(id);
  const fmt = (v) =>
    v
      ? new Date(typeof v === "number" ? v * 1000 : v).toLocaleString("zh-CN", {
          hour12: false,
        })
      : "—";
  const stage = {
    attempt_started: "开始请求",
    attempt_finished: "请求完成",
    candidate_queued: "复验通过 · 待入库",
    account_finished: "本轮结束",
    paused: "暂停 / 冷却",
  };
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    n.textContent = text ?? "—";
    if (cls) n.className = cls;
    return n;
  }
  function cell(row, text, cls) {
    const td = el("td", "");
    td.append(el("span", text, cls));
    row.append(td);
  }
  function passed(e) {
    return (
      e.renewed ||
      e.captured ||
      (e.event === "attempt_finished" &&
        e.completed &&
        e.actual_model === "gpt-6-astra" &&
        (e.phase === "verify" || [292, 332].includes(e.length)))
    );
  }
  function failed(e) {
    return (
      (e.event === "attempt_finished" && !passed(e)) ||
      (e.event === "account_finished" && !e.renewed)
    );
  }
  function result(e) {
    if (e.event === "attempt_started") return "进行中";
    if (e.event === "paused") return e.auth_block ? "认证暂停" : "等待冷却";
    if (e.event === "candidate_queued") return "携票复验成功";
    if (e.event === "account_finished")
      return e.renewed
        ? "已入库"
        : e.captured
          ? "已采到，待确认入库"
          : "本轮未取得有效票据";
    if (e.transport_error) return "网络错误 · curl " + e.transport_error;
    if (e.error || e.error_code) return e.error_code || e.error;
    if (e.http !== 200) return "HTTP " + e.http;
    if (!e.completed) return "响应未完整结束";
    if (e.actual_model !== "gpt-6-astra") return "模型不匹配";
    if (e.phase === "verify") return "携票复验成功";
    return [292, 332].includes(e.length) ? "取得候选" : "票据长度未达标";
  }
  function render(d) {
    last = d;
    const now = d.server_time,
      tickets = d.tickets.filter((t) => t.expires_at > now),
      valid = new Map(tickets.map((t) => [t.account_id, t]));
    $("ready").textContent = valid.size;
    $("missing").textContent = d.accounts.filter(
      (a) => !valid.has(a.account_id) && a.status !== "paused" && !a.auth_block,
    ).length;
    $("paused").textContent = d.accounts.filter(
      (a) => a.status === "paused" || a.auth_block,
    ).length;
    $("checked").textContent = d.updated_at
      ? new Date(d.updated_at).toLocaleTimeString("zh-CN", { hour12: false })
      : "—";
    $("accounts").replaceChildren();
    for (const a of d.accounts) {
      const t = valid.get(a.account_id),
        stop = a.status === "paused" || a.auth_block,
        row = el("tr", "");
      cell(row, "#" + a.account_id);
      cell(
        row,
        stop
          ? "已暂停"
          : t
            ? "有效 · 不降智"
            : a.status === "model_not_enabled"
              ? "模型未启用"
              : "等待有效票据",
        "badge " + (stop ? "bad" : t ? "good" : "warn"),
      );
      cell(row, t ? t.length + " 字符" : "—");
      cell(row, fmt(t?.expires_at));
      cell(
        row,
        stop
          ? "等待账号恢复"
          : t
            ? fmt(t.issued_at + 3000)
            : a.next_attempt > now
              ? fmt(a.next_attempt)
              : "下一轮检查",
      );
      $("accounts").append(row);
    }
    const selected = $("account-filter").value,
      ids = [
        ...new Set([
          ...d.accounts.map((a) => a.account_id),
          ...d.events.map((e) => e.account_id),
        ]),
      ].sort((a, b) => a - b);
    $("account-filter").replaceChildren(new Option("全部", ""));
    for (const id of ids)
      $("account-filter").add(new Option("#" + id, "" + id));
    $("account-filter").value = selected;
    let events = d.events.filter(
        (e) => !selected || String(e.account_id) === selected,
      ),
      kind = $("result-filter").value;
    if (kind === "success") events = events.filter(passed);
    if (kind === "failure") events = events.filter(failed);
    $("events").replaceChildren();
    for (const e of events.slice().reverse()) {
      const row = el("tr", "");
      cell(row, fmt(e.at));
      cell(row, "#" + e.account_id);
      cell(
        row,
        (stage[e.event] || e.event) +
          (e.phase === "verify" ? " · 携票复验" : ""),
      );
      cell(row, e.source || "—");
      cell(
        row,
        result(e),
        "badge " + (passed(e) ? "good" : failed(e) ? "warn" : ""),
      );
      cell(
        row,
        [e.length ? e.length + " 字符" : "", e.actual_model]
          .filter(Boolean)
          .join(" / ") || "—",
      );
      $("events").append(row);
    }
    if (!events.length) {
      const row = el("tr", ""),
        td = el("td", "暂无匹配事件。新采集开始后会自动显示。", "empty");
      td.colSpan = 6;
      row.append(td);
      $("events").append(row);
    }
    $("updated").textContent =
      "页面更新 " +
      new Date().toLocaleTimeString("zh-CN", { hour12: false }) +
      " · 服务端 " +
      fmt(now) +
      " · " +
      d.events.length +
      " 条事件";
    const age = d.updated_at ? now - Date.parse(d.updated_at) / 1000 : Infinity;
    $("notice").textContent =
      age > 180 ? "采集状态超过 3 分钟未更新，请检查定时任务。" : "";
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      const token = localStorage.getItem("auth_token");
      if (!token) throw new Error("请先在号池登录管理员账号，再打开本页。");
      const r = await fetch("/fn-state/api", {
        headers: { Authorization: "Bearer " + token },
        cache: "no-store",
      });
      const d = await r.json();
      if (!r.ok) {
        if (r.status === 401 || r.status === 403) {
          $("events").replaceChildren();
          $("accounts").replaceChildren();
          last = null;
        }
        throw new Error(d.error || "读取失败");
      }
      render(d);
      $("live").textContent = paused ? "已暂停" : "● 每 3 秒刷新";
    } catch (e) {
      $("notice").textContent = e.message;
      $("live").textContent = "连接异常";
    } finally {
      busy = false;
    }
  }
  $("toggle").onclick = () => {
    paused = !paused;
    $("toggle").textContent = paused ? "继续刷新" : "暂停刷新";
    $("live").textContent = paused ? "已暂停" : "● 每 3 秒刷新";
    if (!paused) refresh();
  };
  $("refresh").onclick = refresh;
  for (const id of ["account-filter", "result-filter"])
    $(id).onchange = () => {
      if (last) render(last);
    };
  setInterval(() => {
    if (!paused && !document.hidden) refresh();
  }, 3000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !paused) refresh();
  });
  refresh();
})();
