# DMS AI Usage

A small, local-first DankMaterialShell widget for multiple Claude and Codex subscriptions.

The bar shows each provider icon and account initial above a small weekly capacity meter, without percentage text. Clicking it shows Claude's five-hour and weekly limits, and Codex's weekly limit. Model-specific limits (including Fable), Codex five-hour, Spark, and GPT reserve quotas are hidden. The popup sizes itself to its account rows, has no header, and closes when clicked inside or outside.

## Why this stays small

- No account swapping or routing.
- No daemon and no inference requests.
- No third-party Python packages.
- Optional CLIProxyAPI connection over SSH keeps login credentials on the server.
- Claude profiles remain normal `CLAUDE_CONFIG_DIR` directories.
- Codex profiles remain normal `CODEX_HOME` directories.
- The cache contains only labels, percentages, and reset times.

## Install

Requirements: Python 3.11+, Claude Code, Codex CLI, and DankMaterialShell 1.5+.

```bash
git clone https://github.com/ivokosir/dms-ai-usage.git
cd dms-ai-usage
./install.sh
```

Edit `~/.config/dms-ai-usage/config.json`, then enable **AI Usage** in DMS Settings → Plugins and add `dmsAiUsage` to a DankBar.

Reload without restarting the shell:

```bash
dms ipc call plugin-scan scan
dms ipc call plugins enable dmsAiUsage
dms ipc call plugin-scan reload dmsAiUsage
```

## Configure accounts

For local CLI accounts, put labels and profile directories in the configuration file:

```json
{
  "refresh_seconds": 600,
  "timeout_seconds": 8,
  "claude": [
    {"label": "Personal", "config_dir": "~/ai-subscriptions/claude-personal"},
    {"label": "Work", "config_dir": "~/ai-subscriptions/claude-work"}
  ],
  "codex": [
    {"label": "Personal", "home": "~/ai-subscriptions/codex-personal"},
    {"label": "Work", "home": "~/ai-subscriptions/codex-work"}
  ]
}
```

Each extra profile needs a one-time login:

```bash
mkdir -p "$HOME/ai-subscriptions/claude-work" "$HOME/ai-subscriptions/codex-work"
CLAUDE_CONFIG_DIR="$HOME/ai-subscriptions/claude-work" claude auth login
CODEX_HOME="$HOME/ai-subscriptions/codex-work" codex login
```

This widget only displays usage. It never switches accounts or sends prompts.

Run a manual check:

```bash
dms-ai-usage --pretty
```

## Use accounts already connected to CLIProxyAPI

If CLIProxyAPI already keeps your subscriptions connected on a server, the widget can use those accounts instead of maintaining another set of local logins. This uses the management API's `GET /v0/management/auth-files` and `POST /v0/management/api-call`, the same quota endpoints used by its management panel. The proxy's `/usage` statistics count proxy requests; they are not subscription capacity.

Requirements: SSH access without interactive prompts, Python 3.11+ on the server, and the existing CLIProxyAPI **management key** in a private text file on that server. A client API key will not work. Store the key with file mode `0600`; do not put it in the widget configuration.

For example, start with [config.proxy.example.json](config.proxy.example.json):

```json
{
  "refresh_seconds": 600,
  "timeout_seconds": 8,
  "cliproxyapi": {
    "ssh_host": "my-server",
    "base_url": "http://127.0.0.1:8317",
    "management_key_file": "~/.config/cliproxyapi/management.key"
  },
  "claude": [
    {"label": "Personal", "proxy_auth_file": "claude-personal.json"}
  ],
  "codex": [
    {"label": "Personal", "proxy_auth_file": "codex-personal.json"}
  ]
}
```

Use your SSH alias and the exact auth-file names shown by the proxy's management panel. The key-file path and base URL are resolved on the server. HTTP is allowed only for loopback addresses; use HTTPS for any other host. The widget sends a small Python worker over one SSH connection per refresh. No helper installation or extra server is needed. Only the requested percentages and reset times come back; keys and provider account details stay on the server.

An account with `proxy_auth_file` uses the proxy exclusively, so it never refreshes or overwrites local CLI credentials. Local and proxy accounts can coexist in the same configuration. The proxy owns token renewal; if an account becomes disconnected there, reconnect it in the proxy. Connection failures retain the last reading with a `stale` status, and the next scheduled poll tries again.

## Authentication behavior

For local profiles, Claude usage is fetched from Anthropic's OAuth usage endpoint using the credentials already owned by Claude Code. Access tokens expire after several hours, so the widget refreshes one shortly before expiry and retries once after an authentication failure. Anthropic rotates refresh tokens; both new tokens are therefore saved together to the same profile with an atomic mode-`0600` write. A per-profile lock prevents two widget processes from refreshing the same token at once, and a concurrent update from Claude Code is never overwritten.

The default 10-minute polling interval keeps the bar current without hammering the experimental endpoint. Provider rate limits or network failures retain the last sanitized result as stale data.

On multi-monitor setups, every bar reads the same locked cache. Simultaneous bars trigger only one provider refresh, while each screen receives the result. The widget lets the collector enforce its own request timeouts instead of terminating it at the shell's default ten-second limit.

For local profiles, Codex usage is fetched through the official local [`codex app-server`](https://developers.openai.com/codex/app-server) method `account/rateLimits/read`. Codex owns its credential lifecycle.

The provider quota endpoints used by CLIProxyAPI are not documented public APIs and may change. See CLIProxyAPI's [API-call handler](https://github.com/router-for-me/CLIProxyAPI/blob/main/internal/api/handlers/management/api_tools.go) and the management panel's [quota endpoints](https://github.com/router-for-me/Cli-Proxy-API-Management-Center/blob/main/src/utils/quota/constants.ts).

## Privacy

Credentials, emails, absolute profile paths, and raw provider responses are never written to the cache or printed. Refresh tokens are sent only to Anthropic's token endpoint and never leave their source profile on disk. The cache is created with mode `0600` under `~/.cache/dms-ai-usage/usage.json`.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Capture the real widget after an edit:

```bash
./scripts/capture-ui.sh /tmp/dms-ai-usage-preview
```

This reloads DMS and saves cropped `bar.ui.png` and `popout.ui.png` images without touching the clipboard or storing a full desktop screenshot. It currently requires niri, jq, and ImageMagick.

MIT licensed.
