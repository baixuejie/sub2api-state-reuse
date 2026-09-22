#!/usr/bin/env python3
"""Rotate isolated proxy exits, queue verified candidates, classify by Astra tickets."""

import local_ip_harvest
import sys
import base64, concurrent.futures, datetime, fcntl, hashlib, json, os, pathlib, subprocess, time, urllib.request, urllib.error
import settings
import fnmatch
from automation import AutomationStopped, PluginSwitch

ROOT = settings.ROOT
STORE = settings.STORE
STATE = ROOT / "cron-state.json"
GROUP = settings.READY_GROUP
PLUGIN = settings.PLUGIN_ID
MODEL = settings.MODEL


def atomic(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def log(event, **kw):
    print(
        json.dumps(
            {
                "time": datetime.datetime.now()
                .astimezone()
                .isoformat(timespec="seconds"),
                "event": event,
                **kw,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def sql(query):
    query = query.replace(
        "'不降智分组'", settings.sql_literal(settings.READY_GROUP)
    ).replace("'降智分组'", settings.sql_literal(settings.FALLBACK_GROUP))
    b = subprocess.check_output(
        [
            "docker",
            "exec",
            settings.PG_CONTAINER,
            "sh",
            "-c",
            'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "$1"',
            "_",
            query,
        ],
        timeout=20,
    )
    return json.loads(b)


def login():
    env = {}
    for line in settings.ADMIN_ENV.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k] = v.strip().strip('"').strip("'")
    if env.get("ADMIN_API_KEY"):
        return {"api_key": env["ADMIN_API_KEY"]}
    d = call(
        "/auth/login",
        "POST",
        {"email": env["ADMIN_EMAIL"], "password": env["ADMIN_PASSWORD"]},
    )["data"]
    token = d.get("access_token") or d.get("token")
    if not token:
        raise RuntimeError("admin login requires interactive authentication")
    return token


def call(path, method="GET", data=None, token=None, stream=False, should_run=None):
    h = {"Content-Type": "application/json"}
    if isinstance(token, dict):
        h["x-api-key"] = token["api_key"]
    elif token:
        h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(
        settings.BASE_URL + path,
        method=method,
        headers=h,
        data=json.dumps(data).encode() if data is not None else None,
    )
    with urllib.request.urlopen(r, timeout=90 if stream else 25) as f:
        if not stream:
            return json.load(f)
        ok = False
        error = False
        for line in f:
            if should_run is not None and not should_run():
                raise AutomationStopped()
            if not line.startswith(b"data:"):
                continue
            try:
                e = json.loads(line[5:])
            except ValueError:
                continue
            if e.get("type") == "test_complete":
                ok = bool(e.get("success"))
            if e.get("type") == "error":
                error = True
        return ok and not error


def valid(t, a, now):
    try:
        b = base64.b64decode(t["value"], altchars=b"-_", validate=True)
        issued = int.from_bytes(b[1:9], "big")
        return (
            (len(t["value"]), len(b)) in ((292, 217), (332, 249))
            and b[0] == 128
            and issued == t["issued"]
            and issued <= now + 30
            and now < issued + settings.TICKET_TTL_SECONDS
            and t["credential_hash"] == a["hash"]
        )
    except (ValueError, KeyError, TypeError):
        return False


def get_tickets():
    try:
        return json.loads(STORE.read_text())
    except FileNotFoundError:
        return []


def accounts_from_plugin(cfg):
    ids = cfg.get("accounts") or []
    if not ids or any(type(i) is not int or i <= 0 for i in ids):
        raise RuntimeError("plugin account scope must be explicit and nonempty")
    scope = ",".join(str(i) for i in sorted(set(ids)))
    schedule_filter = "" if settings.TICKET_SCHEDULING else " AND a.schedulable"
    return sql("""SELECT coalesce(json_agg(json_build_object(
        'id',a.id,'name',a.name,'platform',a.platform,'type',a.type,'status',a.status,
        'schedulable',a.schedulable,
        'eligible',(a.status='active'""" + schedule_filter + """
            AND (a.rate_limit_reset_at IS NULL OR a.rate_limit_reset_at<now())
            AND (a.overload_until IS NULL OR a.overload_until<now())
            AND (a.temp_unschedulable_until IS NULL OR a.temp_unschedulable_until<now())
            AND (a.expires_at IS NULL OR a.expires_at>now())),
        'token',a.credentials->>'access_token',
        'account',a.credentials->>'chatgpt_account_id',
        'plan',a.credentials->>'plan_type','mapping',a.credentials->'model_mapping'
    )), '[]'::json) FROM accounts a WHERE a.deleted_at IS NULL
    AND a.platform='openai' AND a.type='oauth' AND a.id IN (""" + scope + ")")


def prepare_accounts(accounts, cfg):
    for account in accounts:
        account["eligible"] = bool(
            account["eligible"]
            and account["platform"] == "openai"
            and account["type"] == "oauth"
            and account["token"]
            and account["account"]
            and (settings.ACCOUNT_SCOPE != "plugin" or account["id"] not in (cfg.get("suspended") or []))
        )
        account["hash"] = hashlib.sha256(
            ((account["token"] or "") + ":" + (account["account"] or "")).encode()
        ).hexdigest()


def model_enabled(account, model):
    mapping = account.get("mapping") or {}
    return not mapping or any(fnmatch.fnmatchcase(model, pattern) for pattern in mapping)


def model_summary(account, model, tickets, state, now):
    candidates = [t for t in tickets if t.get("account_id") == account["id"]
                  and t.get("model") == model and valid(t, account, now)]
    newest = max(candidates, key=lambda t: t["issued"]) if candidates else None
    status = "fresh" if newest and now < newest["issued"] + settings.TICKET_REFRESH_AFTER_SECONDS else "renew_due" if newest else "missing"
    shared = local_ip_harvest.shared_pause(state.get("local_ip_harvest", {}), account)
    model_state = state.get("local_ip_harvest", {}).get(local_ip_harvest.model_key(account, model), {})
    auth_block = bool(shared.get("auth_block") or model_state.get("auth_block"))
    if not account["eligible"] or auth_block:
        status = "paused"
    elif not model_enabled(account, model):
        status = "model_not_enabled"
    last = state.get("last_results", {}).get(f"{account['id']}:{model}")
    return {"account_id": account["id"], "account_name": account.get("name", ""),
        "plan": account.get("plan"), "model": model, "status": status,
        "expires_at": newest["issued"] + settings.TICKET_TTL_SECONDS if newest else None,
        "issued_at": newest["issued"] if newest else None,
        "length": len(newest["value"]) if newest else None,
        "next_attempt": max(state["attempts"].get(f"{account['id']}:{model}", 0)
            + local_ip_harvest.retry_interval(status), model_state.get("next_attempt", 0), shared.get("next_attempt", 0)),
        "auth_block": auth_block, "last_probe": last}


def collection_routes(cfg):
    if settings.PROXY_SOURCE != "plugin":
        return local_ip_harvest.routes_for(sys.modules[__name__])
    from urllib.parse import urlsplit, urlunsplit

    raw = cfg.get("proxy_url") or ""
    proxy = urlsplit(raw)
    if proxy.scheme not in ("http", "https", "socks5", "socks5h") or not proxy.hostname:
        raise RuntimeError("plugin harvest proxy is missing or invalid")
    if proxy.scheme == "socks5":
        raw = urlunsplit(proxy._replace(scheme="socks5h"))
    return [{"key": "plugin-proxy", "name": "已配置采集代理", "url": raw}]


def sync_groups(accounts, tickets, token, switch=None):
    if not settings.AUTO_GROUP:
        return
    groups = sql(
        "select json_agg(json_build_object('id',id,'name',name)) from groups where name in ('不降智分组','降智分组') and deleted_at is null"
    )
    by_name = {g["name"]: g["id"] for g in groups}
    if len(groups) != 2 or len(by_name) != 2:
        raise RuntimeError("classification groups missing or ambiguous")
    good = by_name[settings.READY_GROUP]
    bad = by_name[settings.FALLBACK_GROUP]
    for a in accounts:
        ready = any(
            t.get("account_id") == a["id"]
            and t.get("model") == MODEL
            and valid(t, a, time.time())
            for t in tickets
        )
        current = sql(
            f"select coalesce(json_agg(group_id),'[]'::json) from account_groups where account_id={int(a['id'])}"
        )
        desired = sorted((set(current) - {good, bad}) | {good if ready else bad})
        if sorted(current) != desired:
            if switch is not None:
                switch.require()
            call(
                f"/admin/accounts/{a['id']}",
                "PUT",
                {"group_ids": desired, "confirm_mixed_channel_risk": True},
                token,
            )
            log(
                "group_classification",
                account_id=a["id"],
                astra_ticket=ready,
                group_ids=desired,
            )


def run():
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    with (ROOT / "cron.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            run_locked()
        except AutomationStopped:
            log("automation_paused")


def run_locked():
    if settings.AUTO_GROUP and settings.READY_GROUP == settings.FALLBACK_GROUP:
        raise RuntimeError("ready and fallback groups must differ")
    now = int(time.time())
    token = login()
    switch = PluginSwitch(lambda: call(f"/admin/plugins/{PLUGIN}", token=token)["data"])
    if not switch.enabled(force=True):
        return
    plugin = switch.plugin
    if plugin["plugin_key"] != "local.flownode.state-reuse":
        raise RuntimeError("plugin identity mismatch")
    if plugin["state"] != "enabled":
        log("plugin_disabled")
        return
    cfg = call(f"/admin/plugins/{PLUGIN}/config", token=token)
    switch.require()
    q = """select json_build_object('groups',(select json_agg(json_build_object('id',id,'name',name)) from groups where name='不降智分组' and deleted_at is null),'accounts',(select coalesce(json_agg(json_build_object('id',a.id,'platform',a.platform,'type',a.type,'status',a.status,'schedulable',a.schedulable,'eligible',(a.status='active' and a.schedulable and (a.rate_limit_reset_at is null or a.rate_limit_reset_at<now()) and (a.overload_until is null or a.overload_until<now()) and (a.temp_unschedulable_until is null or a.temp_unschedulable_until<now()) and (a.expires_at is null or a.expires_at>now())),'token',a.credentials->>'access_token','account',a.credentials->>'chatgpt_account_id','plan',a.credentials->>'plan_type','mapping',a.credentials->'model_mapping')), '[]'::json) from accounts a where a.deleted_at is null and exists(select 1 from account_groups ag join groups g on g.id=ag.group_id where ag.account_id=a.id and g.name in ('不降智分组','降智分组') and g.deleted_at is null)))"""
    if settings.ACCOUNT_SCOPE == "plugin":
        if not cfg.get("accounts"):
            return
        accounts = accounts_from_plugin(cfg)
        group_id = None
    else:
        d = sql(q)
        groups = d["groups"] or []
        if len(groups) != 1:
            raise RuntimeError("target group missing or ambiguous")
        accounts = d["accounts"]
        group_id = groups[0]["id"]
    ids = sorted(a["id"] for a in accounts)
    # Never send an empty account scope to the host: [] means every account.
    if not ids:
        raise RuntimeError("empty target group; existing protection kept unchanged")
    prepare_accounts(accounts, cfg)
    suspended = sorted(a["id"] for a in accounts if not a["eligible"])
    # Retain previously protected accounts as well as current members; moving a group must not bypass protection.
    desired_ids = sorted(set(ids + cfg.get("accounts", [])))
    # Retain existing suspension for accounts outside this group.
    desired_suspended = sorted(
        set(suspended + [i for i in cfg.get("suspended", []) if i not in ids])
    )
    desired = dict(cfg, accounts=desired_ids, suspended=desired_suspended)
    if settings.ACCOUNT_SCOPE != "plugin" and desired != cfg:
        switch.require()
        call(f"/admin/plugins/{PLUGIN}/config", "PUT", desired, token)
        log("config_sync", accounts=desired_ids, suspended=desired_suspended)
    # The original v1 host has no per-account routing API. Scope is enforced
    # inside this plugin; non-selected accounts are forwarded without tickets.
    if not any(
        b["capability"] == "openai.oauth.outbound_transport.v1" and b.get("enabled")
        for b in plugin["bindings"]
    ):
        raise RuntimeError("v1 OAuth transport binding is not enabled")
    if plugin["state"] != "enabled":
        raise RuntimeError("plugin disabled; do not override manual stop")
    state = json.loads(STATE.read_text()) if STATE.exists() else {"attempts": {}}
    tickets = get_tickets()
    sync_groups(accounts, tickets, token, switch)
    jobs = {}
    for account in accounts:
        for model in settings.MODELS:
            row = model_summary(account, model, tickets, state, now)
            key = f"{account['id']}:{model}"
            if row["status"] in ("renew_due", "missing") and row["next_attempt"] <= now:
                jobs.setdefault(account["id"], []).append((account["id"], model,
                    local_ip_harvest.retry_interval(row["status"])))
                state["attempts"][key] = now
    atomic(STATE, state)
    results = []
    routes = collection_routes(cfg)
    harvest_state = state.setdefault("local_ip_harvest", {})
    deadline = time.monotonic() + 150

    def worker(item):
        id, model, interval = item
        a = next(a for a in accounts if a["id"] == id)
        result = local_ip_harvest.collect(
            a, routes, harvest_state, STORE, deadline, interval=interval,
            should_run=switch.enabled, model=model,
        )
        # The cycle lock protects collection; all worker cooldowns are saved after the batch.
        ok = False
        if result.get("captured"):
            try:
                switch.require()
                ok = call(
                    f"/admin/accounts/{id}/test",
                    "POST",
                    {"model_id": model},
                    token,
                    stream=True,
                    should_run=switch.enabled,
                )
            except Exception:
                pass
        renewed = any(
            t.get("account_id") == id
            and t.get("model") == model
            and valid(t, a, time.time())
            and t["issued"] >= now
            for t in get_tickets()
        )
        result.update(
            test_success=ok, renewed=renewed, business_proxy="sub-account-configured"
        )
        local_ip_harvest.emit(
            id,
            "account_finished",
            model=model,
            captured=result.get("captured", False),
            renewed=renewed,
            test_success=ok,
        )
        return result

    def account_worker(items):
        # Keep one account's capture/verify sequence serial, so a 401/403/429
        # from either model prevents the next model from sending more probes.
        return [worker(item) for item in items]

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        for batch in ex.map(account_worker, jobs.values()):
            results.extend(batch)
    for result in results:
        if result.get("probes"):
            probe = result["probes"][-1]
            state.setdefault("last_results", {})[f"{result['account_id']}:{result['model']}"] = {
                "at": time.time(), "http": probe.get("http"), "actual_model": probe.get("actual_model"),
                "length": probe.get("length"), "phase": probe.get("phase"),
                "completed": bool(probe.get("completed")), "transport_error": probe.get("transport_error"),
                "error": probe.get("error"), "error_code": probe.get("error_code"),
                "captured": bool(result.get("captured")), "renewed": bool(result.get("renewed"))}
    atomic(STATE, state)
    final = get_tickets()
    switch.require()
    sync_groups(accounts, final, token, switch)
    a_by_id = {a["id"]: a for a in accounts}
    stored = []
    for t in final:
        a = a_by_id.get(t.get("account_id"))
        if a and valid(t, a, time.time()):
            stored.append(
                {
                    "account_id": a["id"],
                    "model": t["model"],
                    "length": len(t["value"]),
                    "issued_at": t["issued"],
                    "expires_at": t["issued"] + settings.TICKET_TTL_SECONDS,
                }
            )
    summary = [model_summary(account, model, final, state, time.time())
               for account in accounts for model in settings.MODELS]
    report = {
        "at": datetime.datetime.now().astimezone().isoformat(),
        "group_id": group_id,
        "group_name": GROUP if settings.AUTO_GROUP else None,
        "monitored_accounts": ids,
        "models": list(settings.MODELS),
        "required_models": list(settings.REQUIRED_MODELS),
        "members": [
            a["id"]
            for a in accounts
            if any(
                t.get("account_id") == a["id"]
                and t.get("model") == MODEL
                and valid(t, a, time.time())
                for t in final
            )
        ],
        "suspended": suspended,
        "before": summary,
        "attempts": results,
        "valid_tickets": stored,
    }
    atomic(ROOT / "cron-status.json", report)
    if jobs or any(s["status"] not in ("fresh", "paused") for s in summary):
        log(
            "cycle",
            members=ids,
            attempts=results,
            valid_tickets=stored,
            suspended=suspended,
        )


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log("error", kind=type(e).__name__)
        raise SystemExit(1)
