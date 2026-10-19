#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battman 이상 감지 규칙. collector 가 샘플 저장 직후 호출."""
import json
import os
import subprocess

import slope

COOLDOWN_SEC = 2 * 3600
OSASCRIPT = "/usr/bin/osascript"

R1_PERSIST = 600    # 이 시간 전 창도 기준 초과여야 발동 (순간 스파이크 무시)
R1_BASE_TTL = 6 * 3600  # 평소 기울기 캐시 수명(초)
R2_CPU = 80.0       # pcpu 임계
R2_STREAK = 5       # 연속 샘플 수
R3_MIN_GAP = 30.0   # 수면 판정 최소 gap(분)
R3_RATE = 2.0       # %/h 초과 시 수면 과다 방전
SLEEP_NORMAL = 0.5  # 정상 수면 방전 %/h (배수 표시용)


def convert_amperage(raw):
    """ioreg InstantAmperage 는 64비트 unsigned 로 찍힘. two's complement 복원."""
    v = int(raw)
    if v >= 2 ** 63:
        v -= 2 ** 64
    return v


def notify(body):
    safe = body.replace("\\", "").replace('"', "'")
    subprocess.run(
        [OSASCRIPT, "-e",
         'display notification "%s" with title "배터리 관리자" sound name "Funk"' % safe],
        timeout=10)


def _load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def check_rules(conn, now, state_path, notify_fn=notify):
    st = _load_state(state_path)
    fired = []

    def trigger(rule, key, detail):
        conn.execute("INSERT INTO anomalies(ts, rule, detail) VALUES (?,?,?)",
                     (now, rule, detail))
        notified = False
        if now - st.get(key, 0) >= COOLDOWN_SEC:
            notify_fn(detail)
            st[key] = now
            notified = True
        fired.append((rule, detail, notified))

    # R1 급방전: 직전 30분 기울기가 평소(최근 2주 중앙값)의 1.5배를 넘고 10분 전에도 넘었을 때
    base = st.get("r1_base")
    if now - st.get("r1_base_ts", 0) >= R1_BASE_TTL:
        base = slope.baseline(conn, now)
        st["r1_base"], st["r1_base_ts"] = base, now
    thr = slope.threshold(base)
    rate = slope.current_rate(conn, now)
    prev = slope.current_rate(conn, now - R1_PERSIST) if rate is not None and rate > thr else None
    if prev is not None and prev > thr:
        pct = conn.execute("SELECT pct FROM samples WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                           (now,)).fetchone()[0]
        vs = " 평소(시간당 %.0f%%)의 %.1f배." % (base, rate / base) if base and base > 0 else ""
        top = conn.execute(
            "SELECT comm, AVG(pcpu) a FROM proc_samples WHERE ts > ? AND ts <= ? "
            "GROUP BY comm ORDER BY a DESC LIMIT 1", (now - R1_PERSIST, now)).fetchone()
        suspect = " 범인 후보: %s (CPU 평균 %.0f%%)" % (top[0], top[1]) if top else ""
        trigger("rapid_drain", "rapid_drain",
                "급방전: 이 속도면 %s 뒤 방전 (시간당 %.0f%%).%s%s"
                % (slope.fmt_hours(pct / rate), rate, vs, suspect))

    # R2 CPU 도둑
    ts5 = [r[0] for r in conn.execute(
        "SELECT ts FROM samples ORDER BY ts DESC LIMIT ?", (R2_STREAK,))]
    if len(ts5) == R2_STREAK:
        ph = ",".join("?" * R2_STREAK)
        for pid, ppid, comm in conn.execute(
                "SELECT pid, ppid, comm FROM proc_samples WHERE ts = ? AND pcpu >= ?",
                (ts5[0], R2_CPU)).fetchall():
            cnt = conn.execute(
                "SELECT COUNT(DISTINCT ts) FROM proc_samples "
                "WHERE ts IN (%s) AND pid = ? AND comm = ? AND pcpu >= ?" % ph,
                tuple(ts5) + (pid, comm, R2_CPU)).fetchone()[0]
            if cnt == R2_STREAK:
                orphan = " (고아 프로세스)" if ppid == 1 else ""
                trigger("cpu_hog", "cpu_hog:%s" % comm,
                        "CPU 도둑: %s%s 5분째 CPU %.0f%% 이상. 종료하려면 kill -9 %d"
                        % (comm, orphan, R2_CPU, pid))

    # R3 수면 과다 방전
    last2 = conn.execute(
        "SELECT ts, pct, gap_min FROM samples ORDER BY ts DESC LIMIT 2").fetchall()
    if len(last2) == 2 and last2[0][2] is not None and last2[0][2] >= R3_MIN_GAP:
        gap_h = last2[0][2] / 60.0
        drop = last2[1][1] - last2[0][1]
        if drop > 0:
            rate = drop / gap_h
            if rate > R3_RATE:
                trigger("sleep_drain", "sleep_drain",
                        "수면 과다 방전: 잠든 %.1f시간 동안 %d%% 감소 "
                        "(시간당 %.1f%%, 정상의 %.0f배)"
                        % (gap_h, drop, rate, rate / SLEEP_NORMAL))

    _save_state(state_path, st)
    return fired
