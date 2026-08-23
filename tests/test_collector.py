from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dms_ai_usage.collector import (
    NoRedirectHandler,
    UsageError,
    account_result,
    bar_text,
    load_config,
    parse_claude_usage,
    parse_codex_rate_limits,
    percentage,
    stale_or_error,
    write_cache,
)


class ClaudeParserTests(unittest.TestCase):
    def test_fable_is_separate_and_first(self) -> None:
        windows = parse_claude_usage(
            {
                "five_hour": {"utilization": 20, "resets_at": "2026-08-24T01:00:00Z"},
                "seven_day": {"utilization": 40, "resets_at": "2026-08-29T01:00:00Z"},
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": 55,
                        "resets_at": "2026-08-28T01:00:00Z",
                        "scope": {"model": {"display_name": "Fable"}},
                    }
                ],
            }
        )
        self.assertEqual([item["label"] for item in windows], ["Fable", "5h", "7d"])
        self.assertEqual(windows[0]["remaining_percent"], 45)


class CodexParserTests(unittest.TestCase):
    def test_primary_and_secondary(self) -> None:
        windows = parse_codex_rate_limits(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1},
                    "secondary": {"usedPercent": 60, "windowDurationMins": 10080, "resetsAt": 2},
                }
            }
        )
        self.assertEqual([item["label"] for item in windows], ["5h", "7d"])
        self.assertEqual(windows[0]["remaining_percent"], 75)

    def test_windows_are_ordered_five_hour_first(self) -> None:
        windows = parse_codex_rate_limits(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 10, "windowDurationMins": 10080, "resetsAt": 1},
                    "secondary": {"usedPercent": 20, "windowDurationMins": 300, "resetsAt": 2},
                }
            }
        )
        self.assertEqual([item["label"] for item in windows], ["5h", "7d"])

    def test_spark_buckets_are_dropped(self) -> None:
        windows = parse_codex_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1},
                        "secondary": {"usedPercent": 60, "windowDurationMins": 10080, "resetsAt": 2},
                    },
                    "codex_spark": {
                        "limitName": "GPT-5.3-Codex-Spark",
                        "primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 3},
                        "secondary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 4},
                    },
                }
            }
        )
        self.assertEqual([item["label"] for item in windows], ["5h", "7d"])
        for window in windows:
            self.assertNotIn("spark", f"{window['id']} {window['label']}".lower())

    def test_spark_bucket_named_only_in_limit_name_is_dropped(self) -> None:
        windows = parse_codex_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "secondary_pool": {
                        "limitName": "Codex-Spark",
                        "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 1},
                    }
                }
            }
        )
        self.assertEqual(windows, [])


class StaleCacheTests(unittest.TestCase):
    def test_stale_codex_windows_drop_spark(self) -> None:
        fresh = account_result("codex", "Codex 1", "error", error="network_error")
        previous = account_result(
            "codex",
            "Codex 1",
            "ok",
            windows=[
                {"id": "codex_spark:primary", "label": "GPT-5.3-Codex-Spark 5h", "remaining_percent": 100},
                {"id": "codex:secondary", "label": "7d", "remaining_percent": 40},
                {"id": "codex:primary", "label": "5h", "remaining_percent": 80},
            ],
            updated_at="2026-08-22T00:00:00Z",
        )
        result = stale_or_error(fresh, previous)
        self.assertEqual(result["status"], "stale")
        self.assertEqual([item["label"] for item in result["windows"]], ["5h", "7d"])

    def test_stale_with_only_spark_windows_becomes_error(self) -> None:
        fresh = account_result("codex", "Codex 1", "error", error="network_error")
        previous = account_result(
            "codex",
            "Codex 1",
            "ok",
            windows=[{"id": "codex_spark:primary", "label": "GPT-5.3-Codex-Spark 5h", "remaining_percent": 100}],
        )
        result = stale_or_error(fresh, previous)
        self.assertIs(result, fresh)

    def test_stale_claude_windows_are_reordered(self) -> None:
        fresh = account_result("claude", "Claude 1", "error", error="network_error")
        previous = account_result(
            "claude",
            "Claude 1",
            "ok",
            windows=[
                {"id": "five_hour", "label": "5h", "remaining_percent": 80},
                {"id": "seven_day", "label": "7d", "remaining_percent": 60},
                {"id": "model:fable", "label": "Fable", "remaining_percent": 45},
            ],
        )
        result = stale_or_error(fresh, previous)
        self.assertEqual(result["status"], "stale")
        self.assertEqual([item["label"] for item in result["windows"]], ["Fable", "5h", "7d"])


class OutputPrivacyTests(unittest.TestCase):
    def test_claude_redirects_are_refused(self) -> None:
        handler = NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://example.test"))

    def test_provider_overage_is_clamped(self) -> None:
        self.assertEqual(percentage(103.5), 100)

    def test_duplicate_labels_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"claude":[{"label":"Same"},{"label":"Same"}]}')
            with self.assertRaisesRegex(UsageError, "invalid_config"):
                load_config(path)

    def test_successful_refresh_does_not_become_stale(self) -> None:
        fresh = account_result("claude", "Claude 1", "ok", windows=[{"label": "5h"}])
        previous = account_result("claude", "Claude 1", "ok", windows=[{"label": "7d"}])
        self.assertIs(stale_or_error(fresh, previous), fresh)

    def test_cache_contains_only_sanitized_input(self) -> None:
        payload = {
            "schema": 1,
            "bar_text": "C1 80/F45",
            "accounts": [
                account_result(
                    "claude",
                    "Claude 1",
                    "ok",
                    windows=[{"id": "five_hour", "label": "5h", "used_percent": 20, "remaining_percent": 80}],
                )
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "usage.json"
            write_cache(path, payload)
            encoded = path.read_text()
            self.assertNotIn("accessToken", encoded)
            self.assertNotIn("refreshToken", encoded)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_compact_bar_uses_remaining_capacity(self) -> None:
        accounts = [
            account_result(
                "claude",
                "Claude 1",
                "ok",
                windows=[
                    {"id": "five_hour", "label": "5h", "remaining_percent": 80},
                    {"id": "model:fable", "label": "Fable", "remaining_percent": 45},
                ],
            ),
            account_result(
                "codex",
                "Codex 1",
                "ok",
                windows=[{"id": "codex:primary", "label": "5h", "remaining_percent": 75}],
            ),
        ]
        self.assertEqual(bar_text(accounts), "C1 80/F45 · X1 75")

    def test_compact_bar_unaffected_by_window_order(self) -> None:
        accounts = [
            account_result(
                "claude",
                "Claude 1",
                "ok",
                windows=[
                    {"id": "model:fable", "label": "Fable", "remaining_percent": 45},
                    {"id": "five_hour", "label": "5h", "remaining_percent": 80},
                ],
            )
        ]
        self.assertEqual(bar_text(accounts), "C1 80/F45")


if __name__ == "__main__":
    unittest.main()
