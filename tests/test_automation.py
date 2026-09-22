import copy
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))
from automation import AutomationStopped, PluginSwitch
import local_ip_harvest
import scheduling


def plugin(state):
    return {"plugin_key": "local.flownode.state-reuse", "state": state, "runtime_healthy": state == "enabled"}


class AutomationTests(unittest.TestCase):
    def test_disabled_cycle_does_not_scan_enroll_or_change_accounts(self):
        cron = scheduling.cron
        with patch.object(cron, "login", return_value={"api_key": "test"}), patch.object(cron, "call", return_value={"data": plugin("disabled")}) as api, patch.object(cron, "sql") as sql, patch.object(cron, "atomic"), patch.object(scheduling, "synchronize") as sync, patch.object(scheduling, "auto_enroll") as enroll:
            scheduling.run_cycle()
        self.assertEqual(api.call_count, 1)
        sql.assert_not_called()
        enroll.assert_not_called()
        sync.assert_not_called()

    def test_enabled_next_cycle_resumes_after_disabled_cycle(self):
        cron = scheduling.cron
        state = "disabled"
        def api(path, *args, **kwargs):
            return {} if path.endswith('/config') else {"data": plugin(state)}
        with patch.object(cron, "login", return_value="token"), patch.object(cron, "call", side_effect=api), patch.object(cron, "atomic"), patch.object(cron, "get_tickets", return_value=[]), patch.object(cron, "STATE") as state_path, patch.object(cron.settings, "AUTO_ENROLL", False), patch.object(scheduling, "synchronize") as sync:
            state_path.exists.return_value = False
            scheduling.run_cycle()
            sync.assert_not_called()
            state = "enabled"
            scheduling.run_cycle()
            sync.assert_called_once()

    def test_new_accounts_are_paused_before_enrollment_and_latest_config_preserved(self):
        cron = scheduling.cron
        old = {"accounts": [1], "proxy_url": "old", "suspended": [1]}
        latest = {"accounts": [1, 4], "proxy_url": "new", "suspended": [4]}
        calls = []
        def api(path, method="GET", data=None, token=None):
            calls.append((path, method, data))
            if path.endswith('/schedulable'):
                return {"data": {"schedulable": False}}
            return copy.deepcopy(latest) if method == "GET" else data
        with patch.object(cron.settings, "AUTO_ENROLL", True), patch.object(cron, "sql", return_value=[{"id": 1, "schedulable": True}, {"id": 2, "schedulable": True}, {"id": 3, "schedulable": False}]) as sql, patch.object(cron, "call", side_effect=api), patch.object(cron.local_ip_harvest, "emit"):
            result = scheduling.auto_enroll(old, "token", Mock())
        self.assertEqual(calls[0], ('/admin/accounts/2/schedulable', 'POST', {'schedulable': False}))
        self.assertEqual(calls[-1][1], 'PUT')
        self.assertEqual(result, dict(latest, accounts=[1, 2, 3, 4]))
        self.assertIn("platform='openai' AND type='oauth'", sql.call_args.args[0])
        self.assertIn("parent_account_id IS NULL", sql.call_args.args[0])

    def test_pause_failure_never_enrolls(self):
        cron = scheduling.cron
        with patch.object(cron.settings, "AUTO_ENROLL", True), patch.object(cron, "sql", return_value=[{"id": 2, "schedulable": True}]), patch.object(cron, "call", return_value={"data": {"schedulable": True}}) as api:
            with self.assertRaises(RuntimeError):
                scheduling.auto_enroll({"accounts": [1]}, "token", Mock())
        self.assertEqual(api.call_count, 1)

    def test_already_enrolled_accounts_do_not_rewrite_configuration(self):
        cron = scheduling.cron
        with patch.object(cron.settings, "AUTO_ENROLL", True), patch.object(cron, "sql", return_value=[{"id": 1, "schedulable": True}]), patch.object(cron, "call") as api:
            scheduling.auto_enroll({"accounts": [1]}, "token", Mock())
        api.assert_not_called()

    def test_disable_before_scheduler_write_prevents_account_change(self):
        guard = Mock()
        guard.require.side_effect = AutomationStopped()
        with patch.object(scheduling.cron, "call") as api:
            with self.assertRaises(AutomationStopped):
                scheduling.synchronize([{"id": 1, "schedulable": True, "eligible": True, "hash": "a" * 64}], [], True, {}, "token", guard)
        api.assert_not_called()

    def test_switch_latches_off_until_next_cycle(self):
        fetch = Mock(side_effect=[plugin("enabled"), plugin("disabled"), plugin("enabled")])
        switch = PluginSwitch(fetch)
        self.assertTrue(switch.enabled(force=True))
        self.assertFalse(switch.enabled(force=True))
        self.assertFalse(switch.enabled(force=True))
        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(PluginSwitch(fetch).enabled(force=True))

    def test_switch_failure_stops_automation(self):
        self.assertFalse(PluginSwitch(Mock(side_effect=OSError())).enabled())

    def test_running_capture_is_cancelled_when_plugin_stops(self):
        checks = iter([True, True, False])
        started = time.monotonic()
        with self.assertRaises(AutomationStopped):
            local_ip_harvest.run_curl([sys.executable, '-c', 'import time; time.sleep(10)'], '', lambda: next(checks))
        self.assertLess(time.monotonic() - started, 3)

    def test_disable_after_verification_prevents_candidate_write(self):
        checks = iter([True, True, True, False])
        with patch.object(local_ip_harvest, "request", return_value=({"completed": True, "actual_model": local_ip_harvest.MODEL}, 'ticket', [])), patch.object(local_ip_harvest.mh, "candidate", return_value={"value": "ticket"}), patch.object(local_ip_harvest.mh, "queue_ticket") as queue, patch.object(local_ip_harvest, "emit"):
            result = local_ip_harvest.collect({"id": 1, "hash": 'a'*64}, [{"key": 'p', "name": 'proxy'}], {}, None, time.monotonic()+150, should_run=lambda: next(checks))
        self.assertTrue(result['automation_stopped'])
        queue.assert_not_called()
