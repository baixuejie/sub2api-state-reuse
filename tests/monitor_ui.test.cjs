const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "monitor/index.html"), "utf8");
const script = fs.readFileSync(path.join(root, "monitor/app.js"), "utf8");
const ASTRA = "gpt-6-astra", SOL = "gpt-5.6-sol";
function fixture() {
  const now = Date.now() / 1000;
  return { server_time: now, updated_at: new Date().toISOString(), scheduling_enabled: true,
    automation: { enabled: true }, policy: { required_models: [ASTRA] }, tickets: [],
    accounts: [{ account_id: 17, account_name: '<img src=x onerror="alert(1)">', plan: "pro",
      business_schedulable: true, scheduling_reason: "ticket_ready", missing_models: [],
      models: { [ASTRA]: { status: "fresh", length: 292, issued_at: now-100, expires_at: now+140 },
                [SOL]: { status: "missing", next_attempt: now+20, last_probe: { at: now, http: 200, actual_model: "gpt-5.6-luna", completed: true, length:312 } } } },
      { account_id: 18, account_name: "Team test", business_schedulable: false, scheduling_reason: "waiting_for_ticket", missing_models: [ASTRA],
        models: { [ASTRA]: { status: "missing" }, [SOL]: { status: "fresh", length:332, expires_at: now+200 } } }],
    events: [{ at:now, account_id:17, event:"attempt_finished", model:SOL, http:200, completed:true, actual_model:SOL, length:292 },
             { at:now, account_id:18, event:"attempt_finished", model:ASTRA, http:200, completed:true, actual_model: "gpt-5.6-luna", length:312 }] };
}
async function mount(data) {
  const dom = new JSDOM(html, { url: "https://example.invalid/admin/state-harvest", runScripts: "outside-only" });
  dom.window.localStorage.setItem("auth_token", "synthetic");
  dom.window.fetch = async () => ({ ok:true, status:200, json:async () => data });
  dom.window.eval(script);
  await new Promise((resolve) => setImmediate(resolve));
  return dom;
}

test("one row per account with independent Astra/Sol state and no HTML injection", async () => {
  const dom = await mount(fixture());
  try {
    const document = dom.window.document;
    assert.equal(document.querySelectorAll("#accounts tr").length, 2);
    assert.equal(document.querySelectorAll("#accounts img").length, 0);
    assert.match(document.querySelector('[data-account-id="17"]').textContent, /可调度/);
    assert.match(document.querySelector('[data-account-id="17"] [data-model="gpt-5.6-sol"]').textContent, /等待有效票据/);
    assert.match(document.querySelector('[data-account-id="18"] [data-model="gpt-5.6-sol"]').textContent, /332 字符/);
    assert.equal(document.getElementById("ready").textContent, "1 / 2");
    assert.equal(document.getElementById("sol-ready").textContent, "1 / 2");
    assert.match(document.getElementById("policy").textContent, /Astra 票据有效/);
  } finally { dom.window.close(); }
});

test("model/result filters recognize successful Sol and search retains scope", async () => {
  const dom = await mount(fixture());
  try {
    const document = dom.window.document;
    const model = document.getElementById("model-filter"), result = document.getElementById("result-filter");
    model.value = SOL; result.value = "success"; model.dispatchEvent(new dom.window.Event("change"));
    assert.equal(document.querySelectorAll("#events tr").length, 1);
    assert.match(document.getElementById("events").textContent, /Sol.*获得候选票据/);
    const search = document.getElementById("account-search");
    search.value = "18"; search.dispatchEvent(new dom.window.Event("input"));
    assert.equal(document.querySelectorAll("#accounts tr").length, 1);
    assert.equal(document.querySelector("#accounts tr").dataset.accountId, "18");
    assert.equal(model.value, SOL);
  } finally { dom.window.close(); }
});

test("expired auth removes account data and counters", async () => {
  const dom = await mount(fixture());
  try {
    dom.window.fetch = async () => ({ ok:false, status:401, json:async () => ({error:"请重新登录"}) });
    dom.window.document.getElementById("refresh").click();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(dom.window.document.querySelectorAll("#accounts tr").length, 0);
    assert.equal(dom.window.document.getElementById("ready").textContent, "—");
    assert.equal(dom.window.document.getElementById("notice").textContent, "请重新登录");
  } finally { dom.window.close(); }
});

test("unavailable management API is distinguished from disabled plugin", async () => {
  const data = fixture(); data.automation = {enabled:false,plugin_state:"unavailable"};
  const dom = await mount(data);
  try {
    assert.match(dom.window.document.getElementById("notice").textContent, /管理密钥/);
    assert.match(dom.window.document.querySelector('[data-account-id="17"]').textContent, /自动化已暂停/);
  } finally { dom.window.close(); }
});

test("egress addresses and cumulative attempts are distinct from confirmed rotations", async () => {
  const data = fixture();
  Object.assign(data.events[0], {egress_ip:"8.8.8.8",egress_status:"confirmed",attempt_no:7,ip_changes:2,ip_changed:true});
  Object.assign(data.events[1], {egress_status:"connection_changed",attempt_no:8,ip_changes:2});
  data.accounts[0].models[ASTRA].egress = {attempts:7,ip_changes:2,last_confirmed_ip:"8.8.8.8"};
  const dom = await mount(data);
  try {
    const events = dom.window.document.getElementById("events").textContent;
    assert.match(events, /8\.8\.8\.8/); assert.match(events, /第 7 次尝试/);
    assert.match(events, /已确认换 IP 2 次/); assert.match(events, /连接已变化/);
    assert.match(dom.window.document.getElementById("accounts").textContent, /累计尝试 7 次/);
    assert.equal(dom.window.document.querySelector('a[href="/admin/plugins"]').textContent, "配置采集代理 ↗");
  } finally { dom.window.close(); }
});
