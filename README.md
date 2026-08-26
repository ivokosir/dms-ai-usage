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
dms ipc call plugin-scan reload dmsAiUsage
```

## Configure accounts

Only labels and profile directories go in the configuration file:

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

## Authentication behavior

Claude usage is fetched from Anthropic's OAuth usage endpoint using the credentials already owned by Claude Code. Access tokens expire after several hours, so the widget refreshes one shortly before expiry and retries once after an authentication failure. Anthropic rotates refresh tokens; both new tokens are therefore saved together to the same profile with an atomic mode-`0600` write. A per-profile lock prevents two widget processes from refreshing the same token at once, and a concurrent update from Claude Code is never overwritten.

The default 10-minute polling interval keeps the bar current without hammering the experimental endpoint. Provider rate limits or network failures retain the last sanitized result as stale data.

On multi-monitor setups, every bar reads the same locked cache. Simultaneous bars trigger only one provider refresh, while each screen receives the result.

Codex usage is fetched through the official local [`codex app-server`](https://developers.openai.com/codex/app-server) method `account/rateLimits/read`. Codex owns its credential lifecycle.

Anthropic's OAuth usage endpoint is not a documented public API and may change.

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
