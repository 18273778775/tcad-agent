#!/bin/sh
# SPDX-FileCopyrightText: 2026 LiTao
# SPDX-License-Identifier: Apache-2.0
set -eu

# Requires Python 3.11+ or uv. Override source/root explicitly for offline installs.
package_source=${TCAD_INSTALL_SOURCE:-https://codeload.github.com/18273778775/tcad-agent/zip/refs/tags/v0.3.1}
install_root=${TCAD_INSTALL_ROOT:-${XDG_DATA_HOME:-"$HOME/.local/share"}/tcad-agent-cli}
download_directory=
temporary_shim=
cleanup() {
    if [ -n "$download_directory" ]; then
        rm -f "$download_directory/package.zip"
        rmdir "$download_directory" 2>/dev/null || :
    fi
    if [ -n "$temporary_shim" ]; then rm -f "$temporary_shim"; fi
}
trap cleanup 0
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
case "$package_source" in
    http://*|https://*)
        source_path=${package_source%%\?*}
        source_path=${source_path%%\#*}
        case "$source_path" in
            *.zip|*.whl|*.tar.gz|*.tar.bz2|*.tar.xz|*.tgz) ;;
            *)
                download_directory=$(mktemp -d "${TMPDIR:-/tmp}/tcad-install-XXXXXXXX")
                curl -fLsS --connect-timeout 15 --max-time 60 --retry 0 --output "$download_directory/package.zip" --url "$package_source"
                package_source="$download_directory/package.zip"
                ;;
        esac
        ;;
esac
mkdir -p "$install_root/venvs" "$install_root/bin"
install_root=$(cd "$install_root" && pwd)
environment=$(mktemp -d "$install_root/venvs/0.3.1-XXXXXXXX")
if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 "$environment"
    uv pip install --python "$environment/bin/python" "$package_source"
else
    python_command=
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
            python_command=$candidate
            break
        fi
    done
    if [ -z "$python_command" ]; then
        printf '%s\n' 'Python 3.11+ or uv is required. Install Python, then rerun.' >&2
        exit 1
    fi
    "$python_command" -m venv "$environment"
    "$environment/bin/python" -m pip install --disable-pip-version-check --no-input --timeout 60 --retries 2 "$package_source"
fi
"$environment/bin/tcad" --version
temporary_shim="$install_root/bin/.tcad-$$"
ln -s "$environment/bin/tcad" "$temporary_shim"
mv -f "$temporary_shim" "$install_root/bin/tcad"
# An explicit env file avoids silently editing arbitrary shell profile files.
"$environment/bin/python" -c 'import shlex,sys; print("export PATH=" + shlex.quote(sys.argv[1]) + ":$PATH")' "$install_root/bin" > "$install_root/env"
printf 'TCAD installed. In your shell run: . "%s/env"\n' "$install_root"
printf '%s\n' 'Then run: tcad capabilities. Add that source command to your shell profile for future terminals.'
