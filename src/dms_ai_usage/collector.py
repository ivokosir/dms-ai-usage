from __future__ import annotations

import http.client
import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
MAX_INPUT_BYTES = 1_048_576


class UsageError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def open_without_redirects(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(NoRedirectHandler()).open(request, timeout=timeout)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def expand_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def read_json(path: Path, *, max_bytes: int = MAX_INPUT_BYTES) -> dict[str, Any]:
    try:
        if path.stat().st_size > max_bytes:
            raise UsageError("file_too_large")
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise UsageError("not_connected") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError("invalid_file") from exc
    if not isinstance(value, dict):
        raise UsageError("invalid_file")
    return value


def percentage(value: Any) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise UsageError("bad_response")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise UsageError("bad_response")
    return round(min(number, 100), 2)


def make_window(window_id: str, label: str, used: Any, resets_at: Any = None) -> dict[str, Any]:
    used_percent = percentage(used)
    result: dict[str, Any] = {
        "id": window_id,
        "label": label,
        "used_percent": used_percent,
        "remaining_percent": round(100 - used_percent, 2),
    }
    if isinstance(resets_at, str) and len(resets_at) <= 64:
        result["resets_at"] = resets_at
    elif isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        result["resets_at"] = int(resets_at)
    return result


def parse_claude_usage(data: dict[str, Any]) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        raw = data.get(key)
        if isinstance(raw, dict) and raw.get("utilization") is not None:
            windows.append(make_window(key, label, raw["utilization"], raw.get("resets_at")))

    limits = data.get("limits")
    if isinstance(limits, list):
        for index, limit in enumerate(limits):
            if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
                continue
            scope = limit.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            if not isinstance(name, str) or not name or len(name) > 40:
                continue
            if limit.get("percent") is None:
                continue
            safe_name = "".join(char.lower() if char.isalnum() else "-" for char in name).strip("-")
            windows.append(
                make_window(
                    f"model:{safe_name or index}",
                    name,
                    limit["percent"],
                    limit.get("resets_at"),
                )
            )
    return windows


def fetch_claude(
    account: dict[str, Any],
    timeout: float,
    *,
    opener: Callable[..., Any] = open_without_redirects,
) -> dict[str, Any]:
    label = safe_label(account.get("label"), "Claude")
    config_value = account.get("config_dir")
    if not isinstance(config_value, str) or not config_value:
        raise UsageError("invalid_config")

    credentials = read_json(expand_path(config_value) / ".credentials.json")
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise UsageError("not_connected")
    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise UsageError("not_connected")
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time() * 1000:
        raise UsageError("auth_expired")

    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
            "User-Agent": "dms-ai-usage/0.1.0",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_INPUT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise UsageError("auth_expired") from exc
        if exc.code == 429:
            raise UsageError("rate_limited") from exc
        raise UsageError("provider_error") from exc
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise UsageError("network_error") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise UsageError("bad_response")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError("bad_response") from exc
    if not isinstance(data, dict):
        raise UsageError("bad_response")
    windows = parse_claude_usage(data)
    if not windows:
        raise UsageError("usage_unavailable")
    return account_result("claude", label, "ok", windows=windows, updated_at=iso_now())


def duration_label(minutes: Any, fallback: str) -> str:
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool):
        return fallback
    value = int(minutes)
    if value == 300:
        return "5h"
    if value == 10_080:
        return "7d"
    if value % 1_440 == 0:
        return f"{value // 1_440}d"
    if value % 60 == 0:
        return f"{value // 60}h"
    return f"{value}m"


