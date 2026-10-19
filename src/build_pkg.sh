#!/bin/bash
# 배터리 관리자 — 배포용 .pkg 설치파일 빌드
#   · 유니버설 바이너리 (Apple Silicon + Intel)
#   · launchd 라벨과 앱 식별자를 배포용 이름으로 설정
#   · 설치 후 격리 플래그 자동 해제 (postinstall)
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PROJ="$(dirname "$SRC")"
DIST="$PROJ/dist"
BUILD="$DIST/build"
ROOT="$DIST/pkgroot"
# pkgbuild 는 NFC 한글 경로에서 payload 를 비워버림 -> NFD 로 강제
APPNAME="$(printf '배터리 관리자.app' | iconv -f UTF-8 -t UTF-8-MAC)"
APP="$ROOT/$APPNAME"
VERSION="1.0"
LABEL="com.battman.collector"

rm -rf "$DIST"
mkdir -p "$BUILD" "$ROOT" "$DIST/scripts" "$DIST/resources"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/battman/vendor"

# ── 1. 유니버설 바이너리 ────────────────────────────────────────────
echo "[1/6] Swift 유니버설 빌드 (arm64 + x86_64)"
for ARCH in arm64 x86_64; do
  xcrun swiftc -O -target "$ARCH-apple-macos13.0" \
    -o "$BUILD/battman-$ARCH" "$SRC/main.swift" \
    -framework Cocoa -framework WebKit
done
lipo -create -output "$APP/Contents/MacOS/battman" \
  "$BUILD/battman-arm64" "$BUILD/battman-x86_64"

# ── 2. Info.plist ──────────────────────────────────────────────────
echo "[2/6] 번들 구성"
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key><string>battman</string>
  <key>CFBundleIdentifier</key><string>com.battman.app</string>
  <key>CFBundleName</key><string>배터리 관리자</string>
  <key>CFBundleDisplayName</key><string>배터리 관리자</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleIconFile</key><string>app.icns</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

# ── 3. 리소스 (라벨 중립화) ────────────────────────────────────────
RES="$APP/Contents/Resources/battman"
cp "$SRC/collector.py" "$SRC/rules.py" "$SRC/slope.py" "$SRC/report.py" "$SRC/optimize.py" "$RES/"
cp "$SRC/bootstrap.sh" "$RES/bootstrap.sh"
cp "$SRC/com.battman.collector.plist.template" "$RES/$LABEL.plist.template"
chmod +x "$RES/bootstrap.sh"
cp "$SRC/vendor/"*.js "$RES/vendor/"
cp "$SRC/app.icns" "$APP/Contents/Resources/"

# 잔여 개인 식별자 검사 — 하나라도 남으면 빌드 중단
if grep -Erq "/Users/[^/]+" "$APP" 2>/dev/null; then
  echo "!! 사용자 홈 경로가 번들에 남아 있습니다"; grep -Ern "/Users/[^/]+" "$APP"; exit 1
fi

# ── 4. 서명 + AppleDouble 제거 ─────────────────────────────────────
echo "[3/6] ad-hoc 서명"
xattr -cr "$APP"
find "$ROOT" -name '._*' -delete 2>/dev/null || true
find "$ROOT" -name '.DS_Store' -delete 2>/dev/null || true
codesign --force --deep -s - "$APP"

