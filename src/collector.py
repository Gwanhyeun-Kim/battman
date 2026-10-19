#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battman 수집 데몬: launchd 가 60초마다 실행. 1회 샘플 후 즉시 종료.

맥이 잠들면 launchd 도 멈추므로 수면 구간은 샘플이 비는데, 시스템 전원 로그
(pmset -g log)에는 잠들기/깨어남/어댑터 연결 시각과 배터리 %가 초 단위로 남는다.
공백(3분+) 직후와 6시간마다 그 로그를 pm_events 테이블에 흡수해 구멍을 메운다."""
import datetime
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from rules import check_rules, convert_amperage  # noqa: E402

DB = os.path.join(BASE, "battman.db")
STATE = os.path.join(BASE, "state.json")
PRUNE_MARKER = os.path.join(BASE, "prune.marker")
PM_MARKER = os.path.join(BASE, "pm.marker")
IOREG = "/usr/sbin/ioreg"
MACMON = "/opt/homebrew/bin/macmon"
PS = "/bin/ps"
PMSET = "/usr/bin/pmset"
RETENTION_SEC = 90 * 86400
TOP_N = 8
PM_GAP_MIN = 3.0          # 이 이상 샘플이 비었으면(=잠들었다 깸) 전원 로그 동기화
PM_CATCHUP_SEC = 6 * 3600  # 공백 없어도 주기적으로 최근 8시간 재동기화 (짧은 수면 대비)
PM_CATCHUP_SPAN = 8 * 3600

# 2026-09-08 13:56:19 +0900 Sleep    \tEntering Sleep state due to 'Clamshell Sleep':... Using Batt (Charge:41%) 1057 secs
# 2026-09-08 14:53:16 +0900 DarkWake \tDarkWake from Deep Idle [CDNPB] : due to ... USB-C_plug ... Using AC (Charge:40%) 0 secs
# 도메인 뒤에 공백 2개 이상 — 'Wake Requests' 같은 다른 도메인과 구분
PM_RX = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4}) (Sleep|Wake|DarkWake)[ \t]{2,}"
    r"(.*?)Using (Batt|BATT|AC) ?\(Charge: ?(\d+)%?\)")
PM_SLEEP_REASON = re.compile(r"due to '([^']+)'")
PM_WAKE_REASON = re.compile(r"due to (.*?)\s*$")

DDL = [
    """CREATE TABLE IF NOT EXISTS samples (
        ts INTEGER PRIMARY KEY,
        pct INTEGER NOT NULL,
        on_battery INTEGER NOT NULL,
        is_charging INTEGER NOT NULL,
        watts REAL,
        gap_min REAL)""",
    """CREATE TABLE IF NOT EXISTS proc_samples (
        ts INTEGER NOT NULL,
        pid INTEGER NOT NULL,
        ppid INTEGER NOT NULL,
        pcpu REAL NOT NULL,
        comm TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_proc_ts ON proc_samples(ts)",
    """CREATE TABLE IF NOT EXISTS anomalies (
        ts INTEGER NOT NULL,
        rule TEXT NOT NULL,
        detail TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS health (
        ts INTEGER PRIMARY KEY,
        cycle_count INTEGER,
        raw_max INTEGER,
        nominal INTEGER,
        design INTEGER,
        temp_c REAL)""",
    """CREATE TABLE IF NOT EXISTS pm_events (
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,
        pct INTEGER,
        on_battery INTEGER,
        detail TEXT,
        PRIMARY KEY (ts, kind))""",
]


