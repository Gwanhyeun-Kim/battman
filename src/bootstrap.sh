#!/bin/bash
# 배터리 관리자.app 부트스트랩: 실행 때마다 런타임 동기화 후 대시보드 오픈
set -euo pipefail
RES="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/battman"
PLIST="$HOME/Library/LaunchAgents/com.battman.collector.plist"

mkdir -p "$DEST/vendor" "$HOME/Library/LaunchAgents"
cp -f "$RES/collector.py" "$RES/rules.py" "$RES/slope.py" "$RES/report.py" "$RES/optimize.py" "$DEST/"
cp -f "$RES/vendor/"*.js "$DEST/vendor/"
sed "s|__HOME__|$HOME|g" "$RES/com.battman.collector.plist.template" > "$PLIST.new"
if ! cmp -s "$PLIST.new" "$PLIST" 2>/dev/null; then
  mv "$PLIST.new" "$PLIST"
  launchctl bootout "gui/$(id -u)/com.battman.collector" 2>/dev/null || true
else
  rm "$PLIST.new"
fi
launchctl print "gui/$(id -u)/com.battman.collector" >/dev/null 2>&1 \
  || launchctl bootstrap "gui/$(id -u)" "$PLIST"
[ -f "$DEST/battman.db" ] || /usr/bin/python3 "$DEST/collector.py"
exec /usr/bin/python3 "$DEST/report.py"
