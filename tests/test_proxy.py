from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dms_ai_usage.collector import collect, collect_cached, fetch_proxy_accounts, iso_now, load_config, source_fingerprint, UsageError
from dms_ai_usage.proxy import collect_proxy, discovered_accounts, NoRedirect, select_usage, ProxyError


class ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.key_path = Path(self.directory.name) / "management.key"
        self.key_path.write_text("private-management-secret\n")
        self.options = {
            "management_key_file": str(self.key_path), "timeout_seconds": 8,
            "accounts": [
                {"provider": "claude", "label": "Personal", "proxy_auth_file": "claude.json"},
                {"provider": "codex", "label": "Work", "proxy_auth_file": "codex.json"},
            ],
        }
        self.files = {"files": [
            {"name": "claude.json", "provider": "claude", "auth_index": "claude-index", "email": "account@example.test"},
            {"name": "codex.json", "provider": "codex", "auth_index": "codex-index",
             "id_token": {"chatgpt_account_id": "private-account-id"}},
        ]}

    def test_real_worker_over_http_contract_and_ssh_only_returns_quota(self) -> None:
        """Execute the exact SSH script locally against a stub management service."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread

        calls = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                calls.append((self.path, self.headers.get("Authorization"), None))
                self.reply(owner.files)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, self.headers.get("Authorization"), body))
                if body["auth_index"] == "claude-index":
                    data = {"five_hour": {"utilization": 5}, "seven_day": {"utilization": 70},
                            "limits": [{"percent": 99}], "email": "private-email"}
                else:
                    data = {"rate_limit": {
                        "primary_window": {"used_percent": 66, "limit_window_seconds": 604800, "reset_at": 1791047209},
                        "secondary_window": {"used_percent": 4, "limit_window_seconds": 18000},
                    }, "credits": {"balance": "private-balance"}, "email": "private-email"}
                self.reply({"status_code": 200, "body": json.dumps(data)})

            def reply(self, value: object) -> None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = {
            "cliproxyapi": {"ssh_host": "test-server", "management_key_file": str(self.key_path),
                            "base_url": f"http://127.0.0.1:{server.server_port}"},
        }
        real_run = subprocess.run
        ssh_output = []

        def local_ssh(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            self.assertEqual(command[-3:], ["--", "test-server", "python3 -"])
            self.assertIn("BatchMode=yes", command)
            self.assertNotIn("private-management-secret", kwargs["input"])
            result = real_run([sys.executable, "-"], **kwargs)
            ssh_output.append(result.stdout)
            return result

        with patch("dms_ai_usage.collector.subprocess.run", side_effect=local_ssh), \
                patch("dms_ai_usage.collector.fetch_claude") as local_claude, \
                patch("dms_ai_usage.collector.fetch_codex") as local_codex:
            payload = collect(config, Path(self.directory.name) / "usage.json")
        local_claude.assert_not_called()
        local_codex.assert_not_called()
        self.assertEqual([a["status"] for a in payload["accounts"]], ["ok", "ok"])
        self.assertEqual([w["label"] for w in payload["accounts"][0]["windows"]], ["5h", "7d"])
        self.assertEqual(payload["accounts"][1]["windows"][0]["remaining_percent"], 34)
        self.assertEqual(len(payload["accounts"][1]["windows"]), 1)
        for serialized in (json.dumps(payload), *ssh_output):
            self.assertNotIn("private-", serialized)
            self.assertNotIn("auth_index", serialized)
        self.assertEqual(payload["accounts"][0]["email"], "account@example.test")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(auth == "Bearer private-management-secret" for _, auth, _ in calls))
        provider_calls = {body["auth_index"]: body for _, _, body in calls if body is not None}
        self.assertEqual(provider_calls["claude-index"]["header"]["Authorization"], "Bearer $TOKEN$")
        self.assertEqual(provider_calls["codex-index"]["header"]["Chatgpt-Account-Id"], "private-account-id")

    def test_upstream_401_inside_successful_management_response(self) -> None:
        with patch("dms_ai_usage.proxy.request_json", side_effect=[self.files,
                   {"status_code": 401, "body": "private-error"},
                   {"status_code": 429, "body": "private-error"}]):
            payload = collect_proxy(self.options)
        self.assertEqual([a["error"] for a in payload["accounts"]], ["auth_expired", "rate_limited"])
        self.assertNotIn("private-error", json.dumps(payload))

    def test_exact_account_and_provider_match_required(self) -> None:
        self.files["files"][0]["provider"] = "codex"
        self.files["files"][1]["disabled"] = True
        with patch("dms_ai_usage.proxy.request_json", return_value=self.files) as request:
            payload = collect_proxy(self.options)
        self.assertEqual([a["error"] for a in payload["accounts"]], ["not_connected", "not_connected"])
        request.assert_called_once()

    def test_plain_http_remote_host_is_rejected_before_sending_key(self) -> None:
        with patch("dms_ai_usage.proxy.request_json") as request:
            payload = collect_proxy(dict(self.options, base_url="http://example.com:8317"))
        self.assertEqual(payload, {"error": "invalid_config"})
        request.assert_not_called()
        self.assertIsNone(NoRedirect().redirect_request())

    def test_missing_management_key_is_sanitized(self) -> None:
        self.key_path.unlink()
        self.assertEqual(collect_proxy(self.options), {"error": "proxy_auth_failed"})

    def test_missing_weekly_is_not_replaced_by_five_hour(self) -> None:
        with self.assertRaisesRegex(ProxyError, "usage_unavailable"):
            select_usage("codex", {"rate_limit": {
                "primary_window": {"used_percent": 5, "limit_window_seconds": 18000}}})

    def test_invalid_percentage_is_rejected(self) -> None:
        for value in (True, "secret", float("nan"), -1):
            with self.subTest(value=value), self.assertRaisesRegex(ProxyError, "bad_response"):
                select_usage("claude", {"five_hour": {"utilization": value}})

    def test_ssh_failure_retains_last_reading_with_stale_status(self) -> None:
        config = {"cliproxyapi": {"ssh_host": "server", "management_key_file": "~/key"},
                  "claude": [{"label": "Personal", "proxy_auth_file": "claude.json"}]}
        cache_path = Path(self.directory.name) / "usage.json"
        old = {"schema": 1, "source_fingerprint": source_fingerprint(config), "accounts": [{"provider": "claude", "label": "Personal", "status": "ok",
            "updated_at": "2026-09-29T00:00:00Z", "windows": [
                {"id": "seven_day", "label": "7d", "remaining_percent": 30},
                {"id": "model:fable", "label": "Fable", "remaining_percent": 99}]}]}
        cache_path.write_text(json.dumps(old))
        with patch("dms_ai_usage.collector.subprocess.run", return_value=subprocess.CompletedProcess([], 255, "")):
            payload = collect(config, cache_path)
        account = payload["accounts"][0]
        self.assertEqual((account["status"], account["error"]), ("stale", "proxy_unavailable"))
        self.assertEqual(account["updated_at"], "2026-09-29T00:00:00Z")
        self.assertEqual([w["label"] for w in account["windows"]], ["7d"])

    def test_fresh_cache_also_drops_old_quotas_without_network(self) -> None:
        path = Path(self.directory.name) / "usage.json"
        path.write_text(json.dumps({"schema": 1, "generated_at": iso_now(), "accounts": [
            {"provider": "claude", "label": "C", "windows": [
                {"id": "model:fable", "label": "Fable"}, {"id": "seven_day", "label": "7d"}]},
            {"provider": "codex", "label": "X", "windows": [{"label": "5h"}, {"label": "7d"}]},
        ]}))
        with patch("dms_ai_usage.collector.fetch_proxy_accounts") as fetch:
            payload = collect_cached({}, path)
        fetch.assert_not_called()
        self.assertEqual([[w["label"] for w in a["windows"]] for a in payload["accounts"]], [["7d"], ["7d"]])

    def test_ssh_timeout_is_reported_without_stderr(self) -> None:
        config = {"cliproxyapi": {"ssh_host": "server", "management_key_file": "~/key"},
                  "claude": [{"label": "Personal", "proxy_auth_file": "claude.json"}]}
        with patch("dms_ai_usage.collector.subprocess.run", side_effect=subprocess.TimeoutExpired("ssh", 8)):
            result = fetch_proxy_accounts(config, 8)
        self.assertEqual(result[("claude", "Personal")]["error"], "timeout")

    def test_discovery_includes_unlisted_and_disabled_accounts_without_private_metadata(self) -> None:
        extra = {"name": "private-email@example.test.json", "provider": "claude",
                 "auth_index": "private-index", "disabled": True}
        files = {"files": self.files["files"] + [extra,
            {"name": "other.json", "provider": "unsupported", "auth_index": "other"}]}
        def request(_base, _key, path, _timeout, body=None):
            if path == "auth-files":
                return files
            self.assertNotEqual(body["auth_index"], "private-index")
            data = ({"seven_day": {"utilization": 10}} if body["auth_index"] == "claude-index"
                    else {"rate_limit": {"primary_window": {"used_percent": 20, "limit_window_seconds": 604800}}})
            return {"status_code": 200, "body": json.dumps(data)}
        with patch("dms_ai_usage.proxy.request_json", side_effect=request):
            payload = collect_proxy(dict(self.options, discover_accounts=True, accounts=[]))
        self.assertEqual(len(payload["accounts"]), 3)
        self.assertEqual(sum(a.get("error") == "not_connected" for a in payload["accounts"]), 1)
        self.assertTrue(all(len(a["account_id"]) == 24 for a in payload["accounts"]))
        self.assertNotIn("private-", json.dumps(payload))
        self.assertNotIn(".json", json.dumps(payload))

    def test_discovery_aliases_do_not_filter_accounts_and_ids_survive_reordering(self) -> None:
        before = discovered_accounts(self.files["files"], self.options["accounts"][:1])
        self.assertEqual([a["label"] for a in before], ["Personal", "Account 1"])
        after = discovered_accounts(list(reversed(self.files["files"])), [])
        self.assertEqual({a["account_id"] for a in before}, {a["account_id"] for a in after})

    def test_discovered_cache_survives_outage_and_does_not_mix_reused_labels(self) -> None:
        config = {"cliproxyapi": {"ssh_host": "server", "management_key_file": "~/key"}}
        path = Path(self.directory.name) / "usage.json"
        old = {"schema": 1, "source_fingerprint": source_fingerprint(config), "accounts": [{"provider": "claude", "label": "Account 1", "account_id": "a" * 24,
            "status": "ok", "windows": [{"id": "seven_day", "label": "7d", "remaining_percent": 30}]}]}
        path.write_text(json.dumps(old))
        with patch("dms_ai_usage.collector.subprocess.run", return_value=subprocess.CompletedProcess([], 255, "")):
            stale = collect(config, path)["accounts"][0]
        self.assertEqual((stale["status"], stale["account_id"]), ("stale", "a" * 24))
        remote = {"accounts": [{"provider": "claude", "label": "Account 1", "account_id": "b" * 24,
                                "error": "auth_expired"}]}
        with patch("dms_ai_usage.collector.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(remote))):
            fresh = collect(config, path)["accounts"][0]
        self.assertEqual(fresh["status"], "error")
        self.assertEqual(fresh["windows"], [])

    def test_switching_proxy_invalidates_fresh_cache_and_stale_readings(self) -> None:
        old_config = {"cliproxyapi": {"ssh_host": "old-server", "management_key_file": "~/key"}}
        new_config = {"cliproxyapi": {"ssh_host": "new-server", "management_key_file": "~/key"}}
        path = Path(self.directory.name) / "usage.json"
        old = {"schema": 1, "generated_at": iso_now(), "source_fingerprint": source_fingerprint(old_config),
               "accounts": [{"provider": "claude", "label": "Account 1", "account_id": "a" * 24,
               "status": "ok", "windows": [{"id": "seven_day", "label": "7d", "remaining_percent": 30}]}]}
        path.write_text(json.dumps(old))
        remote = {"accounts": [{"provider": "claude", "label": "Account 1", "account_id": "a" * 24,
                                "error": "auth_expired"}]}
        with patch("dms_ai_usage.collector.subprocess.run", return_value=subprocess.CompletedProcess([], 0, json.dumps(remote))) as request:
            result = collect_cached(new_config, path)
        request.assert_called_once()
        self.assertEqual(result["accounts"][0]["status"], "error")
        self.assertEqual(result["accounts"][0]["windows"], [])
        self.assertEqual(result["source_fingerprint"], source_fingerprint(new_config))
        self.assertNotIn("new-server", path.read_text())

    def test_widget_config_requires_proxy_accounts_and_accepts_examples(self) -> None:
        path = Path(self.directory.name) / "config.json"
        connection = {"ssh_host": "my-server", "management_key_file": "~/management.key"}
        for config in (
            {"claude": [{"label": "Local", "config_dir": "~/profile"}]},
            {"cliproxyapi": connection, "codex": [{"label": "Local", "home": "~/profile"}]},
            {"cliproxyapi": connection, "claude": [{"label": "Mixed", "proxy_auth_file": "a.json", "config_dir": "~/profile"}]},
        ):
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(UsageError, "invalid_config"):
                load_config(path)
        root = Path(__file__).resolve().parents[1]
        for name in ("config.example.json", "config.proxy.example.json"):
            config = load_config(root / name)
            self.assertEqual(config["cliproxyapi"]["ssh_host"], "my-server")
            self.assertTrue(all("proxy_auth_file" in a for provider in ("claude", "codex") for a in config.get(provider, [])))

    def test_proxy_account_requires_valid_connection_config(self) -> None:
        path = Path(self.directory.name) / "config.json"
        for proxy in (None, {"ssh_host": "-oProxyCommand=bad", "management_key_file": "~/key"}):
            config = {"claude": [{"proxy_auth_file": "claude.json"}]}
            if proxy is not None:
                config["cliproxyapi"] = proxy
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(UsageError, "invalid_config"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
