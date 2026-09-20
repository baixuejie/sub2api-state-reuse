import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
spec = importlib.util.spec_from_file_location("state_cron_v1", ROOT / "collector/state-cron.py")
cron = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cron
spec.loader.exec_module(cron)


class V1CollectorTests(unittest.TestCase):
    def test_empty_or_invalid_scope_never_reads_accounts(self):
        with patch.object(cron, "sql") as query:
            for ids in ([], [True], [0], [-1], ["1); DROP TABLE accounts"]):
                with self.assertRaises(RuntimeError):
                    cron.accounts_from_plugin({"accounts": ids})
            query.assert_not_called()

    def test_only_configured_oauth_ids_are_read(self):
        with patch.object(cron, "sql", return_value=[]) as query:
            cron.accounts_from_plugin({"accounts": [3, 1, 3]})
        sql = query.call_args.args[0]
        self.assertIn("a.id IN (1,3)", sql)
        self.assertIn("a.platform='openai' AND a.type='oauth'", sql)

    def test_group_updates_disabled(self):
        with patch.object(cron.settings, "AUTO_GROUP", False), patch.object(cron, "sql") as query, patch.object(cron, "call") as api:
            cron.sync_groups([{"id": 1}], [], "test")
        query.assert_not_called()
        api.assert_not_called()

    def test_configured_proxy_is_only_route_and_label_has_no_credentials(self):
        with patch.object(cron.settings, "PROXY_SOURCE", "plugin"), patch.object(cron.local_ip_harvest, "routes_for") as others:
            routes = cron.collection_routes({"proxy_url": "socks5://user:secret@example.invalid:1000"})
        others.assert_not_called()
        self.assertEqual(len(routes), 1)
        self.assertTrue(routes[0]["url"].startswith("socks5h://"))
        self.assertNotIn("secret", routes[0]["name"])

    def test_machine_key_uses_supported_header(self):
        import io
        with patch.object(cron.urllib.request, "urlopen", return_value=io.BytesIO(b'{"code":0}')) as fetch:
            cron.call("/admin/plugins", token={"api_key": "synthetic-key"})
        request = fetch.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "synthetic-key")
        self.assertIsNone(request.get_header("Authorization"))

    def test_disabled_plugin_never_reads_credentials_or_collects(self):
        with patch.object(cron, "login", return_value={"api_key": "synthetic"}), patch.object(cron, "call", return_value={"data": {"plugin_key": "local.flownode.state-reuse", "state": "disabled"}}), patch.object(cron, "sql") as query, patch.object(cron.local_ip_harvest, "collect") as collect:
            cron.run_locked()
        query.assert_not_called()
        collect.assert_not_called()
