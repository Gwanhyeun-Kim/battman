#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battman 최적화 도구: 배터리 먹는 범인 찾기·정리 (확인 후에만).

찾는 것 두 가지:
  hog   — 최근 10분 중 5분 이상 CPU 50%+ 이고 지금도 도는 프로세스
          (대시보드 권장 조치·CPU 도둑 알림과 같은 기준)
  storm — 초당 1개 이상 새 프로세스를 계속 띄우는 부모 (TouchEn nxKey 가
          ps 를 초당 4번 띄워 ecosystemd·trustd 를 달구던 패턴)
대상은 창 없이 뒤에서 도는 것뿐이다. Dock 에 뜨는 앱과 그 자식(Chrome 탭,
터미널 속 작업 등)은 사용자가 쓰는 것이라 절대 끄자고 하지 않고 보고만 한다.
시스템 프로세스도 끄지 않고 '같이 바빠진 것'으로만 보고한다.
"""
import json
import os
import plistlib
import re
import shlex
import signal
import sqlite3
import statistics
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "battman.db")
PS = "/bin/ps"
LAUNCHCTL = "/bin/launchctl"
LIPO = "/usr/bin/lipo"
LSAPPINFO = "/usr/bin/lsappinfo"
OSASCRIPT = "/usr/bin/osascript"

# 절대 건드리면 안 되는 프로세스 (시스템 필수)
DENYLIST = {
    "kernel_task", "launchd", "WindowServer", "Finder", "Dock", "loginwindow",
    "SystemUIServer", "WindowManager", "coreaudiod", "bluetoothd", "powerd",
    "configd", "distnoted", "cfprefsd", "syslogd", "logd", "opendirectoryd",
    "securityd", "tccd", "trustd", "backupd", "mds", "mds_stores", "mdworker",
    "mdworker_shared", "photoanalysisd", "mediaanalysisd", "battman",
}
# 이 경로의 실행 파일은 macOS 구성요소 — 끄지 않고 보고만
SYSTEM_PREFIXES = ("/System/Library/", "/usr/libexec/", "/usr/sbin/", "/sbin/")
# 사용자가 직접 쓰는 도구: 자식을 많이 띄우는 게 정상이라 storm 판정 제외, hog 는 기본 체크 해제
INTERACTIVE = {
    "zsh", "bash", "sh", "fish", "login", "ghostty", "Terminal", "iTerm2",
    "tmux", "tmux: server", "screen",
    "claude", "Claude", "codex", "Codex", "make", "ninja", "xcodebuild",
    "cargo", "swift-frontend", "swiftc",
}

# 인텔 전용 앱이 프로세스를 띄울 때마다 일하는 데몬 (Rosetta 분석·서명 검사)
ROSETTA_DAEMONS = {"ecosystemd", "ecosystemanalyticsd", "trustd", "syspolicyd", "oahd"}

HOG_WINDOW = 10       # 최근 샘플 수 (약 10분)
HOG_MIN_HITS = 5      # 그중 몇 샘플 이상
HOG_CPU = 50.0        # pcpu 임계 — report.build_recs 와 동일
LIVE_MIN_CPU = 20.0   # 지금 측정에서도 이 이상이어야 (이미 멈춘 건 제외)
STORM_RATE = 1.0      # 초당 새 자식 프로세스 수
BUSY_CPU = 20.0       # '같이 바빠진 시스템 프로세스' 보고 임계
SNAP_COUNT = 20
SNAP_WINDOW = 3.0     # 초

LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DOMAIN_RE = re.compile(r"^(system|gui/\d+)$")


# ---------------------------------------------------------------- 측정

def parse_cputime(s):
    """ps time 필드 → 초. macOS 는 'MMM:SS.ss', 길면 'D-HH:MM:SS.ss' 도 대비."""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    secs = 0.0
    for part in s.split(":"):
        secs = secs * 60 + float(part)
    return days * 86400 + secs


def parse_ps(text):
    """'pid ppid uid time exe' 줄들 → {pid: {ppid, uid, cpu(누적초), exe}}."""
    snap = {}
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            snap[int(parts[0])] = {"ppid": int(parts[1]), "uid": int(parts[2]),
                                   "cpu": parse_cputime(parts[3]),
                                   "exe": parts[4].strip()}
        except ValueError:
            continue
    return snap


def take_snapshots(count=SNAP_COUNT, window=SNAP_WINDOW):
    snaps, stamps = [], []
    for i in range(count):
        stamps.append(time.time())
        out = subprocess.run([PS, "-Ao", "pid=,ppid=,uid=,time=,comm="],
                             capture_output=True, text=True, timeout=10).stdout
        snaps.append(parse_ps(out))
        if i < count - 1:
            time.sleep(window / (count - 1))
    return snaps, max(stamps[-1] - stamps[0], 0.001)


def live_stats(snaps, elapsed):
    """(pid별 실측 CPU%, 부모별 측정 중 새로 뜬 자식 pid 집합)."""
    first, last = snaps[0], snaps[-1]
    cpu = {}
    for pid, p in last.items():
        f = first.get(pid)
        if f and f["exe"] == p["exe"]:
            cpu[pid] = max(p["cpu"] - f["cpu"], 0.0) / elapsed * 100.0
    # 새 pid 는 측정 전부터 살아 있던 조상 몫으로 센다 (sh 는 순식간에 끝나도
    # 그 밑의 ps·grep 이 잡히면 원래 부모가 띄운 것으로 집계)
    parent = {}
    for snap in snaps:
        for pid, p in snap.items():
            parent.setdefault(pid, p["ppid"])
    spawned = {}
    for snap in snaps[1:]:
        for pid in snap:
            if pid in first:
                continue
            root = parent[pid]
            while root not in first and root in parent and root > 1:
                root = parent[root]
            spawned.setdefault(root, set()).add(pid)
    return cpu, spawned


def own_tree(snap, pid):
    """자기 자신과 조상들 (측정용 ps 를 띄우는 우리 자신을 storm 으로 잡지 않게)."""
    seen = set()
    while pid and pid not in seen and pid > 1:
        seen.add(pid)
        p = snap.get(pid)
        if not p:
            break
        pid = p["ppid"]
    return seen


def parse_lsappinfo(text):
    """lsappinfo list → Dock 에 뜨는 일반 앱(Foreground) pid 집합."""
    return {int(m.group(1)) for m in re.finditer(r'pid = (\d+) type="Foreground"', text)}


def foreground_pids():
    r = subprocess.run([LSAPPINFO, "list"], capture_output=True, text=True, timeout=10)
    return parse_lsappinfo(r.stdout)


def in_app(pid, parent, foreground):
    """Dock 앱 자신이거나 그 밑에서 돌면 사용자가 쓰는 것 → 보호."""
    seen = set()
    while pid > 1 and pid not in seen:
        if pid in foreground:
            return True
        seen.add(pid)
        pid = parent.get(pid, 0)
    return False


def app_guard():
    """지금 시점 기준 '사용자가 쓰는 앱에 속하나' 판정 함수 (대시보드 버튼용)."""
    out = subprocess.run([PS, "-Ao", "pid=,ppid="], capture_output=True, text=True).stdout
    parent = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            parent[int(parts[0])] = int(parts[1])
    fg = foreground_pids()
    return lambda pid: in_app(pid, parent, fg)


def power_context(conn, now):
    """배터리 사용 중이면 {지금 W(최근 10분 평균), 평소 W(최근 7일 중앙값)}."""
    now_w = [r[0] for r in conn.execute(
        "SELECT watts FROM samples WHERE ts >= ? AND on_battery = 1 AND watts IS NOT NULL",
        (now - 600,))]
    if len(now_w) < 3:
        return None
    usual = [r[0] for r in conn.execute(
        "SELECT watts FROM samples WHERE ts >= ? AND ts < ? AND on_battery = 1 "
        "AND watts IS NOT NULL", (now - 7 * 86400, now - 3600))]
    return {"now_w": round(sum(now_w) / len(now_w), 1),
            "usual_w": round(statistics.median(usual), 1) if usual else None}


def sustained_hogs(conn):
    """최근 10분 중 5샘플 이상 CPU 50%+ → {pid: (comm, avg, hits)}."""
    ts = [r[0] for r in conn.execute(
        "SELECT ts FROM samples ORDER BY ts DESC LIMIT ?", (HOG_WINDOW,))]
    if len(ts) < HOG_MIN_HITS:
        return {}
    ph = ",".join("?" * len(ts))
    return {pid: (comm, avg, cnt) for pid, comm, cnt, avg in conn.execute(
        "SELECT pid, comm, COUNT(*), AVG(pcpu) FROM proc_samples "
        "WHERE ts IN (%s) AND pcpu >= ? GROUP BY pid, comm HAVING COUNT(*) >= ?" % ph,
        tuple(ts) + (HOG_CPU, HOG_MIN_HITS))}


def is_system(comm, exe):
    return comm in DENYLIST or exe.startswith(SYSTEM_PREFIXES)


def is_intel_only(exe):
    if not exe.startswith("/"):
        return False
    r = subprocess.run([LIPO, "-archs", exe], capture_output=True, text=True)
    archs = r.stdout.split()
    return r.returncode == 0 and bool(archs) and "arm64" not in archs


# ---------------------------------------------------------------- launchd

def launchctl_print(target):
    r = subprocess.run([LAUNCHCTL, "print", target], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def default_launchd_dirs(uid):
    gui = "gui/%d" % uid
    return [(os.path.expanduser("~/Library/LaunchAgents"), gui),
            ("/Library/LaunchAgents", gui),
            ("/Library/LaunchDaemons", "system")]


def find_launchd_job(exe, pid, uid, dirs=None, printer=launchctl_print):
    """이 pid 를 띄워 두고 죽이면 되살리는 launchd 서비스 → {domain, label} 또는 None."""
    real = os.path.realpath(exe)
    for d, domain in (dirs if dirs is not None else default_launchd_dirs(uid)):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".plist"):
                continue
            try:
                with open(os.path.join(d, name), "rb") as f:
                    pl = plistlib.load(f)
            except Exception:
                continue
            label = pl.get("Label")
            prog = pl.get("Program") or (pl.get("ProgramArguments") or [None])[0]
            if not (isinstance(label, str) and isinstance(prog, str)
                    and LABEL_RE.match(label)):
                continue
            if os.path.realpath(prog) != real:
                continue
            out = printer("%s/%s" % (domain, label))
            m = re.search(r"^\s*pid = (\d+)", out or "", re.M)
            if m and int(m.group(1)) == pid:
                return {"domain": domain, "label": label}
    return None


# ---------------------------------------------------------------- 분석

def analyze(conn, snaps, elapsed, my_uid, exclude,
            job_finder=find_launchd_job, intel=is_intel_only, foreground=frozenset()):
    first, last = snaps[0], snaps[-1]
    cpu, spawned = live_stats(snaps, elapsed)
    parent = {pid: p["ppid"] for snap in (first, last) for pid, p in snap.items()}

    busy = []
    for pid, c in cpu.items():
        p = last[pid]
        comm = os.path.basename(p["exe"])
        if c >= BUSY_CPU and is_system(comm, p["exe"]):
            busy.append({"pid": pid, "comm": comm, "cpu": round(c, 1)})
    busy.sort(key=lambda b: -b["cpu"])
    busy = busy[:6]

    found = {}
    for ppid, kids in spawned.items():
        rate = len(kids) / elapsed
        p = last.get(ppid) or first.get(ppid)
        if rate < STORM_RATE or ppid <= 1 or ppid in exclude or not p:
            continue
        comm = os.path.basename(p["exe"])
        if comm in INTERACTIVE or is_system(comm, p["exe"]) or in_app(ppid, parent, foreground):
            continue
        found[ppid] = {"kind": "storm", "pid": ppid, "comm": comm, "exe": p["exe"],
                       "uid": p["uid"], "cpu": round(cpu.get(ppid, 0.0), 1),
                       "rate": round(rate, 1), "avg": None, "hits": None}

    apps = []
    for pid, (comm, avg, hits) in sustained_hogs(conn).items():
        p = last.get(pid)
        c = cpu.get(pid, 0.0)
        if (not p or os.path.basename(p["exe"]) != comm or c < LIVE_MIN_CPU
                or pid in exclude or is_system(comm, p["exe"])):
            continue
        if in_app(pid, parent, foreground):
            apps.append({"pid": pid, "comm": comm, "cpu": round(c, 1)})
            continue
        item = found.setdefault(pid, {"kind": "hog", "pid": pid, "comm": comm,
                                      "exe": p["exe"], "uid": p["uid"], "rate": None})
        item.update({"cpu": round(c, 1), "avg": round(avg, 1), "hits": hits})

    items = []
    busy_names = "·".join(b["comm"] for b in busy if b["comm"] in ROSETTA_DAEMONS)
    for it in found.values():
        it["intel"] = intel(it["exe"])
        it["job"] = job_finder(it["exe"], it["pid"], it["uid"])
        it["admin"] = needs_admin(it["job"], it["uid"], my_uid)
        it["checked"] = it["comm"] not in INTERACTIVE
        why = []
        if it["kind"] == "storm":
            why.append("초당 %.1f개씩 새 프로세스를 계속 띄우는 중" % it["rate"])
            if it["intel"]:
                why.append("인텔 전용 앱이라 띄울 때마다 Rosetta 번역·서명 검사가 붙음"
                           + (" → %s까지 같이 바빠짐" % busy_names if busy_names else ""))
        else:
            why.append("최근 10분 중 %d분 동안 평균 CPU %.0f%%, 지금 %.0f%%"
                       % (it["hits"], it["avg"], it["cpu"]))
        if it["job"]:
            why.append("자동 재시작 서비스라 서비스째 중지 (재부팅하면 다시 켜짐)")
        it["why"] = " · ".join(why)
        items.append(it)
    items.sort(key=lambda i: (i["kind"] != "storm", -i["cpu"]))

    top = sorted(((c, pid) for pid, c in cpu.items() if pid not in exclude),
                 reverse=True)[:5]
    apps.sort(key=lambda a: -a["cpu"])
    return {"items": items, "busy": busy, "apps": apps,
            "top": [{"pid": pid, "comm": os.path.basename(last[pid]["exe"]),
                     "cpu": round(c, 1)} for c, pid in top if c >= 5],
            "total_cpu": round(sum(cpu.values()), 1),
            "window": round(elapsed, 1)}


# ---------------------------------------------------------------- 정리

def ps_one(pid):
    out = subprocess.run([PS, "-p", str(pid), "-o", "uid=,ppid=,comm="],
                         capture_output=True, text=True).stdout.strip()
    parts = out.split(None, 2)
    if len(parts) < 3:
        return None
    return {"uid": int(parts[0]), "ppid": int(parts[1]), "exe": parts[2].strip()}


def pids_of(exe):
    """같은 실행 파일로 도는 (pid, ppid) 목록."""
    out = subprocess.run([PS, "-Ao", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2].strip() == exe:
            found.append((int(parts[0]), int(parts[1])))
    return found


def send_signal(pid, sig):
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_admin(script):
    """관리자 암호 창 한 번으로 root 작업 일괄 실행 → (성공, stderr)."""
    esc = script.replace("\\", "\\\\").replace('"', '\\"')
    r = subprocess.run(
        [OSASCRIPT, "-e", 'do shell script "%s" with administrator privileges' % esc],
        capture_output=True, text=True, timeout=600)
    return r.returncode == 0, r.stderr.strip()


def run_launchctl(args):
    return subprocess.run([LAUNCHCTL] + args, capture_output=True).returncode == 0


def admin_script(actions):
    parts = []
    for a in actions:
        if a[0] == "bootout":
            domain, label = a[1], a[2]
            if not (DOMAIN_RE.match(domain) and LABEL_RE.match(label)):
                raise ValueError("unsafe launchd target: %r" % (a,))
            parts.append("/bin/launchctl bootout %s/%s 2>/dev/null" % (domain, label))
        elif a[0] == "kill":
            pid, exe = int(a[1]), a[2]
            if pid <= 1:
                raise ValueError("unsafe pid: %r" % (pid,))
            # 암호 입력 사이 pid 가 재사용됐을 수 있으니 죽이기 직전마다 실행 파일 재확인
            same = '[ "$(/bin/ps -p %d -o comm=)" = %s ]' % (pid, shlex.quote(exe))
            parts.append("%s && { /bin/kill -TERM %d 2>/dev/null; /bin/sleep 1; "
                         "%s && /bin/kill -KILL %d 2>/dev/null; }" % (same, pid, same, pid))
        else:
            raise ValueError("unknown action: %r" % (a,))
    return "; ".join(parts) + "; true"


def needs_admin(job, uid, my_uid):
    """내 GUI 세션 서비스나 내 프로세스가 아니면 관리자 권한이 필요하다."""
    if job:
        return job["domain"] != "gui/%d" % my_uid
    return uid != my_uid


def apply_items(items, my_uid, ps_one=ps_one, job_finder=find_launchd_job,
                kill=send_signal, run_admin=run_admin, launchctl=run_launchctl,
                sleep=time.sleep, pids_of=pids_of):
    """선택된 범인 정리. 입력은 pid·exe 만 믿고 나머지는 전부 지금 다시 확인한다."""
    lines, acted, admin_actions, admin_items = [], [], [], []
    for it in items:
        pid, exe = int(it["pid"]), it["exe"]
        name = os.path.basename(exe)
        cur = ps_one(pid)
        if not cur or cur["exe"] != exe or name in DENYLIST:
            lines.append("⏭ %s — 건너뜀 (이미 종료됐거나 다른 프로그램으로 바뀜)" % name)
            continue
        job = job_finder(exe, pid, cur["uid"])
        before = {p for p, _ in pids_of(exe)}
        entry = (pid, exe, name, job, before, cur.get("ppid"))
        if needs_admin(job, cur["uid"], my_uid):
            admin_actions.append(("bootout", job["domain"], job["label"]) if job
                                 else ("kill", pid, exe))
            admin_items.append(entry)
        elif job:
            launchctl(["bootout", "%s/%s" % (job["domain"], job["label"])])
            acted.append(entry)
        else:
            kill(pid, signal.SIGTERM)
            sleep(1.5)
            now = ps_one(pid)
            if now and now["exe"] == exe:  # SIGTERM 무시 → SIGKILL
                kill(pid, signal.SIGKILL)
            acted.append(entry)

    if admin_actions:
        ok, err = run_admin(admin_script(admin_actions))
        if ok:
            acted.extend(admin_items)
        else:
            names = ", ".join(e[2] for e in admin_items)
            if "-128" in err or "cancel" in err.lower():
                lines.append("🔒 %s — 관리자 암호 입력을 취소해서 그대로 둠" % names)
            else:
                lines.append("⚠️ %s — 관리자 권한 실행 실패: %s" % (names, err[:120]))

    if acted:
        sleep(1.0)
    for pid, exe, name, job, before, ppid in acted:
        now = ps_one(pid)
        if now and now["exe"] == exe:
            lines.append("⚠️ %s — 종료 실패 (아직 실행 중)" % name)
            continue
        # 같은 부모(대개 launchd)가 곧바로 다시 띄운 것만 '되살아남'으로 본다
        back = {p for p, pp in pids_of(exe) if pp == ppid} - before - {pid}
        if back:
            lines.append("⚠️ %s — 종료했지만 바로 다시 실행됨 (PID %s)"
                         % (name, ", ".join(map(str, sorted(back)))))
        elif job:
            lines.append("✅ %s — 서비스 중지됨 (재부팅하면 다시 켜짐)" % name)
        else:
            lines.append("✅ %s — 종료됨" % name)
    return "\n".join(lines) or "정리할 항목이 없습니다"


def main():
    try:
        if "--scan" in sys.argv:
            snaps, elapsed = take_snapshots()
            conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
            try:
                res = analyze(conn, snaps, elapsed, os.getuid(),
                              own_tree(snaps[-1], os.getpid()),
                              foreground=foreground_pids())
                res["power"] = power_context(conn, int(time.time()))
            finally:
                conn.close()
            print(json.dumps(res, ensure_ascii=False))
            return 0
        if "--apply" in sys.argv:
            i = sys.argv.index("--apply")
            print(apply_items(json.loads(sys.argv[i + 1]), os.getuid()))
            return 0
    except Exception as e:  # 앱이 '깨끗함'으로 오해하지 않게 오류를 그대로 전달
        print(json.dumps({"error": "%s: %s" % (type(e).__name__, e)}, ensure_ascii=False))
        return 1
    print("usage: optimize.py --scan | --apply '[{\"pid\": N, \"exe\": PATH}, ...]'")
    return 1


if __name__ == "__main__":
    sys.exit(main())
