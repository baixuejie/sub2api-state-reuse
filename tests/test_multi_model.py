import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
import local_ip_harvest as harvest
import scheduling
import settings
import ticket_store

ASTRA, SOL = settings.MODELS


class MultiModelTests(unittest.TestCase):
    def setUp(self):
        self.account = {"id": 17, "hash": "a" * 64, "eligible": True, "schedulable": False}
        self.value = base64.urlsafe_b64encode(b'\x80' + int(time.time()).to_bytes(8, 'big') + bytes(208)).decode()
        self.routes = [{"name": "private route", "key": "p", "url": "socks5://example.invalid:1000"}]

    def test_capture_and_verify_use_sol_and_queue_sol_candidate(self):
        with patch.object(harvest, "request", return_value=({"completed": True, "actual_model": SOL}, self.value)) as request, patch.object(harvest, "emit"), patch.object(ticket_store, "queue_ticket") as queue:
            result = harvest.collect(self.account, self.routes, {}, None, time.monotonic()+150, model=SOL)
        self.assertTrue(result['captured'])
        self.assertEqual(len(request.call_args_list), 2)
        self.assertTrue(all(call.kwargs['model'] == SOL for call in request.call_args_list))
        self.assertEqual(request.call_args_list[1].args[2], self.value)
        self.assertEqual(queue.call_args.args[1]['model'], SOL)

    def test_astra_response_to_sol_request_is_rejected(self):
        with patch.object(harvest, "request", return_value=({"completed": True, "actual_model": ASTRA}, self.value)), patch.object(harvest, "emit"), patch.object(ticket_store, "queue_ticket") as queue:
            result = harvest.collect(self.account, self.routes, {}, None, time.monotonic()+150, model=SOL)
        self.assertFalse(result['captured'])
        queue.assert_not_called()

    def test_failed_sol_verification_is_not_queued(self):
        with patch.object(harvest, "request", side_effect=[({"completed": True, "actual_model": SOL}, self.value), ({"completed": True, "actual_model": ASTRA}, '')]), patch.object(harvest, "emit"), patch.object(ticket_store, "queue_ticket") as queue:
            harvest.collect(self.account, self.routes, {}, None, time.monotonic()+150, model=SOL)
        queue.assert_not_called()

    def test_two_models_have_distinct_candidates(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(settings, "PLUGIN_UID", os.getuid()), patch.object(settings, "PLUGIN_GID", os.getgid()):
            store = Path(temporary)/'tickets.json'
            for model in settings.MODELS:
                ticket_store.queue_ticket(store, ticket_store.candidate(self.account, self.value, model))
            files = list((store.parent/'incoming').glob('*.json'))
            self.assertEqual(len(files), 2)
            self.assertEqual({json.loads(file.read_text())['model'] for file in files}, set(settings.MODELS))

    def test_missing_sol_does_not_block_astra_scheduling(self):
        ticket = ticket_store.candidate(self.account, self.value, ASTRA)
        with patch.object(settings, "REQUIRED_MODELS", (ASTRA,)):
            result = scheduling.decision(self.account, [ticket], True, {}, time.time())
        self.assertTrue(result['business_schedulable'])
        self.assertFalse(result['model_tickets'][SOL]['ticket_ready'])
        self.assertEqual(result['missing_models'], [])

    def test_sol_cannot_substitute_for_astra_scheduling(self):
        ticket = ticket_store.candidate(self.account, self.value, SOL)
        with patch.object(settings, "REQUIRED_MODELS", (ASTRA,)):
            result = scheduling.decision(self.account, [ticket], True, {}, time.time())
        self.assertFalse(result['business_schedulable'])

    def test_model_retries_independent_but_auth_and_quota_are_shared(self):
        state = {}
        with patch.object(harvest, "request", return_value=({"completed": True, "actual_model": "other"}, '')) as request, patch.object(harvest, "emit"):
            for model in settings.MODELS:
                harvest.collect(self.account, self.routes, state, None, time.monotonic()+150, model=model)
        self.assertEqual(request.call_count, 2)
        for first, second in [(ASTRA, SOL), (SOL, ASTRA)]:
            for result in [{'stop': True, 'cooldown': 7200}, {'stop': True, 'auth_block': True}]:
                state = {}
                with patch.object(harvest, "request", return_value=(result, '')) as request, patch.object(harvest, "emit"):
                    harvest.collect(self.account, self.routes, state, None, time.monotonic()+150, model=first)
                    harvest.collect(self.account, self.routes, state, None, time.monotonic()+150, model=second)
                self.assertEqual(request.call_count, 1)

    def test_legacy_cooldown_is_preserved_for_sol(self):
        state = {harvest.account_key(self.account): {'next_attempt': time.time()+7200}}
        with patch.object(harvest, "request") as request, patch.object(harvest, "emit"):
            harvest.collect(self.account, self.routes, state, None, time.monotonic()+150, model=SOL)
        request.assert_not_called()

    def test_request_payload_contains_target_model(self):
        def curl(args, cfg, should_run):
            entries = dict(line.split(' = ', 1) for line in cfg.splitlines())
            payload = json.loads(json.loads(entries['data']))
            self.assertEqual(payload['model'], SOL)
            Path(json.loads(entries['dump-header'])).write_text('HTTP/2 200\r\n')
            Path(json.loads(entries['output'])).write_text('data: '+json.dumps({'type':'response.completed','response':{'model':SOL}})+'\n')
            return subprocess.CompletedProcess(args, 0)
        with patch.object(harvest, 'run_curl', side_effect=curl):
            result, _ = harvest.request(dict(self.account, token='synthetic', account='synthetic'), self.routes[0], model=SOL)
        self.assertEqual(result['model'], SOL)
        self.assertEqual(result['actual_model'], SOL)
        self.assertTrue(result['completed'])

    def test_summary_matches_fresh_sol_after_same_cycle_import(self):
        ticket = ticket_store.candidate(self.account, self.value, SOL)
        state = {'attempts': {}, 'last_results': {'17:'+SOL: {'renewed': True}}}
        row = scheduling.cron.model_summary(self.account, SOL, [ticket], state, time.time())
        self.assertEqual(row['status'], 'fresh')
        self.assertEqual(row['length'], 292)
        self.assertTrue(row['last_probe']['renewed'])


class MultiModelMonitorTests(unittest.TestCase):
    def test_one_account_two_models_and_separate_ticket_state(self):
        spec = importlib.util.spec_from_file_location('monitor_models', ROOT/'monitor/server.py')
        monitor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(monitor)
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor, 'ROOT', Path(directory)):
            (Path(directory)/'cron-status.json').write_text(json.dumps({
                'models': [ASTRA, SOL], 'required_models': [ASTRA],
                'before': [{'account_id': 17, 'model': ASTRA, 'status':'fresh', 'expires_at':2000},
                           {'account_id': 17, 'model': SOL, 'status':'missing', 'last_probe':{'http':200,'actual_model':'other','token':'SECRET'}}]}))
            result = monitor.snapshot()
        self.assertEqual(len(result['accounts']), 1)
        self.assertEqual(result['accounts'][0]['models'][ASTRA]['status'], 'fresh')
        self.assertEqual(result['accounts'][0]['models'][SOL]['status'], 'missing')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['policy']['required_models'], [ASTRA])