def parse_codex_rate_limits(result: dict[str, Any]) -> list[dict[str, Any]]:
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        single = result.get("rateLimits")
        buckets = {"codex": single} if isinstance(single, dict) else {}

    windows: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    ordered = sorted(buckets.items(), key=lambda item: (item[0] != "codex", item[0]))
    for limit_id, bucket in ordered:
        if not isinstance(limit_id, str) or not isinstance(bucket, dict):
            continue
        bucket_name = bucket.get("limitName")
        for slot, fallback in (("primary", "Primary"), ("secondary", "Secondary")):
            raw = bucket.get(slot)
            if not isinstance(raw, dict) or raw.get("usedPercent") is None:
                continue
            fingerprint = (raw.get("windowDurationMins"), raw.get("resetsAt"), raw.get("usedPercent"))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            label = duration_label(raw.get("windowDurationMins"), fallback)
            if limit_id != "codex" and isinstance(bucket_name, str) and bucket_name:
                label = f"{bucket_name} {label}"
            windows.append(
                make_window(
                    f"{limit_id}:{slot}",
                    label[:60],
                    raw["usedPercent"],
                    raw.get("resetsAt"),
                )
            )
    return windows


class CodexRpcClient:
    def __init__(self, home: Path, timeout: float):
        codex = shutil.which("codex")
        if codex is None:
            raise UsageError("cli_missing")
        env = os.environ.copy()
        env["CODEX_HOME"] = str(home)
        env.pop("OPENAI_API_KEY", None)
        try:
            self.process = subprocess.Popen(
                [codex, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            raise UsageError("cli_failed") from exc
        self.timeout = timeout
        self.buffer = b""
        self.selector = selectors.DefaultSelector()
        assert self.process.stdout is not None
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            self.process.stdin.flush()
        except OSError as exc:
            raise UsageError("protocol_error") from exc

    def receive(self, message_id: int) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(message, dict)
                    and message.get("id") == message_id
                    and "method" not in message
                ):
                    if message.get("error") is not None:
                        raise UsageError("provider_error")
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise UsageError("bad_response")
                    return result
            remaining = deadline - time.monotonic()
            try:
                ready = self.selector.select(max(0, remaining))
            except OSError as exc:
                raise UsageError("protocol_error") from exc
            if not ready:
                break
            assert self.process.stdout is not None
            try:
                chunk = os.read(self.process.stdout.fileno(), 65_536)
            except OSError as exc:
                raise UsageError("protocol_error") from exc
            if not chunk:
                break
            self.buffer += chunk
            if len(self.buffer) > MAX_INPUT_BYTES:
                raise UsageError("bad_response")
        raise UsageError("timeout")

    def close(self) -> None:
        self.selector.close()
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def fetch_codex(account: dict[str, Any], timeout: float) -> dict[str, Any]:
    label = safe_label(account.get("label"), "Codex")
    home_value = account.get("home")
    if not isinstance(home_value, str) or not home_value:
        raise UsageError("invalid_config")
    home = expand_path(home_value)
    if not (home / "auth.json").is_file():
        raise UsageError("not_connected")

    client = CodexRpcClient(home, timeout)
    try:
        client.send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "dms_ai_usage",
                        "title": "DMS AI Usage",
                        "version": "0.1.0",
                    }
                },
            }
        )
        client.receive(0)
        client.send({"method": "initialized", "params": {}})
        client.send({"method": "account/rateLimits/read", "id": 1})
        result = client.receive(1)
    finally:
        client.close()
    windows = parse_codex_rate_limits(result)
    if not windows:
        raise UsageError("usage_unavailable")
    return account_result("codex", label, "ok", windows=windows, updated_at=iso_now())


