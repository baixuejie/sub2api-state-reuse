import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'collector'))
import egress
import local_ip_harvest as harvest
import settings


class Response(io.BytesIO):
    status = 200


class EgressTests(unittest.TestCase):
    def test_only_same_connection_trace_is_attributed(self):
        transfers = {'upstream': {'exitcode': 0}, 'trace': {'exitcode': 0, 'http_code': 200, 'num_connects': 0}}
        trace = 'h=chatgpt.com\nip=8.8.8.8\n'
        self.assertEqual(egress.observation(transfers, trace)['egress_ip'], '8.8.8.8')
        for key, value in [('num_connects', 1), ('http_code', 403), ('exitcode', 28)]:
            result = egress.observation(dict(transfers, trace=dict(transfers['trace'], **{key: value})), trace)
            self.assertNotIn('egress_ip', result)
        for body in ['ip=8.8.8.8', 'h=elsewhere\nip=8.8.8.8', 'h=chatgpt.com\nip=127.0.0.1', 'h=chatgpt.com\nip=<script>']:
            self.assertNotIn('egress_ip', egress.observation(transfers, body))

    def test_rotation_counts_confirmed_changes_and_survives_reload(self):
        state = {}; stats = egress.counters(state, 7, settings.MODEL)
        stats['attempts'] += 1
        for address in ['8.8.8.8', '8.8.8.8', None, '1.1.1.1']:
            result = {'egress_status': 'confirmed' if address else 'connection_changed', 'egress_ip': address}
            egress.record(stats, result)
        self.assertEqual(stats['requests'], 4)
        self.assertEqual(stats['ip_changes'], 1)
        restored = egress.counters(json.loads(json.dumps(state)), 7, settings.MODEL)
        self.assertEqual(restored, stats)
        self.assertEqual(egress.counters(state, 7, 'gpt-5.6-sol')['ip_changes'], 0)

    def test_trace_has_no_account_headers_and_failure_preserves_primary_success(self):
        for trace_exit in (0, 28):
            def curl(args, cfg, should_run):
                first, second = cfg.split('\nnext\n')
                self.assertIn('Authorization', first)
                self.assertNotIn('Authorization', second)
                self.assertNotIn('Cookie', second)
                self.assertNotIn('ChatGPT-Account-Id', second)
                def options(text):
                    return {k: json.loads(v) for k, v in (line.split(' = ', 1) for line in text.splitlines() if ' = ' in line)}
                main, trace = options(first), options(second)
                Path(main['dump-header']).write_text('HTTP/1.1 200 OK\r\n')
                Path(main['output']).write_text('data: '+json.dumps({'type':'response.completed','response':{'model':settings.MODEL}})+'\n')
                Path(trace['output']).write_text('h=chatgpt.com\nip=8.8.8.8\n')
                stdout = json.dumps({'transfer':'upstream','exitcode':0})+'\n'+json.dumps({'transfer':'trace','exitcode':trace_exit,'http_code':200,'num_connects':0})
                return subprocess.CompletedProcess(args, trace_exit, stdout, '')
            with patch.object(harvest, 'run_curl', side_effect=curl):
                result, _, _ = harvest.request({'token':'SECRET','account':'PRIVATE'}, {'name':'test','url':'http://proxy:80'}, cookies=['secret=value'])
            self.assertTrue(result['completed'])
            self.assertNotIn('transport_error', result)
            self.assertEqual(result.get('egress_ip'), '8.8.8.8' if trace_exit == 0 else None)
            self.assertNotIn('SECRET', json.dumps(result))

    def test_generator_resolved_for_each_capture_and_reused_for_verification(self):
        route = {'name':'动态 IP 提取接口','key':'plugin-generator','generator_url':'https://provider.example/custom?key=SECRET'}
        replies = [Response(b'8.8.8.8:8000\n'), Response(b'1.1.1.1:8001\n')]
        with patch.object(harvest.urllib.request, 'urlopen', side_effect=replies) as fetch:
            first = harvest.resolve_route(route); second = harvest.resolve_route(route)
        self.assertEqual(fetch.call_count, 2)
        self.assertNotEqual(first['url'], second['url'])
        account = {'id':7,'hash':'a'*64}
        state = {}
        with patch.object(harvest, 'resolve_route', return_value=first) as resolve, patch.object(harvest, 'request', return_value=({'completed':True,'actual_model':settings.MODEL},'ticket',[])) as request, patch.object(harvest.mh, 'candidate', return_value={'value':'ticket'}), patch.object(harvest.mh,'queue_ticket'), patch.object(harvest,'emit'):
            result = harvest.collect(account, [route], state, None, time.monotonic()+150)
        self.assertTrue(result['captured'])
        resolve.assert_called_once()
        self.assertEqual(request.call_args_list[0].args[1]['url'], request.call_args_list[1].args[1]['url'])
        self.assertEqual(result['probes'][0]['attempt_no'], result['probes'][1]['attempt_no'])

    def test_invalid_generator_response_never_uses_direct_connection(self):
        for raw in [b'not-an-ip:80', b'8.8.8.8:0', b'x'*4097]:
            with patch.object(harvest.urllib.request, 'urlopen', return_value=Response(raw)):
                with self.assertRaises(ValueError):
                    harvest.resolve_route({'generator_url':'https://provider.example/api'})

    def test_generator_config_is_read_from_plugin_each_cycle(self):
        spec = importlib.util.spec_from_file_location('egress_cron', ROOT/'collector/state-cron.py')
        cron = importlib.util.module_from_spec(spec); spec.loader.exec_module(cron)
        with patch.object(settings,'PROXY_SOURCE','plugin'):
            route = cron.collection_routes({'proxy_url':'socks5://u:p@host:1080','harvest_proxy_api':'https://provider.example/get?secret=x'})[0]
        self.assertIn('generator_url', route)
        self.assertNotIn('secret', route['name'])

    def test_monitor_exposes_only_safe_egress_fields(self):
        spec = importlib.util.spec_from_file_location('egress_monitor', ROOT/'monitor/server.py')
        monitor = importlib.util.module_from_spec(spec); spec.loader.exec_module(monitor)
        with tempfile.TemporaryDirectory() as directory, patch.object(monitor,'ROOT',Path(directory)):
            (Path(directory)/'cron-status.json').write_text(json.dumps({'before':[{
                'account_id':7,'model':settings.MODEL,'egress':{'attempts':5,'requests':7,'ip_changes':2,'last_confirmed_ip':'8.8.8.8','secret':'PRIVATE'},
                'last_probe':{'egress_ip':'8.8.8.8','egress_status':'confirmed','ip_changes':2,'token':'PRIVATE'}}]}))
            (Path(directory)/'harvest-events.jsonl').write_text(json.dumps({'event':'attempt_finished','egress_ip':'8.8.8.8','egress_status':'confirmed','attempt_no':5,'ip_changes':2,'proxy_url':'PRIVATE'})+'\n')
            data = monitor.snapshot()
        self.assertEqual(data['accounts'][0]['models'][settings.MODEL]['egress']['attempts'],5)
        self.assertEqual(data['events'][0]['ip_changes'],2)
        self.assertNotIn('PRIVATE',json.dumps(data))
