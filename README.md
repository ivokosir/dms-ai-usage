# DMS AI Usage

A small, local-first DankMaterialShell widget for multiple Claude and Codex subscriptions.

The bar shows remaining capacity at a glance. Clicking it opens every available quota window, including Claude's model-scoped Fable limit.

## Why this stays small

- No account swapping or routing.
- No daemon and no inference requests.
- No third-party Python packages.
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
dms ipc call plugins reload dmsAiUsage
```

## Configure accounts

Only labels and profile directories go in the configuration file:

```json
{
  "refresh_seconds": 120,
  "timeout_seconds": 8,
  "claude": [
    {"label": "Claude 1", "config_dir": "~/.claude"},
    {"label": "Claude 2", "config_dir": "~/.claude-2"}
  ],
  "codex": [
    {"label": "Codex 1", "home": "~/.codex"},
    {"label": "Codex 2", "home": "~/.codex-2"}
  ]
}
```

Each extra profile needs a one-time login:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-2" claude auth login
CODEX_HOME="$HOME/.codex-2" codex login
```

This widget only displays usage. It never switches accounts or sends prompts.

Run a manual check:

```bash
dms-ai-usage --pretty
```

## Authentication behavior

Claude usage is fetched from Anthropic's OAuth usage endpoint using the access token already owned by Claude Code. This tool never reads the refresh token, refreshes credentials, or writes to a Claude profile. If the access token is expired, the last good result is marked stale until Claude Code refreshes it during normal work.

Codex usage is fetched through the official local [`codex app-server`](https://developers.openai.com/codex/app-server) method `account/rateLimits/read`. Codex owns its credential lifecycle.

Anthropic's OAuth usage endpoint is not a documented public API and may change. Failures retain the last sanitized result and make staleness visible.

## Privacy

Credentials, emails, absolute profile paths, and raw provider responses are never written to the cache or printed. The cache is created with mode `0600` under `~/.cache/dms-ai-usage/usage.json`.

## Development

```bash
python -m unittest discover -s tests -v
```

MIT licensed.