# ── 5. postinstall + 설치 화면 문구 ────────────────────────────────
echo "[4/6] 인스톨러 스크립트"
cat > "$DIST/scripts/postinstall" << 'POST'
#!/bin/bash
# 설치된 앱의 격리 플래그 해제 (Gatekeeper "손상됨" 경고 방지)
# 파일명 인코딩(NFC/NFD)에 의존하지 않도록 번들 ID 로 찾는다
for a in /Applications/*.app; do
  id="$(/usr/bin/defaults read "$a/Contents/Info" CFBundleIdentifier 2>/dev/null)"
  if [ "$id" = "com.battman.app" ]; then
    /usr/bin/xattr -dr com.apple.quarantine "$a" 2>/dev/null
  fi
done
exit 0
POST
chmod +x "$DIST/scripts/postinstall"

cat > "$DIST/resources/welcome.html" << 'WEL'
<meta charset="utf-8">
<body style="font:13px -apple-system,'Apple SD Gothic Neo',sans-serif;line-height:1.6">
<h2>🔋 배터리 관리자</h2>
<p>맥북 배터리를 1분마다 기록하고, <b>배터리를 몰래 갉아먹는 프로세스</b>를 찾아 알려주는 도구입니다.</p>
<ul>
  <li><b>급방전 감지</b> — 시간당 30% 이상 빠지면 알림</li>
  <li><b>CPU 도둑 추적</b> — 지속적으로 CPU를 점유하는 프로세스 색출</li>
  <li><b>수면 중 방전 감지</b> — 덮어놨는데 배터리가 닳는 경우</li>
  <li><b>대시보드</b> — 24시간 / 3일 / 1주일 배터리 그래프</li>
</ul>
<p style="color:#666">인터넷 통신을 하지 않습니다. 모든 기록은 본인 맥 안
(<code>~/.config/battman/</code>)에만 저장됩니다.</p>
<p style="color:#666">macOS 13(Ventura) 이상 · Apple Silicon &amp; Intel 모두 지원</p>
</body>
WEL

cat > "$DIST/resources/conclusion.html" << 'CON'
<meta charset="utf-8">
<body style="font:13px -apple-system,'Apple SD Gothic Neo',sans-serif;line-height:1.6">
<h2>설치 완료 ✅</h2>
<p><b>응용 프로그램</b> 폴더에서 <b>배터리 관리자</b>를 실행하세요.</p>
<p>처음 실행하면 백그라운드 수집기가 자동으로 등록되고 대시보드가 열립니다.
그래프는 데이터가 쌓일수록(1~2시간 뒤부터) 의미 있어집니다.</p>
<p style="color:#666">· 첫 알림이 뜰 때 알림 권한을 허용해 주세요.<br>
· 대시보드의 <b>최적화</b> 버튼은 CPU를 붙잡고 있는 프로세스를 실제로 종료합니다.
확인 창에서 목록을 보고 결정하세요.</p>
</body>
CON

# ── 6. 패키지 빌드 ─────────────────────────────────────────────────
echo "[5/6] pkgbuild"
pkgbuild --component "$APP" \
         --identifier com.battman.installer \
         --version "$VERSION" \
         --scripts "$DIST/scripts" \
         --install-location /Applications \
         "$BUILD/battman-component.pkg" >/dev/null

cat > "$BUILD/distribution.xml" << 'XML'
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="1">
  <title>배터리 관리자</title>
  <organization>com.battman</organization>
  <welcome file="welcome.html" mime-type="text/html"/>
  <conclusion file="conclusion.html" mime-type="text/html"/>
  <options customize="never" require-scripts="false" hostArchitectures="arm64,x86_64"/>
  <volume-check>
    <allowed-os-versions><os-version min="13.0"/></allowed-os-versions>
  </volume-check>
  <choices-outline><line choice="default"/></choices-outline>
  <choice id="default"><pkg-ref id="com.battman.installer"/></choice>
  <pkg-ref id="com.battman.installer" version="1.0">battman-component.pkg</pkg-ref>
</installer-gui-script>
XML

echo "[6/6] productbuild"
productbuild --distribution "$BUILD/distribution.xml" \
             --resources "$DIST/resources" \
             --package-path "$BUILD" \
             "$DIST/배터리관리자-$VERSION.pkg" >/dev/null

# ── 7. 페이로드 검증 ───────────────────────────────────────────────
echo "[검증] 페이로드 추출 테스트"
VERIFY="$BUILD/verify"
rm -rf "$VERIFY" && mkdir -p "$VERIFY"   # pkgutil --expand 는 대상이 없어야 함
pkgutil --expand "$DIST/배터리관리자-$VERSION.pkg" "$VERIFY/x" >/dev/null
( cd "$VERIFY" && mkdir -p out && tar -xf x/battman-component.pkg/Payload -C out 2>/dev/null \
  || ( cd out && cpio -i --quiet < ../x/battman-component.pkg/Payload ) )
VAPP="$(find "$VERIFY/out" -maxdepth 2 -name '*.app' -print -quit)"
[ -n "$VAPP" ] || { echo "!! 페이로드에 앱이 없습니다"; exit 1; }
echo "  추출된 앱: $(basename "$VAPP") ($(du -sh "$VAPP" | cut -f1))"
echo "  아키텍처 : $(lipo -archs "$VAPP/Contents/MacOS/battman")"
codesign -v "$VAPP" 2>&1 && echo "  서명 검증 : OK" || echo "  서명 검증 : 경고(ad-hoc)"
python3 - "$VAPP" << 'PYV'
import os, sys
app = sys.argv[1]
need = ["Contents/MacOS/battman", "Contents/Info.plist", "Contents/Resources/app.icns",
        "Contents/Resources/battman/collector.py", "Contents/Resources/battman/rules.py", "Contents/Resources/battman/slope.py",
        "Contents/Resources/battman/report.py", "Contents/Resources/battman/optimize.py",
        "Contents/Resources/battman/bootstrap.sh",
        "Contents/Resources/battman/com.battman.collector.plist.template",
        "Contents/Resources/battman/vendor/chart.umd.js",
        "Contents/Resources/battman/vendor/chartjs-plugin-zoom.min.js",
        "Contents/Resources/battman/vendor/hammer.min.js"]
miss = [f for f in need if not os.path.exists(os.path.join(app, f))]
print("  파일 검사 : " + ("전체 %d개 OK" % len(need) if not miss else "누락 " + str(miss)))
sys.exit(1 if miss else 0)
PYV

echo
echo "완료: $DIST/배터리관리자-$VERSION.pkg"
lipo -archs "$APP/Contents/MacOS/battman"
du -h "$DIST/배터리관리자-$VERSION.pkg"
