#!/usr/bin/env bash
set -euo pipefail

if (( EUID == 0 )); then
    echo 'Run as your normal user, not with sudo.' >&2
    exit 1
fi

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/rclone-mounts-tasks@psark007.github.io"
target="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/rclone-mounts-tasks@psark007.github.io"
install -d -m 0755 "$target"
for name in backend.py extension.js metadata.json prefs.js stylesheet.css; do
    install -m 0644 "$source_dir/$name" "$target/$name"
done
echo "Installed extension code at $target"
echo 'Enable it in the Extensions app or run: gnome-extensions enable rclone-mounts-tasks@psark007.github.io'
echo 'No mount or transfer was started.'
