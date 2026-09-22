#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
destination="${1:?usage: install.sh DESTINATION}"
parent_dir="$(dirname "$destination")"
mkdir -p "$parent_dir"

# Build the complete installed project in a sibling directory, then swap it in
# with rollback. No jcode configuration is read or written by this helper.
stage="$(mktemp -d "$parent_dir/.delegation-efficiency.XXXXXX")"
backup="${destination}.previous.$$"
old_moved=0
cleanup() {
  status=$?
  if [[ "$status" -ne 0 && "$old_moved" -eq 1 && ! -e "$destination" && -e "$backup" ]]; then
    mv "$backup" "$destination" || true
  fi
  rm -rf "$stage" "$backup"
  exit "$status"
}
trap cleanup EXIT

install -m 644 "$source_dir/pyproject.toml" "$source_dir/uv.lock" "$stage/"
install -m 755 "$source_dir/record.py" "$source_dir/report.py" "$source_dir/retention.py" "$stage/"
cp -R "$source_dir/delegation_efficiency" "$stage/"
python3 -m compileall -q "$stage/delegation_efficiency"

if [[ -e "$destination" || -L "$destination" ]]; then
  mv "$destination" "$backup"
  old_moved=1
fi
mv "$stage" "$destination"
old_moved=0
printf 'Installed delegation-efficiency plugin: %s\n' "$destination"