def parse_pm_events(text, start_ts, end_ts):
    """pmset -g log 본문에서 잠들기/깨어남/다크웨이크 이벤트 추출.
    반환: [(ts, kind, pct, on_battery, detail)] 시간순. kind = sleep|darkwake|wake."""
    out = []
    for line in text.splitlines():
        m = PM_RX.match(line)
        if not m:
            continue
        try:
            ts = int(datetime.datetime.strptime(
                m.group(1), "%Y-%m-%d %H:%M:%S %z").timestamp())
        except ValueError:
            continue
        if not (start_ts <= ts <= end_ts):
            continue
        kind = m.group(2).lower()
        body = m.group(3)
        if kind == "sleep":
            r = PM_SLEEP_REASON.search(body)
            detail = r.group(1) if r else body.strip()
        else:
            r = PM_WAKE_REASON.search(body)
            detail = r.group(1).strip(" /:") if r else body.strip()
        pct = int(m.group(5))
        if pct > 100:
            continue
        out.append((ts, kind, pct, 0 if m.group(4) == "AC" else 1, detail[:120]))
    out.sort(key=lambda e: (e[0], e[1]))
    return out


def store_pm_events(conn, events):
    """INSERT OR IGNORE — 같은 (ts, kind) 는 한 번만. 새로 들어간 행 수 반환."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO pm_events(ts, kind, pct, on_battery, detail) VALUES (?,?,?,?,?)",
        events)
    return conn.total_changes - before


def sync_pm_events(conn, start_ts, end_ts):
    """pmset 로그(약 1.5초)를 읽어 구간 내 이벤트를 저장. 실패해도 조용히 0."""
    try:
        out = subprocess.run([PMSET, "-g", "log"], capture_output=True,
                             text=True, timeout=60).stdout
    except Exception:
        return 0
    n = store_pm_events(conn, parse_pm_events(out, start_ts, end_ts))
    try:
        with open(PM_MARKER, "w") as f:
            f.write(str(int(end_ts)))
    except OSError:
        pass
    return n


def pm_sync_window(conn, prev_ts, gap_min, now):
    """이번 실행에서 전원 로그를 읽어야 하면 (start, end), 아니면 None.
    첫 실행(테이블 비어 있음)은 로그 전체, 공백 직후는 공백 앞뒤, 6시간마다 최근 8시간."""
    if conn.execute("SELECT 1 FROM pm_events LIMIT 1").fetchone() is None:
        return (0, now + 1)
    if gap_min is not None and gap_min >= PM_GAP_MIN and prev_ts:
        return (prev_ts - 600, now + 1)
    try:
        last = os.path.getmtime(PM_MARKER)
    except OSError:
        last = 0
    if now - last >= PM_CATCHUP_SEC:
        return (now - PM_CATCHUP_SPAN, now + 1)
    return None


def init_db(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    for stmt in DDL:
        conn.execute(stmt)
    try:
        conn.execute("ALTER TABLE samples ADD COLUMN cpu_temp REAL")
    except sqlite3.OperationalError:
        pass  # 이미 있음


def read_temps(samples=2, interval=250):
    """macmon(선택 설치)으로 CPU/GPU 온도. 유휴 센서 출렁임 때문에 다중 샘플 중앙값.
    없거나 실패하면 (None, None)."""
    if not os.path.exists(MACMON):
        return (None, None)
    try:
        out = subprocess.run(
            [MACMON, "pipe", "-s", str(samples), "-i", str(interval)],
            capture_output=True, text=True, timeout=10).stdout
        cs, gs = [], []
        for line in out.strip().splitlines():
            t = json.loads(line).get("temp", {})
            if t.get("cpu_temp_avg"):
                cs.append(t["cpu_temp_avg"])
            if t.get("gpu_temp_avg"):
                gs.append(t["gpu_temp_avg"])

        def med(a):
            return sorted(a)[len(a) // 2] if a else None

        c, g = med(cs), med(gs)
        return (round(c, 1) if c else None, round(g, 1) if g else None)
    except Exception:
        return (None, None)


def ioreg_dump():
    return subprocess.run([IOREG, "-rn", "AppleSmartBattery"],
                          capture_output=True, text=True, timeout=10).stdout


def _grabber(out):
    def grab(key):
        m = re.search(r'"%s" = (\S+)' % key, out)
        return m.group(1) if m else None
    return grab


def read_health(out):
    """일일 건강 스냅샷용: 사이클·용량·온도. 값 없으면 None."""
    grab = _grabber(out)

    def num(key, div=1.0):
        v = grab(key)
        try:
            return int(v) / div if div != 1 else int(v)
        except (TypeError, ValueError):
            return None

    return {"cycle_count": num("CycleCount"),
            "raw_max": num("AppleRawMaxCapacity"),
            "nominal": num("NominalChargeCapacity"),
            "design": num("DesignCapacity"),
            "temp_c": num("Temperature", 100.0)}


def read_battery(out=None):
    if out is None:
        out = ioreg_dump()
    grab = _grabber(out)

    pct = int(grab("CurrentCapacity"))
    volt = int(grab("Voltage"))
    amp = convert_amperage(grab("InstantAmperage"))
    ext = grab("ExternalConnected") == "Yes"
    chg = grab("IsCharging") == "Yes"
    watts = round(volt / 1000.0 * (-amp) / 1000.0, 2) if amp < 0 else None
    return pct, ext, chg, watts


def read_procs():
    out = subprocess.run([PS, "-Ao", "pid,ppid,pcpu,comm", "-r"],
                         capture_output=True, text=True, timeout=10).stdout.splitlines()
    procs = []
    for line in out[1:TOP_N + 1]:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        procs.append((int(parts[0]), int(parts[1]), float(parts[2]),
                      os.path.basename(parts[3].strip())))
    return procs


def prune(conn, now):
    try:
        if os.path.exists(PRUNE_MARKER) and now - os.path.getmtime(PRUNE_MARKER) < 86400:
            return
        cutoff = now - RETENTION_SEC
        for table in ("samples", "proc_samples", "anomalies", "pm_events"):
            conn.execute("DELETE FROM %s WHERE ts < ?" % table, (cutoff,))
        with open(PRUNE_MARKER, "w") as f:
            f.write(str(now))
    except Exception:
        pass


def main():
    now = int(time.time())
    conn = sqlite3.connect(DB, timeout=30)
    try:
        init_db(conn)
        prev = conn.execute(
            "SELECT ts, cpu_temp FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        gap_min = round((now - prev[0]) / 60.0, 1) if prev else None
        dump = ioreg_dump()
        pct, ext, chg, watts = read_battery(dump)
        last_h = conn.execute("SELECT MAX(ts) FROM health").fetchone()[0]
        if last_h is None or now - last_h >= 86400:
            h = read_health(dump)
            conn.execute(
                "INSERT OR REPLACE INTO health(ts, cycle_count, raw_max, nominal, design, temp_c) "
                "VALUES (?,?,?,?,?,?)",
                (now, h["cycle_count"], h["raw_max"], h["nominal"], h["design"], h["temp_c"]))
        cpu_temp, _ = read_temps()
        if cpu_temp is not None and prev and prev[1]:  # 지수평활로 출렁임 완화
            cpu_temp = round(0.5 * prev[1] + 0.5 * cpu_temp, 1)
        conn.execute(
            "INSERT OR REPLACE INTO samples"
            "(ts, pct, on_battery, is_charging, watts, gap_min, cpu_temp) "
            "VALUES (?,?,?,?,?,?,?)",
            (now, pct, 0 if ext else 1, 1 if chg else 0, watts, gap_min, cpu_temp))
        for pid, ppid, pcpu, comm in read_procs():
            conn.execute(
                "INSERT INTO proc_samples(ts, pid, ppid, pcpu, comm) VALUES (?,?,?,?,?)",
                (now, pid, ppid, pcpu, comm))
        check_rules(conn, now, STATE)
        prune(conn, now)
        conn.commit()
        # 샘플은 이미 저장됐으니, 전원 로그 동기화는 실패해도 이번 샘플에 영향 없음
        win = pm_sync_window(conn, prev[0] if prev else None, gap_min, now)
        if win:
            try:
                sync_pm_events(conn, win[0], win[1])
                conn.commit()
            except Exception:
                sys.stderr.write("[%s] pm_events sync error\n%s\n"
                                 % (time.strftime("%F %T"), traceback.format_exc()))
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.stderr.write("[%s] collector error\n%s\n"
                         % (time.strftime("%F %T"), traceback.format_exc()))
        sys.exit(1)
