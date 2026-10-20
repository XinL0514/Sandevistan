#!/bin/zsh
# Cache the exact LGPL source archives shipped with the macOS bundle.
set -eu

project_dir="$(cd "$(dirname "$0")/.." && pwd)"
source_dir="$project_dir/distribution/sources"
manifest="$source_dir/SHA256SUMS.txt"

while read -r expected filename; do
    [[ -n "$filename" ]] || continue
    target="$source_dir/$filename"
    if [[ -f "$target" ]] && [[ "$(shasum -a 256 "$target" | cut -d ' ' -f 1)" == "$expected" ]]; then
        continue
    fi
    if [[ "$filename" == pyside-* ]]; then
        url="https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/$filename"
    else
        url="https://download.qt.io/official_releases/qt/6.11/6.11.1/submodules/$filename"
    fi
    print "下载 LGPL 对应源码：$filename"
    curl --fail --location --retry 3 --retry-all-errors --output "$target.part" "$url"
    actual="$(shasum -a 256 "$target.part" | cut -d ' ' -f 1)"
    if [[ "$actual" != "$expected" ]]; then
        print -u2 "源码校验失败：$filename"
        exit 1
    fi
    mv "$target.part" "$target"
done < "$manifest"

(cd "$source_dir" && shasum -a 256 -c SHA256SUMS.txt)