def safe_label(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = " ".join(value.split())
    return cleaned[:40] or fallback


def account_result(
    provider: str,
    label: str,
    status: str,
    *,
    windows: list[dict[str, Any]] | None = None,
    updated_at: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provider": provider,
        "label": safe_label(label, provider.title()),
        "status": status,
        "windows": windows or [],
    }
    if updated_at:
        result["updated_at"] = updated_at
    if error:
        result["error"] = error[:40]
    return result


def load_config(path: Path) -> dict[str, Any]:
    config = read_json(path)
    for key in ("claude", "codex"):
        value = config.get(key, [])
        if not isinstance(value, list) or len(value) > 20:
            raise UsageError("invalid_config")
        labels: set[str] = set()
        for account in value:
            if not isinstance(account, dict):
                raise UsageError("invalid_config")
            label = safe_label(account.get("label"), key.title())
            if label in labels:
                raise UsageError("invalid_config")
            labels.add(label)
    timeout = config.get("timeout_seconds", 8)
    refresh = config.get("refresh_seconds", 120)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise UsageError("invalid_config")
    if not isinstance(refresh, (int, float)) or not 30 <= refresh <= 3600:
        raise UsageError("invalid_config")
    return config


def load_cache(path: Path) -> dict[str, Any]:
    try:
        data = read_json(path)
    except UsageError:
        return {}
    return data if data.get("schema") == 1 else {}


def stale_or_error(fresh: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if fresh.get("status") != "error":
        return fresh
    if previous and previous.get("windows") and previous.get("provider") == fresh.get("provider"):
        return account_result(
            str(fresh["provider"]),
            str(fresh["label"]),
            "stale",
            windows=previous.get("windows") if isinstance(previous.get("windows"), list) else [],
            updated_at=previous.get("updated_at") if isinstance(previous.get("updated_at"), str) else None,
            error=str(fresh.get("error", "unavailable")),
        )
    return fresh


def window_remaining(account: dict[str, Any], wanted: str) -> int | None:
    windows = account.get("windows")
    if not isinstance(windows, list):
        return None
    for window in windows:
        if not isinstance(window, dict):
            continue
        window_id = str(window.get("id", "")).lower()
        label = str(window.get("label", "")).lower()
        if wanted == "fable" and "fable" not in window_id and "fable" not in label:
            continue
        if wanted != "fable" and window.get("label") != wanted:
            continue
        remaining = window.get("remaining_percent")
        if isinstance(remaining, (int, float)):
            return int(round(remaining))
    return None


def bar_text(accounts: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    counters = {"claude": 0, "codex": 0}
    for account in accounts:
        provider = account.get("provider")
        if provider not in counters:
            continue
        counters[provider] += 1
        prefix = ("C" if provider == "claude" else "X") + str(counters[provider])
        five = window_remaining(account, "5h")
        if provider == "claude":
            fable = window_remaining(account, "fable")
            five_text = "–" if five is None else str(five)
            fable_text = "–" if fable is None else str(fable)
            parts.append(f"{prefix} {five_text}/F{fable_text}")
        else:
            primary = five
            if primary is None:
                windows = account.get("windows")
                if isinstance(windows, list) and windows:
                    first = windows[0]
                    value = first.get("remaining_percent") if isinstance(first, dict) else None
                    primary = int(round(value)) if isinstance(value, (int, float)) else None
            parts.append(f"{prefix} {'–' if primary is None else primary}")
    return " · ".join(parts) if parts else "AI –"


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def collect(config: dict[str, Any], cache_path: Path) -> dict[str, Any]:
    timeout = float(config.get("timeout_seconds", 8))
    previous = load_cache(cache_path)
    previous_accounts = {
        (item.get("provider"), item.get("label")): item
        for item in previous.get("accounts", [])
        if isinstance(item, dict)
    }
    accounts: list[dict[str, Any]] = []
    for provider, fetcher in (("claude", fetch_claude), ("codex", fetch_codex)):
        configured = config.get(provider, [])
        for raw_account in configured:
            if not isinstance(raw_account, dict):
                raw_account = {}
            label = safe_label(raw_account.get("label"), provider.title())
            try:
                result = fetcher(raw_account, timeout)
            except UsageError as exc:
                result = account_result(provider, label, "error", error=exc.code)
            prior = previous_accounts.get((provider, label))
            accounts.append(stale_or_error(result, prior))

    payload = {
        "schema": 1,
        "generated_at": iso_now(),
        "refresh_seconds": int(config.get("refresh_seconds", 120)),
        "bar_text": bar_text(accounts),
        "accounts": accounts,
    }
    write_cache(cache_path, payload)
    return payload
