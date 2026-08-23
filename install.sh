#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
bin_dir="$HOME/.local/bin"
app_config_dir="$config_root/dms-ai-usage"
dms_plugins_dir="$config_root/DankMaterialShell/plugins"

mkdir -p "$bin_dir" "$app_config_dir" "$dms_plugins_dir"

link_safely() {
  local source="$1"
  local target="$2"
  if [[ -e "$target" && ! -L "$target" ]]; then
    echo "Refusing to replace existing path: $target" >&2
    exit 1
  fi
  ln -sfn "$source" "$target"
}

link_safely "$repo_dir/bin/dms-ai-usage" "$bin_dir/dms-ai-usage"
link_safely "$repo_dir/dms-plugin" "$dms_plugins_dir/dmsAiUsage"

if [[ ! -e "$app_config_dir/config.json" ]]; then
  cp "$repo_dir/config.example.json" "$app_config_dir/config.json"
fi
chmod 600 "$app_config_dir/config.json"

echo "Installed dms-ai-usage."
echo "Edit $app_config_dir/config.json, then enable dmsAiUsage in DMS."
