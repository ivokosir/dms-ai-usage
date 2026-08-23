#!/usr/bin/env bash
set -euo pipefail

output_dir="${1:-/tmp/dms-ai-usage-preview}"
case "$output_dir" in
  /*) ;;
  *)
    echo "Output directory must be absolute." >&2
    exit 2
    ;;
esac

for command_name in dms jq magick niri; do
  if ! command -v "$command_name" >/dev/null; then
    echo "Missing command: $command_name" >&2
    exit 1
  fi
done

screen="${DMS_AI_USAGE_SCREEN:-DP-1}"
screen_json="$(niri msg -j outputs)"
screen_width="$(jq -r --arg name "$screen" '.[$name].logical.width // empty' <<<"$screen_json")"
screen_height="$(jq -r --arg name "$screen" '.[$name].logical.height // empty' <<<"$screen_json")"
if [[ -z "$screen_width" || -z "$screen_height" ]]; then
  echo "Output is not active: $screen" >&2
  exit 1
fi

bar_width=$((screen_width < 1200 ? screen_width : 1200))
bar_x=$((screen_width - bar_width))
popout_width=$((screen_width < 1100 ? screen_width : 1100))
popout_height=$((screen_height < 900 ? screen_height : 900))
popout_x=$((screen_width - popout_width))

umask 077
mkdir -p "$output_dir"
dms ipc call plugin-scan reload dmsAiUsage >/dev/null
sleep 5
dms screenshot output -o "$screen" --stdout --no-file --no-clipboard --no-notify \
  | magick png:- -crop "${bar_width}x64+${bar_x}+0" +repage "$output_dir/bar.ui.png"

dms ipc call widget toggle dmsAiUsage >/dev/null
sleep 1
dms screenshot output -o "$screen" --stdout --no-file --no-clipboard --no-notify \
  | magick png:- -crop "${popout_width}x${popout_height}+${popout_x}+0" +repage "$output_dir/popout.ui.png"
dms ipc call widget toggle dmsAiUsage >/dev/null

printf '%s\n' "$output_dir/bar.ui.png" "$output_dir/popout.ui.png"
