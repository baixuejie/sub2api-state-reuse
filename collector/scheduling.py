"""Synchronize business scheduling without blocking the background collector."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

spec = importlib.util.spec_from_file_location("state_gate_cron", Path(__file__).with_name("state-cron.py"))
cron = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cron
spec.loader.exec_module(cron)


def decision(account, tickets, plugin_ready, harvest_state, now):
    per_model = {}
    for model in cron.settings.MODELS:
        candidates = [ticket for ticket in tickets
            if ticket.get("account_id") == account["id"] and ticket.get("model") == model
            and cron.valid(ticket, account, now)]
        newest = max(candidates, key=lambda ticket: ticket["issued"]) if candidates else None
        expires = newest["issued"] + cron.settings.TICKET_TTL_SECONDS if newest else None
        per_model[model] = {"ticket_ready": expires is not None and expires > now + cron.settings.SCHEDULING_MARGIN,
                            "ticket_expires_at": expires}
    missing = [model for model in cron.settings.REQUIRED_MODELS if not per_model[model]["ticket_ready"]]
    ticket_ready = not missing
    expires = min(per_model[model]["ticket_expires_at"] for model in cron.settings.REQUIRED_MODELS) if ticket_ready else None
    auth_blocked = cron.local_ip_harvest.shared_pause(harvest_state, account).get("auth_block", False)
    if not plugin_ready:
        reason = "plugin_unavailable"
    elif not account["eligible"] or auth_blocked:
        reason = "account_paused"
    elif not ticket_ready:
        reason = "waiting_for_ticket"
    else:
        reason = "ticket_ready"
    return {"account_id": account["id"], "business_schedulable": reason == "ticket_ready",
        "ticket_ready": ticket_ready, "ticket_expires_at": expires, "scheduling_reason": reason,
        "model_tickets": per_model, "missing_models": missing,
        "required_models": list(cron.settings.REQUIRED_MODELS)}


def synchronize(accounts, tickets, plugin_ready, harvest_state, token, switch=None):
    now = time.time()
    rows = []
    for account in accounts:
        row = decision(account, tickets, plugin_ready, harvest_state, now)
        desired = row["business_schedulable"]
        if bool(account["schedulable"]) != desired:
            if switch is not None:
                switch.require()
            result = cron.call(f"/admin/accounts/{account['id']}/schedulable", "POST", {"schedulable": desired}, token)
            if result.get("data", {}).get("schedulable") != desired:
                raise RuntimeError("account scheduling update was not applied")
            cron.local_ip_harvest.emit(account["id"], "scheduling_changed",
                business_schedulable=desired, scheduling_reason=row["scheduling_reason"])
        rows.append(row)
    if switch is not None:
        switch.require()
    cron.atomic(cron.ROOT / "scheduling-status.json", {"at": now, "enabled": True, "accounts": rows})
    return rows


def auto_enroll(cfg, token, switch):
    if not cron.settings.AUTO_ENROLL:
        return cfg
    switch.require()
    candidates = cron.sql("""SELECT coalesce(json_agg(json_build_object(
        'id',id,'schedulable',schedulable) ORDER BY id),'[]'::json)
        FROM accounts WHERE deleted_at IS NULL AND platform='openai' AND type='oauth'
        AND parent_account_id IS NULL
        AND coalesce(extra->>'synthetic_ui_test','false') <> 'true'""")
    known = set(cfg.get("accounts") or [])
    new = [account for account in candidates if account["id"] not in known]
    if not new:
        return cfg
    for account in new:
        if type(account["id"]) is not int or account["id"] <= 0:
            raise RuntimeError("invalid discovered account ID")
        switch.require()
        if account["schedulable"]:
            result = cron.call(f"/admin/accounts/{account['id']}/schedulable", "POST", {"schedulable": False}, token)
            if result.get("data", {}).get("schedulable") is not False:
                raise RuntimeError("new account could not be paused before enrollment")
    # Read the current configuration again so changes to the proxy or suspension
    # list made while scanning accounts are preserved.
    switch.require()
    latest = cron.call(f"/admin/plugins/{cron.PLUGIN}/config", token=token)
    updated = dict(latest, accounts=sorted(set(latest.get("accounts") or []) | {a["id"] for a in new}))
    switch.require()
    saved = cron.call(f"/admin/plugins/{cron.PLUGIN}/config", "PUT", updated, token)
    if not all(account["id"] in saved.get("accounts", []) for account in new):
        raise RuntimeError("new accounts were not enrolled")
    for account in new:
        cron.local_ip_harvest.emit(account["id"], "account_enrolled", business_schedulable=False,
            scheduling_reason="waiting_for_ticket")
    return saved


def run_cycle():
    token = cron.login()
    switch = cron.PluginSwitch(lambda: cron.call(f"/admin/plugins/{cron.PLUGIN}", token=token)["data"])
    if not switch.enabled(force=True):
        cron.atomic(cron.ROOT / "automation-status.json", {
            "at": time.time(), "enabled": False, "plugin_state": switch.plugin.get("state", "unavailable")})
        return
    try:
        cfg = cron.call(f"/admin/plugins/{cron.PLUGIN}/config", token=token)
        cfg = auto_enroll(cfg, token, switch)
        switch.require()
        if cfg.get("accounts"):
            accounts = cron.accounts_from_plugin(cfg)
        else:
            accounts = []
        cron.prepare_accounts(accounts, cfg)
        try:
            tickets = cron.get_tickets()
        except (OSError, ValueError):
            tickets = []
        state = json.loads(cron.STATE.read_text()) if cron.STATE.exists() else {}
        ready = switch.plugin.get("runtime_healthy", False)
        synchronize(accounts, tickets, ready, state.get("local_ip_harvest", {}), token, switch)
        cron.atomic(cron.ROOT / "automation-status.json", {
            "at": time.time(), "enabled": True, "plugin_state": "enabled",
            "auto_enroll": cron.settings.AUTO_ENROLL, "accounts": cfg.get("accounts") or []})
    except cron.AutomationStopped:
        cron.atomic(cron.ROOT / "automation-status.json", {
            "at": time.time(), "enabled": False, "plugin_state": switch.plugin.get("state", "unavailable")})


def run():
    if not cron.settings.TICKET_SCHEDULING:
        return
    os.umask(0o077)
    cron.ROOT.mkdir(mode=0o700, exist_ok=True)
    with (cron.ROOT / "scheduling.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        run_cycle()


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        cron.log("scheduling_error", kind=type(error).__name__)
        raise SystemExit(1)
