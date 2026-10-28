#!/bin/sh
set -eu

# Build a controlled, arm64 macOS application bundle. The bundle contains the
# exact CPython runtime and wheel dependencies used at build time; operator
# data remains outside the bundle in ~/Library/Application Support/Mailforge.

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ARCH=$(uname -m)
if [ "$ARCH" != "arm64" ]; then
  echo "This initial package target is macOS arm64; found $ARCH" >&2
  exit 2
fi

OUTPUT=${1:-"$PROJECT_ROOT/dist/Mailforge.app"}
STAGE="$PROJECT_ROOT/.package-stage"
RESOURCES="$OUTPUT/Contents/Resources"
rm -rf "$STAGE" "$OUTPUT"
mkdir -p "$STAGE" "$RESOURCES" "$OUTPUT/Contents/MacOS"

cd "$PROJECT_ROOT"
npm --prefix web ci
npm --prefix web run build
uv sync --locked --no-dev

BASE_PREFIX=$(uv run --locked python -c 'import sys; print(sys.base_prefix)')
BASE_EXECUTABLE=$(uv run --locked python -c 'import sys; print(sys._base_executable)')
uv venv --python "$BASE_EXECUTABLE" "$STAGE/venv"
uv pip install --python "$STAGE/venv/bin/python" "$PROJECT_ROOT"

cp -R "$BASE_PREFIX" "$RESOURCES/python"
mkdir -p "$RESOURCES/site-packages"
cp -R "$STAGE/venv/lib/python3.15/site-packages/." "$RESOURCES/site-packages/"
# Local wheel installation metadata can contain the developer checkout path;
# the application bundle must not depend on or disclose that path.
find "$RESOURCES/site-packages" -name direct_url.json -delete

cat > "$OUTPUT/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDisplayName</key><string>Mailforge</string>
  <key>CFBundleExecutable</key><string>Mailforge</string>
  <key>CFBundleIdentifier</key><string>com.mailforge.local</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>Mailforge</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.1.0</string>
  <key>CFBundleVersion</key><string>0.1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

cat > "$OUTPUT/Contents/MacOS/Mailforge" <<'LAUNCHER'
#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RESOURCES="$HERE/Resources"
export PYTHONHOME="$RESOURCES/python"
export PYTHONPATH="$RESOURCES/site-packages"
exec "$RESOURCES/python/bin/python3.15" -m mailforge app open "$@"
LAUNCHER
chmod 755 "$OUTPUT/Contents/MacOS/Mailforge"

VERSION=$(PYTHONPATH=src uv run --locked python -c 'from mailforge import __version__; print(__version__)')
CHECKSUM=$(shasum -a 256 "$OUTPUT/Contents/MacOS/Mailforge" | awk '{print $1}')
printf '%s\n' "Mailforge macOS arm64 bundle" "version=$VERSION" "artifact=Mailforge.app" "launcher_sha256=$CHECKSUM" > "$OUTPUT/Contents/Resources/BUILD-METADATA.txt"
(cd "$OUTPUT/Contents" && find . -type f ! -name BUILD-METADATA.sha256 -print | sort | while IFS= read -r file; do shasum -a 256 "$file"; done) > "$OUTPUT/Contents/Resources/BUILD-METADATA.sha256"

rm -rf "$STAGE"
echo "Built $OUTPUT"
echo "Version: $VERSION"
echo "Launcher SHA-256: $CHECKSUM"
echo "Signing/notarization: not performed (no authorized identity supplied)"
