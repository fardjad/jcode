#!/usr/bin/env bash
# Install a patched jcode release from fardjad/jcode without changing shell files.
set -euo pipefail

repo="fardjad/jcode"
install_dir="${JCODE_INSTALL_DIR:-$HOME/.local/bin}"
requested_version="${1:-}"

usage() {
  cat <<'EOF'
Usage: install.sh [VERSION]

Install the latest patched jcode release, or a specific release VERSION.
VERSION may be written with or without its leading v, for example 0.80.0 or
v0.80.0.

Environment:
  JCODE_INSTALL_DIR  Installation directory. Defaults to ~/.local/bin.
EOF
}

if [[ "$requested_version" == "-h" || "$requested_version" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "$#" -gt 1 ]]; then
  usage >&2
  exit 2
fi

case "$(uname -s)" in
  Darwin) platform="macos" ;;
  Linux) platform="linux" ;;
  *)
    echo "Unsupported operating system: $(uname -s)" >&2
    exit 1
    ;;
esac
case "$(uname -m)" in
  x86_64|amd64) arch="x86_64" ;;
  arm64|aarch64) arch="aarch64" ;;
  *)
    echo "Unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required to install jcode." >&2
  exit 1
fi
if ! command -v tar >/dev/null 2>&1; then
  echo "tar is required to install jcode." >&2
  exit 1
fi

if [[ -n "$requested_version" ]]; then
  tag="${requested_version#v}"
  tag="v$tag"
else
  tag="$(curl --fail --location --silent --show-error \
    "https://api.github.com/repos/$repo/releases/latest" \
    | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    | head -n 1)"
  if [[ -z "$tag" ]]; then
    echo "Could not determine the latest jcode release." >&2
    exit 1
  fi
fi

asset="jcode-$platform-$arch.tar.gz"
base_url="https://github.com/$repo/releases/download/$tag"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

printf 'Installing jcode %s for %s %s...\n' "$tag" "$platform" "$arch"
curl --fail --location --show-error --output "$tmp_dir/$asset" "$base_url/$asset"
curl --fail --location --show-error --output "$tmp_dir/SHA256SUMS" "$base_url/SHA256SUMS"

expected="$(awk -v asset="$asset" '$2 == asset { print $1 }' "$tmp_dir/SHA256SUMS")"
if [[ -z "$expected" ]]; then
  echo "No checksum found for $asset in $tag." >&2
  exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "$tmp_dir/$asset" | awk '{print $1}')"
else
  actual="$(shasum -a 256 "$tmp_dir/$asset" | awk '{print $1}')"
fi
if [[ "$actual" != "$expected" ]]; then
  echo "Checksum verification failed for $asset." >&2
  exit 1
fi

tar -xzf "$tmp_dir/$asset" -C "$tmp_dir"
if [[ ! -f "$tmp_dir/jcode-$platform-$arch" ]]; then
  echo "Release archive did not contain the expected jcode binary." >&2
  exit 1
fi

mkdir -p "$install_dir"
install -m 755 "$tmp_dir/jcode-$platform-$arch" "$install_dir/jcode"
printf 'Installed jcode %s to %s/jcode\n' "$tag" "$install_dir"

case ":$PATH:" in
  *":$install_dir:"*) ;;
  *)
    printf '\nTo use jcode from a new shell, add this directory to PATH:\n'
    printf '  export PATH="%s:$PATH"\n' "$install_dir"
    ;;
esac
