#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
PROJECT_ROOT="${SCRIPT_DIR:h:h}"
BRIDGE_DIR="$PROJECT_ROOT/bridge"
VENV_DIR="$BRIDGE_DIR/.venv-macos"

[[ "$(uname -s)" == "Darwin" ]] || { print -u2 "Must run on macOS."; exit 1; }
ARCH="$(uname -m)"
[[ "$ARCH" == "arm64" ]] || {
  print -u2 "DianAgent's current security runtime requires Apple Silicon (arm64); Intel macOS builds are not supported."
  exit 1
}
command -v python3 >/dev/null || { print -u2 "Python 3 is required for the build machine."; exit 1; }

# Refuse to freeze a binary under a different extension/package version.  The
# installer also checks the runtime receipt, but a mismatched artifact should
# never leave the build machine in the first place.
VERSIONS="$(python3 - "$BRIDGE_DIR/version.py" "$PROJECT_ROOT/extension/manifest.json" <<'PY'
import ast
import json
import pathlib
import sys

tree = ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"), filename=sys.argv[1])
agent_version = None
for node in tree.body:
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "AGENT_VERSION" for target in targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                agent_version = value
if agent_version is None:
    raise SystemExit("AGENT_VERSION is missing or not a string literal")
manifest_version = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))["version"]
print(agent_version)
print(manifest_version)
PY
)" || { print -u2 "Could not read exact Agent and extension versions."; exit 1; }
AGENT_VERSION="${VERSIONS%%$'\n'*}"
MANIFEST_VERSION="${VERSIONS##*$'\n'}"
[[ "$AGENT_VERSION" == "$MANIFEST_VERSION" ]] || {
  print -u2 "Agent version $AGENT_VERSION does not match extension version $MANIFEST_VERSION."
  exit 1
}
print -r -- "$AGENT_VERSION" | /usr/bin/grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$' || {
  print -u2 "Invalid Agent release version."
  exit 1
}

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$BRIDGE_DIR/requirements-build.txt"
"$VENV_DIR/bin/python" -m PyInstaller --noconfirm --clean --distpath "$PROJECT_ROOT/dist/agent-macos" --workpath "$PROJECT_ROOT/dist/pyinstaller-macos-work" "$BRIDGE_DIR/dian_agent_macos.spec"

AGENT="$PROJECT_ROOT/dist/agent-macos/DianAgent"
[[ -x "$AGENT" ]] || { print -u2 "Build completed without DianAgent."; exit 1; }
/usr/bin/file "$AGENT" | /usr/bin/grep -q "$ARCH" || { print -u2 "Built Agent architecture does not match $ARCH."; exit 1; }
SELF_TEST_ROOT="$(mktemp -d)"
trap '/bin/rm -rf "$SELF_TEST_ROOT"' EXIT
DIAN_AGENT_SELF_TEST=1 \
  DIAN_AGENT_INSTALL_ROOT="$SELF_TEST_ROOT/install" \
  DIAN_AGENT_DATA_DIR="$SELF_TEST_ROOT/data" \
  DIAN_AGENT_LOG_DIR="$SELF_TEST_ROOT/logs" \
  "$AGENT"
print "macOS Agent: $AGENT"
