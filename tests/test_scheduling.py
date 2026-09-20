import base64
import hashlib
import importlib.util
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
import scheduling


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.now = int(time.time())
        self.account = {"id": 17, "eligible": True, "schedulable": True, "hash": "a" * 64}

    def ticket(self, issued=None):
        issued = self.now if issued is None else issued
        raw = b"\x80" + issued.to_bytes(8, "big") + bytes(208)
        return {"account_id": 17, "model": "gpt-6-astra", "credential_hash": "a" * 64,
                "issued": issued, "value": base64.urlsafe_b64encode(raw).decode()}

    def test_missing_ticket_stops_scheduling(self):
        with patch.object(scheduling.cron, "call", return_value={"data": {"schedulable": False}}) as api, patch.object(scheduling.cron, "atomic"), patch.object(scheduling.cron.local_ip_harvest, "emit"):
            rows = scheduling.synchronize([self.account], [], True, {}, "token")
        self.assertFalse(rows[0]["business_schedulable"])
        self.assertEqual(api.call_args.args[0], "/admin/accounts/17/schedulable")
        self.assertEqual(api.call_args.args[2], {"schedulable": False})

    def test_verified_ticket_recovers_stopped_account(self):
        self.account["schedulable"] = False
        with patch.object(scheduling.cron, "call", return_value={"data": {"schedulable": True}}) as api, patch.object(scheduling.cron, "atomic"), patch.object(scheduling.cron.local_ip_harvest, "emit"):
            rows = scheduling.synchronize([self.account], [self.ticket()], True, {}, "token")
        self.assertTrue(rows[0]["business_schedulable"])
        self.assertEqual(api.call_args.args[2], {"schedulable": True})

    def test_expiry_credential_model_and_account_isolation(self):
        variants = [self.ticket(self.now - 3541), self.ticket(self.now - 3570)]
        for key, value in [("credential_hash", "b" * 64), ("model", "other"), ("account_id", 18)]:
            ticket = self.ticket()
            ticket[key] = value
            variants.append(ticket)
        for ticket in variants:
            self.assertFalse(scheduling.decision(self.account, [ticket], True, {}, self.now)["business_schedulable"])

    def test_manual_suspension_auth_block_and_disabled_plugin_stay_off(self):
        ticket = self.ticket()
        self.assertFalse(scheduling.decision(self.account, [ticket], False, {}, self.now)["business_schedulable"])
        state = {"17:" + self.account["hash"]: {"auth_block": True}}
        self.assertFalse(scheduling.decision(self.account, [ticket], True, state, self.now)["business_schedulable"])
        self.account["eligible"] = False
        self.assertFalse(scheduling.decision(self.account, [ticket], True, {}, self.now)["business_schedulable"])

    def test_unschedulable_accounts_are_still_collected_in_ticket_mode(self):
        cron = scheduling.cron
        with patch.object(cron.settings, "TICKET_SCHEDULING", True), patch.object(cron, "sql", return_value=[]) as query:
            cron.accounts_from_plugin({"accounts": [17]})
        self.assertNotIn("AND a.schedulable", query.call_args.args[0])
        account = dict(self.account, schedulable=False, platform="openai", type="oauth", token="synthetic", account="synthetic")
        cron.prepare_accounts([account], {"accounts": [17], "suspended": []})
        self.assertTrue(account["eligible"])
