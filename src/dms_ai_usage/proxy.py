"""Small stdlib-only worker sent over SSH; credentials stay on the proxy host."""
from __future__ import annotations

import http.client
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MAX_BYTES = 1_048_576


class ProxyError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def check_status(status: int, *, management: bool = False) -> None:
    if status in (401, 403):
        raise ProxyError("proxy_auth_failed" if management else "auth_expired")
    if status == 429:
        raise ProxyError("rate_limited")
    if not 200 <= status < 300:
        raise ProxyError("provider_error")


def request_json(base_url: str, key: str, path: str, timeout: float,
                 body: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v0/management/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        check_status(exc.code, management=True)
        raise ProxyError("provider_error") from exc
    except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
        raise ProxyError("network_error") from exc
    if len(raw) > MAX_BYTES:
        raise ProxyError("bad_response")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ProxyError("bad_response") from exc
    if not isinstance(data, dict):
        raise ProxyError("bad_response")
    return data


def quota_window(raw: Any, used_key: str, reset_key: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get(used_key) is None:
        return None
    used = raw[used_key]
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
        raise ProxyError("bad_response")
    result = {"used": used}
    reset = raw.get(reset_key)
    if isinstance(reset, str) and len(reset) <= 64:
        result["reset"] = reset
    elif isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset):
        result["reset"] = reset
    return result


def select_usage(provider: str, data: dict[str, Any]) -> dict[str, Any]:
    """Only quota numbers and reset times may cross back over SSH."""
    selected: dict[str, Any] = {}
    if provider == "claude":
        for name in ("five_hour", "seven_day"):
            window = quota_window(data.get(name), "utilization", "resets_at")
            if window is not None:
                selected[name] = {"utilization": window["used"], "resets_at": window.get("reset")}
    elif provider == "codex":
        rate_limit = data.get("rate_limit")
        if not isinstance(rate_limit, dict):
            raise ProxyError("usage_unavailable")
        for slot in ("primary", "secondary"):
            raw = rate_limit.get(slot + "_window")
            if not isinstance(raw, dict) or raw.get("limit_window_seconds") != 604800:
                continue
            window = quota_window(raw, "used_percent", "reset_at")
            if window is not None:
                selected.setdefault("rateLimits", {})[slot] = {
                    "usedPercent": window["used"], "windowDurationMins": 10080,
                    "resetsAt": window.get("reset"),
                }
    if not selected:
        raise ProxyError("usage_unavailable")
    return selected


def collect_proxy(options: dict[str, Any]) -> dict[str, Any]:
    try:
        base_url = options.get("base_url", "http://127.0.0.1:8317")
        url = urllib.parse.urlsplit(base_url)
        if (url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/") or not url.hostname
                or not (url.scheme == "https" or (url.scheme == "http"
                    and url.hostname in ("localhost", "127.0.0.1", "::1")))):
            raise ProxyError("invalid_config")
        try:
            key_path = Path(options["management_key_file"]).expanduser()
            if key_path.stat().st_size > 4096:
                raise ProxyError("proxy_auth_failed")
            key = key_path.read_text().strip()
        except (OSError, UnicodeError) as exc:
            raise ProxyError("proxy_auth_failed") from exc
        if not key or "\n" in key or "\r" in key:
            raise ProxyError("proxy_auth_failed")
        timeout = options["timeout_seconds"]
        files = request_json(base_url, key, "auth-files", timeout).get("files")
        if not isinstance(files, list):
            raise ProxyError("bad_response")
        accounts = []
        for account in options["accounts"]:
            provider = account["provider"]
            result = {"provider": provider, "label": account["label"]}
            try:
                matches = [item for item in files if isinstance(item, dict)
                           and item.get("name") == account["proxy_auth_file"]
                           and item.get("provider") == provider]
                if len(matches) != 1 or matches[0].get("disabled"):
                    raise ProxyError("not_connected")
                auth = matches[0]
                index = auth.get("auth_index")
                if not isinstance(index, str) or not index:
                    raise ProxyError("not_connected")
                headers = {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"}
                if provider == "claude":
                    endpoint = "https://api.anthropic.com/api/oauth/usage"
                    headers["anthropic-beta"] = "oauth-2025-04-20"
                else:
                    endpoint = "https://chatgpt.com/backend-api/wham/usage"
                    headers["User-Agent"] = "codex-tui"
                    metadata = auth.get("id_token")
                    account_id = metadata.get("chatgpt_account_id") if isinstance(metadata, dict) else None
                    if isinstance(account_id, str) and account_id:
                        headers["Chatgpt-Account-Id"] = account_id
                response = request_json(base_url, key, "api-call", timeout, {
                    "auth_index": index, "method": "GET", "url": endpoint, "header": headers,
                })
                status = response.get("status_code")
                if not isinstance(status, int) or isinstance(status, bool):
                    raise ProxyError("bad_response")
                check_status(status)
                try:
                    data = json.loads(response["body"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ProxyError("bad_response") from exc
                if not isinstance(data, dict):
                    raise ProxyError("bad_response")
                result["usage"] = select_usage(provider, data)
            except ProxyError as exc:
                result["error"] = str(exc)
            accounts.append(result)
        return {"accounts": accounts}
    except ProxyError as exc:
        return {"error": str(exc)}
