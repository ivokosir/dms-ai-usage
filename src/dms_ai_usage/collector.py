from __future__ import annotations

import fcntl
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
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_SCOPE = (
    "user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
)
CLAUDE_REFRESH_SKEW_MS = 5 * 60 * 1000
USER_AGENT = "dms-ai-usage/0.2.1"
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


def is_spark_window(window: Any) -> bool:
    if not isinstance(window, dict):
        return True
    text = f"{window.get('id', '')} {window.get('label', '')}".lower()
    return "spark" in text


def strip_spark_windows(windows: Any) -> list[dict[str, Any]]:
    if not isinstance(windows, list):
        return []
    return [window for window in windows if not is_spark_window(window)]


def window_sort_key(provider: str, window: dict[str, Any]) -> int:
    label = str(window.get("label", "")).lower()
    if provider == "claude":
        # Weekly-scoped model windows (Fable) first, then 5h, then 7d.
        if label == "5h":
            return 1
        if label == "7d":
            return 2
        return 0
    if label == "5h" or label.endswith(" 5h"):
        return 0
    if label == "7d" or label.endswith(" 7d"):
        return 1
    return 2


def order_windows(provider: str, windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(windows, key=lambda window: window_sort_key(provider, window))


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
    return order_windows("claude", windows)


def write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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


def claude_oauth(credentials: dict[str, Any]) -> dict[str, Any]:
    oauth = credentials.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise UsageError("not_connected")
    return oauth


def token_is_fresh(oauth: dict[str, Any], now_ms: float, *, skew_ms: int = 0) -> bool:
    access_token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt")
    return (
        isinstance(access_token, str)
        and bool(access_token)
        and isinstance(expires_at, (int, float))
        and not isinstance(expires_at, bool)
        and expires_at > now_ms + skew_ms
    )


def refresh_claude_credentials(
    credentials_path: Path,
    timeout: float,
    *,
    opener: Callable[..., Any] = open_without_redirects,
    force: bool = False,
    rejected_access_token: str | None = None,
) -> dict[str, Any]:
    """Refresh one profile and persist Anthropic's rotated token pair safely."""
    lock_path = credentials_path.with_name(f".{credentials_path.name}.dms-ai-usage.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)

        credentials = read_json(credentials_path)
        oauth = claude_oauth(credentials)
        now_ms = time.time() * 1000
        if (
            force
            and rejected_access_token
            and oauth.get("accessToken") != rejected_access_token
            and token_is_fresh(oauth, now_ms)
        ):
            return credentials
        if not force and token_is_fresh(oauth, now_ms, skew_ms=CLAUDE_REFRESH_SKEW_MS):
            return credentials

        refresh_token = oauth.get("refreshToken")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise UsageError("auth_expired")

        scopes = oauth.get("scopes")
        if isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
            scope = " ".join(item for item in scopes if item) or CLAUDE_OAUTH_SCOPE
        elif isinstance(scopes, str) and scopes:
            scope = scopes
        else:
            scope = CLAUDE_OAUTH_SCOPE

        body = json.dumps(
            {
                "client_id": CLAUDE_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": scope,
            },
            separators=(",", ":"),
        ).encode()
        request = urllib.request.Request(
            CLAUDE_TOKEN_URL,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with opener(request, timeout=timeout) as response:
                raw = response.read(MAX_INPUT_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (400, 401, 403):
                # Claude Code may have refreshed this same profile concurrently.
                newest = read_json(credentials_path)
                newest_oauth = claude_oauth(newest)
                if newest_oauth.get("refreshToken") != refresh_token and token_is_fresh(
                    newest_oauth, time.time() * 1000
                ):
                    return newest
                raise UsageError("auth_expired") from exc
            if exc.code == 429:
                raise UsageError("rate_limited") from exc
            raise UsageError("provider_error") from exc
        except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            raise UsageError("network_error") from exc

        if len(raw) > MAX_INPUT_BYTES:
            raise UsageError("bad_response")
        try:
            token_data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UsageError("bad_response") from exc
        if not isinstance(token_data, dict):
            raise UsageError("bad_response")

        access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(expires_in, (int, float))
            or isinstance(expires_in, bool)
            or expires_in <= 0
        ):
            raise UsageError("bad_response")

        # Never overwrite a rotation completed while the network call was in flight.
        newest = read_json(credentials_path)
        newest_oauth = claude_oauth(newest)
        if newest_oauth.get("refreshToken") != refresh_token:
            if token_is_fresh(newest_oauth, time.time() * 1000):
                return newest
            raise UsageError("auth_expired")

        updated_oauth = dict(newest_oauth)
        updated_oauth["accessToken"] = access_token
        rotated_refresh = token_data.get("refresh_token")
        if isinstance(rotated_refresh, str) and rotated_refresh:
            updated_oauth["refreshToken"] = rotated_refresh
        updated_oauth["expiresAt"] = int(time.time() * 1000 + float(expires_in) * 1000)
        refresh_expires_in = token_data.get("refresh_token_expires_in")
        if (
            isinstance(refresh_expires_in, (int, float))
            and not isinstance(refresh_expires_in, bool)
            and refresh_expires_in > 0
        ):
            updated_oauth["refreshTokenExpiresAt"] = int(
                time.time() * 1000 + float(refresh_expires_in) * 1000
            )

        newest["claudeAiOauth"] = updated_oauth
        write_private_json(credentials_path, newest)
        return newest
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def fetch_claude_usage_json(
    access_token: str,
    timeout: float,
    *,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            raw = response.read(MAX_INPUT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
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
    return data


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

    credentials_path = (expand_path(config_value) / ".credentials.json").resolve()
    credentials = read_json(credentials_path)
    oauth = claude_oauth(credentials)
    access_token = oauth.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise UsageError("not_connected")
    if not token_is_fresh(oauth, time.time() * 1000, skew_ms=CLAUDE_REFRESH_SKEW_MS):
        credentials = refresh_claude_credentials(credentials_path, timeout, opener=opener)
        oauth = claude_oauth(credentials)
        access_token = oauth.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise UsageError("auth_expired")

    try:
        data = fetch_claude_usage_json(access_token, timeout, opener=opener)
    except UsageError as exc:
        if exc.code != "auth_expired":
            raise
        credentials = refresh_claude_credentials(
            credentials_path,
            timeout,
            opener=opener,
            force=True,
            rejected_access_token=access_token,
        )
        refreshed_oauth = claude_oauth(credentials)
        refreshed_token = refreshed_oauth.get("accessToken")
        if not isinstance(refreshed_token, str) or not refreshed_token:
            raise UsageError("auth_expired") from exc
        data = fetch_claude_usage_json(refreshed_token, timeout, opener=opener)
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
        # Spark buckets must never reach output or cache.
        spark_text = f"{limit_id} {bucket_name if isinstance(bucket_name, str) else ''}".lower()
        if "spark" in spark_text:
            continue
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
    return order_windows("codex", strip_spark_windows(windows))


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
    refresh = config.get("refresh_seconds", 600)
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
        provider = str(fresh["provider"])
        windows = previous.get("windows")
        windows = strip_spark_windows(windows) if provider == "codex" else (
            [window for window in windows if isinstance(window, dict)] if isinstance(windows, list) else []
        )
        if not windows:
            return fresh
        return account_result(
            provider,
            str(fresh["label"]),
            "stale",
            windows=order_windows(provider, windows),
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
    write_private_json(path, payload)


def cache_is_fresh(cache: dict[str, Any], max_age_seconds: float) -> bool:
    generated_at = cache.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at:
        return False
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    age = time.time() - parsed.timestamp()
    return -5 <= age < max_age_seconds


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
        "refresh_seconds": int(config.get("refresh_seconds", 600)),
        "bar_text": bar_text(accounts),
        "accounts": accounts,
    }
    write_cache(cache_path, payload)
    return payload


def collect_cached(config: dict[str, Any], cache_path: Path) -> dict[str, Any]:
    """Share one provider refresh across simultaneous bar instances."""
    lock_path = cache_path.with_name(f".{cache_path.name}.collect.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        cached = load_cache(cache_path)
        refresh_seconds = float(config.get("refresh_seconds", 600))
        if cache_is_fresh(cached, refresh_seconds):
            return cached
        return collect(config, cache_path)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
