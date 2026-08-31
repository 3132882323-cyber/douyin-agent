#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h:h}"
[[ "$(uname -s)" == "Darwin" ]] || {
  print -u2 "macOS release artifacts must be built on macOS."
  exit 1
}
ARCH="$(uname -m)"
[[ "$ARCH" == "arm64" ]] || {
  print -u2 "DianAgent's current security runtime requires Apple Silicon (arm64); Intel release artifacts are not supported."
  exit 1
}

TEMP_PARENT="${TMPDIR:-/tmp}"
PUBLIC_BUILD_ROOT="$(/usr/bin/mktemp -d "$TEMP_PARENT/DianAgent-macos-public.XXXXXX")"
PUBLIC_SOURCE="$PUBLIC_BUILD_ROOT/source"
RELEASE_ROOT=""
STAGE=""
ZIP=""
BUILD_COMPLETE=0

release_cleanup() {
  local public_root="${PUBLIC_BUILD_ROOT:A}"
  local expected_parent="${TEMP_PARENT:A}"
  if [[ -d "$public_root" && ! -L "$public_root" && "${public_root:h:A}" == "$expected_parent" &&
        "${public_root:t}" == DianAgent-macos-public.* ]]; then
    /bin/rm -rf "$public_root" >/dev/null 2>&1 || true
  fi
  if [[ "$BUILD_COMPLETE" != "1" && -n "$ZIP" ]]; then
    [[ -L "$ZIP" ]] || /bin/rm -f "$ZIP" >/dev/null 2>&1 || true
    [[ -L "$ZIP.sha256" ]] || /bin/rm -f "$ZIP.sha256" >/dev/null 2>&1 || true
  fi
  if [[ "$BUILD_COMPLETE" != "1" && -n "$STAGE" && -n "$RELEASE_ROOT" &&
        "${STAGE:h:A}" == "${RELEASE_ROOT:A}" && "${STAGE:t}" == DianAgent-v*-macos-* &&
        -d "$STAGE" && ! -L "$STAGE" ]]; then
    /bin/rm -rf "$STAGE" >/dev/null 2>&1 || true
  fi
}
trap release_cleanup EXIT

# The developer checkout may contain the registered local-only Chengfang
# runtime.  Compile only from the independently rescanned public tree; never
# rely on the final artifact scan to remove modules already frozen by
# PyInstaller.
/usr/bin/python3 "$PROJECT_ROOT/tools/prepare_public_source.py" \
  --source "$PROJECT_ROOT" --destination "$PUBLIC_SOURCE"
/usr/bin/python3 "$PUBLIC_SOURCE/tools/check_public_release.py" --source "$PUBLIC_SOURCE"
# A distributable package must first prove all crash-recovery transitions on
# the same Darwin/launchd environment that will build it. This is deliberately
# local to the build command so a manual workflow cannot bypass the gate.
(cd "$PUBLIC_SOURCE/bridge" && /usr/bin/python3 -m unittest -v \
  test_macos_release.MacOSInstallTransactionBehaviorTests)
"$PUBLIC_SOURCE/tools/macos/build_agent.sh"

VERSION="$(/usr/bin/python3 - "$PUBLIC_SOURCE/extension/manifest.json" <<'PY'
import json, pathlib, sys
print(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))["version"])
PY
)"
print -r -- "$VERSION" | /usr/bin/grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$' || { print -u2 "Invalid release version."; exit 1; }

RELEASE_ROOT="$PROJECT_ROOT/dist/release"
STAGE="$RELEASE_ROOT/DianAgent-v$VERSION-macos-$ARCH"
ZIP="$RELEASE_ROOT/DianAgent-v$VERSION-macos-$ARCH.zip"
[[ "$STAGE" == "$RELEASE_ROOT/DianAgent-v$VERSION-macos-$ARCH" && "$STAGE" != "$RELEASE_ROOT" ]] || { print -u2 "Unsafe release staging path."; exit 1; }
for release_path in "$PROJECT_ROOT/dist" "$RELEASE_ROOT" "$STAGE" "$ZIP" "$ZIP.sha256"; do
  if [[ -e "$release_path" || -L "$release_path" ]]; then
    [[ ! -L "$release_path" ]] || { print -u2 "Unsafe symbolic link in release output path: $release_path"; exit 1; }
  fi
done
/bin/mkdir -p "$RELEASE_ROOT"
/bin/rm -rf "$STAGE"
/bin/mkdir -p "$STAGE/app" "$STAGE/extension" "$STAGE/tools/macos"
/bin/cp "$PUBLIC_SOURCE/dist/agent-macos/DianAgent" "$STAGE/app/DianAgent"
/bin/chmod 755 "$STAGE/app/DianAgent"
/usr/bin/rsync -a --exclude 'test-*.js' --exclude 'manifest.compat.json' "$PUBLIC_SOURCE/extension/" "$STAGE/extension/"
RUNTIME_TOOLS=(
  atomic_directory_update.sh
  install_dian_agent.command
  launch_agent.sh
  recovery_bootstrap.sh
  repair_dian_agent.command
  uninstall_dian_agent.command
  verify_local_api.sh
)
for runtime_tool in "${RUNTIME_TOOLS[@]}"; do
  [[ -f "$PUBLIC_SOURCE/tools/macos/$runtime_tool" && ! -L "$PUBLIC_SOURCE/tools/macos/$runtime_tool" ]] || {
    print -u2 "Missing or unsafe macOS runtime tool: $runtime_tool"
    exit 1
  }
  /bin/cp "$PUBLIC_SOURCE/tools/macos/$runtime_tool" "$STAGE/tools/macos/$runtime_tool"
done
/bin/chmod 755 "$STAGE/tools/macos/"*.sh "$STAGE/tools/macos/"*.command
/bin/cp "$PUBLIC_SOURCE/README-MAC.md" "$STAGE/README-MAC.md"
/bin/cp "$PUBLIC_SOURCE/LICENSE" "$STAGE/LICENSE"
print -r -- "$ARCH" > "$STAGE/macos-architecture.txt"
/bin/cp "$STAGE/tools/macos/install_dian_agent.command" "$STAGE/install_dian_agent.command"
/bin/cp "$STAGE/tools/macos/repair_dian_agent.command" "$STAGE/repair_dian_agent.command"
/bin/cp "$STAGE/tools/macos/uninstall_dian_agent.command" "$STAGE/uninstall_dian_agent.command"
/bin/chmod 755 "$STAGE/"*.command

/usr/bin/python3 "$PUBLIC_SOURCE/tools/check_public_release.py" --artifact "$STAGE"

/bin/rm -f "$ZIP" "$ZIP.sha256"
/usr/bin/ditto -c -k --sequesterRsrc --keepParent "$STAGE" "$ZIP"
/usr/bin/unzip -tq "$ZIP" >/dev/null || {
  /bin/rm -f "$ZIP"
  print -u2 "macOS release ZIP integrity check failed."
  exit 1
}
/usr/bin/python3 "$PUBLIC_SOURCE/tools/check_public_release.py" --artifact "$ZIP" || {
  /bin/rm -f "$ZIP"
  print -u2 "Public macOS ZIP boundary check failed."
  exit 1
}
(cd "$RELEASE_ROOT" && /usr/bin/shasum -a 256 "${ZIP:t}" > "${ZIP:t}.sha256")
BUILD_COMPLETE=1
print "macOS release: $ZIP"
print "This artifact is architecture-specific and must be built and tested on macOS."
