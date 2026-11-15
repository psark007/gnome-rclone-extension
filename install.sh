#!/usr/bin/env bash
set -euo pipefail

if (( EUID == 0 )); then
    echo 'Run as your normal user, not with sudo.' >&2
    exit 1
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_dir="$project_dir/rclone-manager@psark007.github.io"
extensions_dir="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions"
target="$extensions_dir/rclone-manager@psark007.github.io"
install -d -m 0755 "$target"
for name in backend.py extension.js metadata.json prefs.js stylesheet.css; do
    install -m 0644 "$source_dir/$name" "$target/$name"
done
install -d -m 0755 "$target/schemas"
for schema in "$project_dir"/schemas/*.gschema.xml; do
    install -m 0644 "$schema" "$target/schemas/"
done
glib-compile-schemas "$target/schemas"
echo "Installed extension code at $target"
echo 'Enable it in the Extensions app or run: gnome-extensions enable rclone-manager@psark007.github.io'
echo 'No mount or transfer was started.'
