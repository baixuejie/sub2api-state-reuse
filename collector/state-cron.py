#!/usr/bin/env python3
"""Rotate isolated proxy exits, queue verified candidates, classify by Astra tickets."""

import local_ip_harvest
import sys
import base64, concurrent.futures, datetime, fcntl, hashlib, json, os, pathlib, subprocess, time, urllib.request, urllib.error
import settings

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
    d = call(
        "/auth/login",
        "POST",
        {"email": env["ADMIN_EMAIL"], "password": env["ADMIN_PASSWORD"]},
    )["data"]
    token = d.get("access_token") or d.get("token")
    if not token:
        raise RuntimeError("admin login requires interactive authentication")
    return token


def call(path, method="GET", data=None, token=None, stream=False):
    h = {"Content-Type": "application/json"}
    if token:
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
            and now < issued + 3570
            and t["credential_hash"] == a["hash"]
        )
    except (ValueError, KeyError, TypeError):
        return False


def get_tickets():
    try:
        return json.loads(STORE.read_text())
    except FileNotFoundError:
        return []


def sync_groups(accounts, tickets, token):
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
        run_locked()


def run_locked():
    if settings.READY_GROUP == settings.FALLBACK_GROUP:
        raise RuntimeError("ready and fallback groups must differ")
    now = int(time.time())
    q = """select json_build_object('groups',(select json_agg(json_build_object('id',id,'name',name)) from groups where name='不降智分组' and deleted_at is null),'accounts',(select coalesce(json_agg(json_build_object('id',a.id,'platform',a.platform,'type',a.type,'status',a.status,'schedulable',a.schedulable,'eligible',(a.status='active' and a.schedulable and (a.rate_limit_reset_at is null or a.rate_limit_reset_at<now()) and (a.overload_until is null or a.overload_until<now()) and (a.temp_unschedulable_until is null or a.temp_unschedulable_until<now()) and (a.expires_at is null or a.expires_at>now())),'token',a.credentials->>'access_token','account',a.credentials->>'chatgpt_account_id','plan',a.credentials->>'plan_type','mapping',a.credentials->'model_mapping')), '[]'::json) from accounts a where a.deleted_at is null and exists(select 1 from account_groups ag join groups g on g.id=ag.group_id where ag.account_id=a.id and g.name in ('不降智分组','降智分组') and g.deleted_at is null)))"""
    d = sql(q)
    groups = d["groups"] or []
    if len(groups) != 1:
        raise RuntimeError("target group missing or ambiguous")
    accounts = d["accounts"]
    ids = sorted(a["id"] for a in accounts)
    group_id = groups[0]["id"]
    # Never send an empty account scope to the host: [] means every account.
    if not ids:
        raise RuntimeError("empty target group; existing protection kept unchanged")
    for a in accounts:
        a["eligible"] = bool(
            a["eligible"]
            and a["platform"] == "openai"
            and a["type"] == "oauth"
            and a["token"]
            and a["account"]
        )
        a["hash"] = hashlib.sha256(
            ((a["token"] or "") + ":" + (a["account"] or "")).encode()
        ).hexdigest()
    suspended = sorted(a["id"] for a in accounts if not a["eligible"])
    token = login()
    plugin = call(f"/admin/plugins/{PLUGIN}", token=token)["data"]
    if plugin["plugin_key"] != "local.flownode.state-reuse":
        raise RuntimeError("plugin identity mismatch")
    if plugin["state"] != "enabled":
        raise RuntimeError("plugin disabled; do not override manual stop")
    cfg = call(f"/admin/plugins/{PLUGIN}/config", token=token)
    # Retain previously protected accounts as well as current members; moving a group must not bypass protection.
    desired_ids = sorted(set(ids + cfg.get("accounts", [])))
    # Retain existing suspension for accounts outside this group.
    desired_suspended = sorted(
        set(suspended + [i for i in cfg.get("suspended", []) if i not in ids])
    )
    desired = dict(cfg, accounts=desired_ids, suspended=desired_suspended)
    if desired != cfg:
        call(f"/admin/plugins/{PLUGIN}/config", "PUT", desired, token)
        log("config_sync", accounts=desired_ids, suspended=desired_suspended)
    b = next(
        b
        for b in plugin["bindings"]
        if b["capability"] == "openai.oauth.protection_transport.v1"
    )
    if (
        sorted(b["account_ids"]) != desired_ids
        or b["group_ids"]
        or b["user_ids"]
        or b["rollout_percent"] != 100
    ):
        plugin = call(f"/admin/plugins/{PLUGIN}", token=token)["data"]
        call(
            f"/admin/plugins/{PLUGIN}/routing",
            "PUT",
            {
                "expected_updated_at": plugin["updated_at"],
                "policies": [
                    {
                        "capability": b["capability"],
                        "priority": 0,
                        "account_ids": desired_ids,
                        "group_ids": [],
                        "user_ids": [],
                        "rollout_percent": 100,
                        "max_concurrency": 64,
                        "timeout_ms": 0,
                    }
                ],
            },
            token,
        )
        log("routing_sync", accounts=desired_ids)
    if plugin["state"] != "enabled":
        raise RuntimeError("plugin disabled; do not override manual stop")
    state = json.loads(STATE.read_text()) if STATE.exists() else {"attempts": {}}
    tickets = get_tickets()
    sync_groups(accounts, tickets, token)
    jobs = []
    summary = []
    for a in accounts:
        models = {MODEL}
        mapping = a.get("mapping") or {}
        for model in sorted(models):
            k = f"{a['id']}:{model}"
            ts = [
                t
                for t in tickets
                if t.get("account_id") == a["id"]
                and t.get("model") == model
                and valid(t, a, now)
            ]
            newest = max(ts, key=lambda t: t["issued"]) if ts else None
            status = (
                "fresh"
                if newest and now < newest["issued"] + 3000
                else "renew_due"
                if newest
                else "missing"
            )
            if not a["eligible"]:
                status = "paused"
            elif mapping and model not in mapping and "*" not in mapping:
                status = "model_not_enabled"
            summary.append(
                {
                    "account_id": a["id"],
                    "model": model,
                    "status": status,
                    "expires_at": newest["issued"] + 3570 if newest else None,
                }
            )
            if status in ("renew_due", "missing") and now - state["attempts"].get(
                k, 0
            ) >= local_ip_harvest.retry_interval(status):
                jobs.append((a["id"], model, local_ip_harvest.retry_interval(status)))
                state["attempts"][k] = now
    atomic(STATE, state)
    results = []
    routes = local_ip_harvest.routes_for(sys.modules[__name__])
    harvest_state = state.setdefault("local_ip_harvest", {})
    deadline = time.monotonic() + 150

    def worker(item):
        id, model, interval = item
        a = next(a for a in accounts if a["id"] == id)
        result = local_ip_harvest.collect(
            a, routes, harvest_state, STORE, deadline, interval=interval
        )
        # The cycle lock protects collection; all worker cooldowns are saved after the batch.
        ok = False
        if result.get("captured"):
            try:
                ok = call(
                    f"/admin/accounts/{id}/test",
                    "POST",
                    {"model_id": MODEL},
                    token,
                    stream=True,
                )
            except Exception:
                pass
        renewed = any(
            t.get("account_id") == id
            and t.get("model") == MODEL
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
            captured=result.get("captured", False),
            renewed=renewed,
            test_success=ok,
        )
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        for result in ex.map(worker, jobs):
            results.append(result)
    atomic(STATE, state)
    final = get_tickets()
    sync_groups(accounts, final, token)
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
                    "expires_at": t["issued"] + 3570,
                }
            )
    for row in summary:
        a = a_by_id[row["account_id"]]
        st = state.get("local_ip_harvest", {}).get(str(a["id"]) + ":" + a["hash"], {})
        row["next_attempt"] = max(
            state["attempts"].get(str(a["id"]) + ":" + MODEL, 0)
            + local_ip_harvest.retry_interval(row["status"]),
            st.get("next_attempt", 0),
        )
        row["auth_block"] = bool(st.get("auth_block", False))
    report = {
        "at": datetime.datetime.now().astimezone().isoformat(),
        "group_id": group_id,
        "group_name": GROUP,
        "monitored_accounts": ids,
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
