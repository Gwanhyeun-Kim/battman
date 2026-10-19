#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battman 대시보드: battman.db(수집기 샘플 + pm_events 전원 로그) -> report.html 생성.

수집기는 맥이 잠들면 멈추므로, 수면 구간은 collector 가 pmset 로그에서 흡수해 둔
pm_events(잠들기/다크웨이크/깨어남, 각각 배터리 %와 AC/배터리)로 메운다.
타임라인은 실제 시간축이라 공백이 공백으로 보이고, 휠/드래그/핀치로 확대된다."""
import bisect
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
from collector import read_battery, read_temps, read_health, ioreg_dump  # noqa: E402
from optimize import DENYLIST, app_guard, is_system, ps_one  # noqa: E402
import slope  # noqa: E402
import html as _html  # noqa: E402

DB = os.path.join(BASE, "battman.db")
OUT = os.path.join(BASE, "report.html")
OPEN = "/usr/bin/open"
SP = "/usr/sbin/system_profiler"
PERIODS = (("p24h", "24시간", 86400), ("p3d", "3일", 259200), ("p7d", "1주", 604800))
RULE_LABEL = {"rapid_drain": "⚡ 급방전", "cpu_hog": "🔥 CPU 도둑", "sleep_drain": "🌙 수면 방전"}
SLEEP_MIN_SEC = 120   # 이보다 짧은 수면은 구간으로 안 침
GAP_SEC = 600         # 수면도 아닌데 이 이상 비면 '미수집' 표시
NEAR_SEC = 45         # pm 이벤트가 수집기 샘플과 이 안에 붙어 있으면 중복이라 생략
SLEEP_CHART_MIN = 20 * 60  # 수면 방전 추이 막대 최소 길이


def sleep_intervals(pm, samples, now):
    """pm_events -> 수면 구간 목록 [{t0, t1, pct0, pct1, reason, wake}].
    잠들기(sleep)에서 열고 완전 기상(wake)에서 닫는다. 다크웨이크는 여전히 수면.
    기상 기록이 없으면 그 뒤 수집기 샘플이 1분 간격으로 다시 이어지는 첫 지점을 기상으로."""
    ivs, cur = [], None
    for ts, kind, pct, ob, detail in pm:
        if kind == "sleep":
            if cur is None:
                cur = {"t0": ts, "pct0": pct, "reason": detail, "t1": None,
                       "pct1": None, "wake": None}
            elif "Low Power" in (detail or "") and ts - cur["t0"] <= 120:
                cur["reason"] = detail  # 잠들자마자 배터리 소진으로 전환된 경우가 진짜 이유
        elif kind == "wake" and cur is not None:
            cur["t1"], cur["pct1"], cur["wake"] = ts, pct, detail
            ivs.append(cur)
            cur = None
    if cur is not None:
        after = [s for s in samples if s[0] > cur["t0"]]
        t1, pct1 = now, None
        for i in range(len(after) - 1):
            if after[i + 1][0] - after[i][0] <= 120:
                t1, pct1 = after[i][0], after[i][1]
                break
        cur["t1"], cur["pct1"] = t1, pct1
        ivs.append(cur)
    return [iv for iv in ivs if iv["t1"] - iv["t0"] >= SLEEP_MIN_SEC]


def merge_series(samples, pm, ivs, start, now):
    """수집기 샘플(ts, pct, on_battery, watts, gap_min, cpu_temp) + pm 이벤트 -> 시간순 포인트.
    각 포인트: t, pct, b, w, ct, src(0 수집기/1 전원로그), k(pm kind), d(pm detail),
    asl(수면 구간 안), gap(직전 관측과의 간격 초, 수면 아닌 공백일 때만)."""
    pts = [{"t": r[0], "pct": r[1], "b": r[2], "w": r[3], "ct": r[5], "src": 0}
           for r in samples if start <= r[0] <= now]
    sts = [p["t"] for p in pts]
    bounds = {iv["t0"] for iv in ivs} | {iv["t1"] for iv in ivs}
    for ts, kind, pct, ob, detail in pm:
        if pct is None or not (start <= ts <= now):
            continue
        i = bisect.bisect_left(sts, ts)
        near = [pts[j] for j in (i - 1, i) if 0 <= j < len(sts)]
        # 수집기 샘플 바로 옆의 같은 상태 기록은 중복. 단 수면 경계나 전원 상태가 다른 기록은
        # 정확한 시각 정보라 남긴다 (예: 잠들며 어댑터 뽑힌 순간)
        dup = any(abs(ts - q["t"]) <= NEAR_SEC and q["b"] == ob and abs(q["pct"] - pct) <= 1
                  for q in near)
        if dup and ts not in bounds:
            continue
        pts.append({"t": ts, "pct": pct, "b": ob, "w": None, "ct": None, "src": 1,
                    "k": kind, "d": detail})
    pts.sort(key=lambda p: (p["t"], p["src"]))
    dedup = []
    for p in pts:
        if dedup and dedup[-1]["t"] == p["t"]:
            continue
        dedup.append(p)
    pts = dedup
    starts = [iv["t0"] for iv in ivs]
    for i, p in enumerate(pts):
        j = bisect.bisect_right(starts, p["t"]) - 1
        p["asl"] = 1 if j >= 0 and ivs[j]["t0"] <= p["t"] < ivs[j]["t1"] else 0
        dt = p["t"] - pts[i - 1]["t"] if i else 0
        p["gap"] = dt if (i and dt >= GAP_SEC and not pts[i - 1]["asl"]) else 0
    return pts


def derive_events(pts, ivs):
    """전원 이벤트(어댑터 연결/분리)와 수면 구간을 한 목록으로. 최신순.
    연결/분리는 앞 포인트와 상태가 달라진 지점. 직전 관측이 90초 이내거나 전원 로그가
    플러그/배터리 상태 변화로 깨어난 기록이면 exact, 아니면 since~t 사이로 표시."""
    ev = []
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        if a["b"] == b["b"]:
            continue
        kind = "unplug" if b["b"] else "plug"
        d = (b.get("d") or "")
        exact = (b["t"] - a["t"] <= 90) or ("USB-C_plug" in d) or ("BatteryStateChange" in d)
        b["ev"] = {"kind": kind, "exact": 1 if exact else 0, "since": a["t"]}
        ev.append({"t": b["t"], "kind": kind, "pct": b["pct"], "exact": 1 if exact else 0,
                   "since": a["t"], "src": b["src"]})
    for iv in ivs:
        ev.append({"t": iv["t0"], "kind": "sleep", "t1": iv["t1"], "pct": iv["pct0"],
                   "pct1": iv["pct1"], "reason": iv["reason"], "wake": iv["wake"]})
    ev.sort(key=lambda e: e["t"], reverse=True)
    return ev


def get_health(conn):
    """건강 지표: DB 최신 스냅샷 + system_profiler (Apple 공식 %·판정)."""
    h = {"cycle": None, "apple_pct": None, "condition": None,
         "eff_pct": None, "eff_mah": None, "design_mah": None, "temp": None}
    row = conn.execute(
        "SELECT cycle_count, raw_max, nominal, design, temp_c FROM health "
        "ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        h["cycle"] = row[0]
        eff = row[2] or row[1]
        if eff and row[3]:
            h["eff_pct"] = round(eff / row[3] * 100, 1)
            h["eff_mah"] = int(eff)
            h["design_mah"] = row[3]
        h["temp"] = row[4]
    try:
        sp = subprocess.run([SP, "SPPowerDataType"], capture_output=True,
                            text=True, timeout=15).stdout
        m = re.search(r"Maximum Capacity: (\d+)%", sp)
        if m:
            h["apple_pct"] = int(m.group(1))
        m = re.search(r"Condition: (\w+)", sp)
        if m:
            h["condition"] = m.group(1)
        m = re.search(r"Cycle Count: (\d+)", sp)
        if m and h["cycle"] is None:
            h["cycle"] = int(m.group(1))
    except Exception:
        pass
    try:  # 실시간 온도로 덮어쓰기
        live = read_health(ioreg_dump())
        if live.get("temp_c"):
            h["temp"] = live["temp_c"]
    except Exception:
        pass
    h["cpu_temp"], h["gpu_temp"] = read_temps(samples=3, interval=200)
    return h


def period_verdict(rows, n_anom):
    """기간 한줄 진단."""
    batt = [(r[0], r[1]) for r in rows if r[2] == 1]
    batt_h = len(batt) / 60.0
    drop = 0
    for i in range(1, len(batt)):
        if batt[i][0] - batt[i - 1][0] <= 180 and batt[i][1] < batt[i - 1][1]:
            drop += batt[i - 1][1] - batt[i][1]
    anom_txt = ("이상 %d건 ⚠️" % n_anom) if n_anom else "이상 없음 ✅"
    if batt_h < 0.3:
        return "대부분 충전기 연결 상태 · " + anom_txt
    rate = drop / batt_h
    est = " · 이 속도면 완충에서 약 %.1f시간" % (100 / rate) if rate > 1 else ""
    return ("배터리로 %.1f시간 사용 · 평균 방전 시간당 %.1f%%%s · %s"
            % (batt_h, rate, est, anom_txt))


def build_sleeps(conn, start, ivs):
    """수면 방전 추이 막대. 전원 로그 구간(정확) 우선, 로그가 없는 옛 구간은 샘플 공백으로 보완."""
    sleeps = []
    for iv in ivs:
        dur = iv["t1"] - iv["t0"]
        if dur < SLEEP_CHART_MIN or iv["pct1"] is None:
            continue
        drop = iv["pct0"] - iv["pct1"]
        sleeps.append({"t": iv["t1"], "gap_h": round(dur / 3600.0, 1), "drop": drop,
                       "rate": max(round(drop / (dur / 3600.0), 2), 0)})
    for ts, pct, gap_min in conn.execute(
            "SELECT ts, pct, gap_min FROM samples WHERE ts >= ? AND gap_min >= 30 "
            "ORDER BY ts", (start,)):
        t0 = ts - gap_min * 60
        if any(iv["t0"] - 300 <= t0 and ts <= iv["t1"] + 300 for iv in ivs):
            continue  # 전원 로그 구간이 이미 덮음
        prev = conn.execute(
            "SELECT pct FROM samples WHERE ts < ? ORDER BY ts DESC LIMIT 1",
            (ts,)).fetchone()
        if prev is None:
            continue
        drop = prev[0] - pct
        rate = round(drop / (gap_min / 60.0), 2) if gap_min else 0
        sleeps.append({"t": ts, "gap_h": round(gap_min / 60.0, 1),
                       "drop": drop, "rate": max(rate, 0)})
    sleeps.sort(key=lambda s: s["t"])
    return sleeps


STEEP_MIN_SEC = 600  # 기준 초과가 이만큼 이어져야 급방전 구간 (알림 규칙과 같은 지속 조건)


def mark_slopes(conn, series, start, now, thr):
    """수집기 포인트에 직전 30분 방전 기울기 r(%/h)·직전 10분 충전 기울기 cr(%/h) 을 달고,
    기준 초과가 STEEP_MIN_SEC 이상 이어진 구간을 급방전 구간으로 반환. 구간 안 포인트에는 st=1."""
    rows = conn.execute(
        "SELECT ts, pct, on_battery, is_charging FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (start - slope.WINDOW, now)).fetchall()
    rmap = {r[0]: v for r, v in zip(rows, slope.window_rates(rows)) if v is not None}
    cmap = {r[0]: v for r, v in zip(rows, slope.window_rates(rows, charge=True)) if v is not None}
    for p in series:
        if p["src"] == 0 and p["t"] in rmap:
            p["r"] = round(rmap[p["t"]], 1)
        if p["src"] == 0 and p["t"] in cmap:
            p["cr"] = round(cmap[p["t"]], 1)
    keep = []
    for iv in slope.steep_intervals(series, thr):
        first = iv["t0"] + slope.WINDOW // 2
        if iv["t1"] - first >= STEEP_MIN_SEC:
            keep.append(iv)
            for p in series:
                if first <= p["t"] <= iv["t1"] and p.get("r") is not None:
                    p["st"] = 1
    return keep


def charge_verdict(runs):
    """기간 충전 요약: 충전 구간 수와 전체 평균 속도(올린 %p 합 / 충전 시간 합)."""
    if not runs:
        return ""
    rise = sum(r["pct1"] - r["pct0"] for r in runs)
    hours = sum(r["t1"] - r["t0"] for r in runs) / 3600.0
    slow = sum(r["slow"] for r in runs)
    return (" · 충전 %d회 평균 시간당 +%.0f%%" % (len(runs), rise / hours)
            + (" (느린 충전 %d회 🐢)" % slow if slow else ""))


def build_data(conn, now, base, prof=None):
    data = {}
    thr = slope.threshold(base)
    proj = slope.projection(conn, now)
    cproj = slope.charge_projection(conn, now, prof)
    for key, label, span in PERIODS:
        start = now - span
        rows = conn.execute(
            "SELECT ts, pct, on_battery, watts, gap_min, cpu_temp, is_charging FROM samples "
            "WHERE ts >= ? ORDER BY ts", (start,)).fetchall()
        cruns = slope.charge_runs([(r[0], r[1], r[2], r[6]) for r in rows], prof)
        # 수면 구간은 기간 시작 전에 잠든 경우도 잡아야 하므로 하루 앞서 읽는다
        pm = conn.execute(
            "SELECT ts, kind, pct, on_battery, detail FROM pm_events "
            "WHERE ts >= ? AND ts <= ? ORDER BY ts, kind", (start - 86400, now)).fetchall()
        ivs = [iv for iv in sleep_intervals(pm, rows, now) if iv["t1"] >= start]
        series = merge_series(rows, pm, ivs, start, now)
        steep = mark_slopes(conn, series, start, now, thr)
        events = derive_events(series, ivs)[:80]
        anos = [{"t": r[0], "label": RULE_LABEL.get(r[1], r[1]), "detail": r[2]}
                for r in conn.execute(
                    "SELECT ts, rule, detail FROM anomalies WHERE ts >= ? "
                    "ORDER BY ts DESC LIMIT 50", (start,))]
        nsamples = conn.execute(
            "SELECT COUNT(*) FROM samples WHERE ts >= ?", (start,)).fetchone()[0]
        top = []
        if nsamples:
            top = [{"comm": r[0], "avg": round(r[1] / nsamples, 1),
                    "peak": round(r[2], 0), "mins": r[3]}
                   for r in conn.execute(
                       "SELECT comm, SUM(pcpu), MAX(pcpu), COUNT(*) FROM proc_samples "
                       "WHERE ts >= ? GROUP BY comm ORDER BY SUM(pcpu) DESC LIMIT 10",
                       (start,))]
        data[key] = {"label": label, "start": start, "end": now, "series": series,
                     "sleep_iv": [{"t0": max(iv["t0"], start), "t1": iv["t1"],
                                   "reason": iv["reason"]} for iv in ivs],
                     "events": events, "anomalies": anos, "top": top,
                     "sleeps": build_sleeps(conn, start, ivs),
                     "steep": steep, "thr": round(thr, 1), "runs": slope.drain_runs(rows),
                     "base": round(base, 1) if base else None, "proj": proj,
                     "cruns": cruns, "cproj": cproj, "chg_slow": slope.CHG_SLOW,
                     "verdict": period_verdict(rows, len(anos))
                     + (" · 급방전 구간 %d회 ⚡" % len(steep) if steep else "")
                     + charge_verdict(cruns)}
    return data


def build_cards(conn, now, h=None, base=None, prof=None):
    try:
        pct, ext, chg, watts = read_battery()
    except Exception:
        row = conn.execute(
            "SELECT pct, on_battery, is_charging, watts FROM samples "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        pct, ext, chg, watts = (row[0], row[1] == 0, row[2] == 1, row[3]) if row \
            else (0, False, False, None)
    state = "충전 중" if chg else ("전원 연결" if ext else "배터리 사용 중")
    rows = conn.execute(
        "SELECT ts, pct FROM samples WHERE ts >= ? AND on_battery = 1 ORDER BY ts",
        (now - 3600,)).fetchall()
    rate = None
    r_drop, r_secs = 0.0, 0
    for i in range(1, len(rows)):  # 수면 공백 제외, 연속 샘플 쌍만
        dt = rows[i][0] - rows[i - 1][0]
        if dt <= 180:
            r_secs += dt
            if rows[i][1] < rows[i - 1][1]:
                r_drop += rows[i - 1][1] - rows[i][1]
    if r_secs >= 1200:
        rate = round(r_drop / (r_secs / 3600.0), 1)
    trend = slope.current_rate(conn, now) if not ext else None
    if trend is not None:
        rate = round(trend, 1)  # 30분 회귀 기울기가 있으면 그걸 우선 (알림과 같은 숫자)
    thr = slope.threshold(base)

    # 최근 1주 배터리 사용 구간의 평균 방전율 (연속 샘플 쌍만 합산)
    hist = conn.execute(
        "SELECT ts, pct FROM samples WHERE ts >= ? AND on_battery = 1 ORDER BY ts",
        (now - 7 * 86400,)).fetchall()
    drop, secs = 0.0, 0
    for i in range(1, len(hist)):
        dt = hist[i][0] - hist[i - 1][0]
        if dt <= 180:
            secs += dt
            if hist[i][1] < hist[i - 1][1]:
                drop += hist[i - 1][1] - hist[i][1]
    typical = drop / (secs / 3600.0) if secs >= 1800 and drop > 0 else None

    def fmt_est(h):
        return "24시간 이상" if h > 24 else "약 %.1f시간" % h

    eff_wh = None
    if h and h.get("eff_mah"):
        eff_wh = h["eff_mah"] * 11.4 / 1000.0  # 3셀 공칭 전압 근사

    if not ext and rate and rate > 0.5:  # 1순위: 20분+ 연속 실측 방전 속도
        est_txt, est_sub = fmt_est(pct / rate), "현재 속도 유지 시"
    elif not ext and watts and eff_wh:   # 2순위: 순간 전력 기준
        est_txt = fmt_est(pct / 100.0 * eff_wh / watts)
        est_sub = "현재 전력 %.1fW 기준 (순간치)" % watts
    elif typical and typical > 0.5:
        if ext:
            est_txt = fmt_est(100 / typical)
            est_sub = "완충 기준 · 최근 1주 사용 패턴"
        else:
            est_txt = fmt_est(pct / typical)
            est_sub = "최근 1주 사용 패턴 기준"
    else:
        est_txt, est_sub = "—", "배터리 사용 데이터 수집 중"
    # 방전 속도 카드: 평소 대비 배수로 색 (주의 = 평소 1.2배+, 급방전 = 기준 초과)
    spd_cls, spd_sub = "", "배터리 사용 구간 기준"
    if ext:
        spd_sub = "충전 중 — 어댑터 분리 시 계산"
    elif rate is not None:
        if rate > thr:
            spd_cls = " hot"
            spd_sub = "⚡ 급방전 — 기준 %.0f%%/h 초과" % thr
        elif base and rate > base * 1.2:
            spd_cls = " warm"
            spd_sub = "평소보다 빠름 — 급방전 기준 %.0f%%/h" % thr
        else:
            spd_sub = "정상 — 급방전 기준 %.0f%%/h" % thr
        if base and base > 0:
            spd_sub = "평소 %.0f%%/h의 %.1f배 · " % (base, rate / base) + spd_sub
    spd_card = (spd_cls, "방전 속도 (30분 추세)", "%.1f%%/h" % rate if rate is not None else "—", spd_sub)
    est_card = ("", "예상 사용 시간", est_txt, est_sub)
    if ext:  # 어댑터 연결 중: 방전 속도 자리에 충전 속도, 충전 중이면 사용 시간 자리에 완충까지
        spd_card, est_card = charge_cards(conn, now, pct, chg, prof, est_card)
    cards = [
        ("", "현재 배터리", "%d%%" % pct, state),
        ("", "순간 전력", "%.1f W" % watts if watts else "—",
         "방전 시 소비 전력" if not ext else "충전 중 — 어댑터 분리 시 표시"),
        spd_card,
        est_card,
    ]
    html = ""
    for cls, title, value, sub in cards:
        html += ('<div class="card%s"><div class="ct">%s</div>'
                 '<div class="cv">%s</div><div class="cs">%s</div></div>'
                 % (cls, title, value, sub))
    return html


def charge_cards(conn, now, pct, chg, prof, est_card):
    """어댑터 연결 중 카드 2장: 충전 속도 (10분 추세) + 완충까지.
    속도는 평소 같은 % 범위 속도와 비교 (80% 넘으면 원래 느려지므로 % 범위를 맞춰야 공정)."""
    if not chg:
        sub = ("충전 안 함 — 완충 상태 유지" if pct >= 99
               else "충전 대기 — %d%%에서 멈춤 (최적화 충전 등)" % pct)
        return ("", "충전 속도", "—", sub), est_card
    cp = slope.charge_projection(conn, now, prof)
    rate = slope.current_rate(conn, now, charge=True)
    if cp is None or rate is None:
        return (("", "충전 속도 (10분 추세)", "—", "충전 시작 직후 — 6분쯤 지나면 계산"), est_card)
    cls, sub = "", "평소 충전 곡선 데이터 수집 중"
    if cp["usual"]:
        sub = "평소 이 구간 %.0f%%/h의 %.1f배 · " % (cp["usual"], cp["rate"] / cp["usual"])
        if cp["slow"]:
            cls = " warm"
            sub += "🐢 평소보다 느림 — 저출력 충전기·과열·고부하 확인"
        else:
            sub += "정상"
    spd = (cls, "충전 속도 (10분 추세)", "+%.1f%%/h" % cp["rate"], sub)
    left = max(cp["full"] - now, 0) / 3600.0
    how = "평소 충전 곡선 기준 (80% 넘으면 느려짐)" if cp["usual"] else "지금 속도 유지 시"
    est = ("", "완충까지", slope.fmt_hours(left) if left >= 1 / 60.0 else "곧",
           "%s 100%% 예상 · %s" % (time.strftime("%H:%M", time.localtime(cp["full"])), how))
    return spd, est


def build_health_html(h):
    tiles = []
    if h["cycle"] is not None:
        tiles.append(("충전 사이클", "%d회" % h["cycle"], "통상 한도 1,000회"))
    if h["apple_pct"] is not None:
        warn = " · 교체 고려" if h["apple_pct"] < 80 else ""
        tiles.append(("최대 용량 (Apple 기준)", "%d%%" % h["apple_pct"],
                      "80%% 미만이면 서비스 대상%s" % warn))
    if h["eff_pct"] is not None:
        tiles.append(("실효 용량 (컨트롤러)", "%.1f%%" % h["eff_pct"],
                      "%s / %s mAh" % (format(h["eff_mah"], ","),
                                       format(h["design_mah"], ","))))
    if h["temp"] is not None:
        tiles.append(("배터리 온도", "%.1f°C" % h["temp"], "실시간 · 안전 10~40°C"))
    if h.get("cpu_temp") is not None:
        tiles.append(("CPU 온도", "%.1f°C" % h["cpu_temp"], "실시간 · 90°C+ 지속 시 문제"))
    if h.get("gpu_temp") is not None:
        tiles.append(("GPU 온도", "%.1f°C" % h["gpu_temp"], "실시간"))
    if h["condition"]:
        ko = {"Normal": "정상"}.get(h["condition"], h["condition"])
        tiles.append(("Apple 진단", ko, "시스템 자체 판정"))
    html = ""
    for title, value, sub in tiles:
        html += ('<div class="card"><div class="ct">%s</div>'
                 '<div class="cv">%s</div><div class="cs">%s</div></div>'
                 % (title, value, sub))
    return html


def build_recs(conn, now, h):
    """데이터 기반 권장 조치. (text, pid, comm) — pid 있으면 종료 버튼."""
    recs = []
    ts10 = [r[0] for r in conn.execute(
        "SELECT ts FROM samples ORDER BY ts DESC LIMIT 10")]
    if len(ts10) >= 5:
        ph = ",".join("?" * len(ts10))
        try:
            in_app = app_guard()
        except Exception:
            in_app = lambda pid: True  # 판정 불가면 안전하게 버튼 없이 안내만
        for pid, comm, cnt, avg in conn.execute(
                "SELECT pid, comm, COUNT(*), AVG(pcpu) FROM proc_samples "
                "WHERE ts IN (%s) AND pcpu >= 50 GROUP BY pid, comm "
                "HAVING COUNT(*) >= 5 ORDER BY AVG(pcpu) DESC LIMIT 5" % ph,
                tuple(ts10)):
            if comm in DENYLIST:
                continue
            cur = ps_one(pid)  # 지금도 같은 프로그램인지 전체 경로로 확인
            if not cur or os.path.basename(cur["exe"]) != comm or is_system(comm, cur["exe"]):
                continue
            if in_app(pid):  # 사용자가 쓰는 앱은 끄자고 하지 않는다
                recs.append(("<b>%s</b>이(가) 최근 10분간 평균 CPU %.0f%%를 쓰고 있습니다. "
                             "쓰고 있는 앱이라 끄지 않습니다 — 필요 없으면 직접 닫으세요."
                             % (_html.escape(comm), avg), None, None))
                continue
            recs.append(("<b>%s</b>이(가) 최근 10분간 평균 CPU %.0f%%를 점유 중입니다. "
                         "직접 돌린 작업이 아니면 종료하세요."
                         % (_html.escape(comm), avg), (pid, cur["exe"]), _html.escape(comm)))
    try:
        boot = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                              capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"sec = (\d+)", boot)
        if m:
            up_days = (now - int(m.group(1))) / 86400.0
            if up_days >= 14:
                recs.append(("업타임 %d일째입니다. 시스템 프로세스(WindowServer 등)에 "
                             "찌꺼기가 쌓입니다 — 재부팅 한 번을 권장합니다." % up_days,
                             None, None))
    except Exception:
        pass
    row = conn.execute(
        "SELECT ts, pct, gap_min FROM samples WHERE gap_min >= 30 AND ts >= ? "
        "ORDER BY ts DESC LIMIT 1", (now - 36 * 3600,)).fetchone()
    if row:
        prev = conn.execute("SELECT pct FROM samples WHERE ts < ? "
                            "ORDER BY ts DESC LIMIT 1", (row[0],)).fetchone()
        if prev and row[2]:
            rate = (prev[0] - row[1]) / (row[2] / 60.0)
            if rate > 2:
                recs.append(("최근 수면 방전이 시간당 %.1f%%로 높았습니다 "
                             "(정상 0.3~0.5%%). 이상 내역에서 범인을 확인하세요." % rate,
                             None, None))
    if h.get("apple_pct") is not None and h["apple_pct"] < 80:
        recs.append(("배터리 최대 용량이 %d%%까지 내려왔습니다 — "
                     "Apple 배터리 서비스 대상입니다." % h["apple_pct"], None, None))
    last = conn.execute("SELECT pct, on_battery FROM samples "
                        "ORDER BY ts DESC LIMIT 1").fetchone()
    if last and last[1] == 1 and last[0] < 20:
        recs.append(("배터리 %d%% — 충전기를 연결하세요." % last[0], None, None))
    if not recs:
        return '<div class="ok">✅ 지금 특별히 할 일이 없습니다 — 상태 양호.</div>'
    out = ""
    for text, target, name in recs:
        btn = ('<button class="kbtn" data-pid="%d" data-exe="%s" data-name="%s">지금 종료</button>'
               % (target[0], _html.escape(target[1], quote=True), name)) if target else ""
        out += '<div class="ano">💡 %s%s</div>' % (text, btn)
    return out


TEMPLATE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>배터리 관리자</title>
<script src="vendor/chart.umd.js"></script>
<script src="vendor/hammer.min.js"></script>
<script src="vendor/chartjs-plugin-zoom.min.js"></script>
<style>
:root { --bg:#f5f5f7; --fg:#1d1d1f; --card:#ffffff; --sub:#6e6e73; --line:#e5e5ea; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1c1c1e; --fg:#f2f2f7; --card:#2c2c2e; --sub:#98989e; --line:#3a3a3c; }
}
* { box-sizing:border-box; margin:0; }
body { background:var(--bg); color:var(--fg);
  font-family:"Apple SD Gothic Neo",-apple-system,sans-serif; padding:24px;
  max-width:1100px; margin:0 auto; }
header { display:flex; align-items:center; justify-content:space-between;
  flex-wrap:wrap; gap:12px; margin-bottom:16px; }
h1 { font-size:22px; }
.meta { color:var(--sub); font-size:12px; }
nav button { border:1px solid var(--line); background:var(--card); color:var(--fg);
  padding:8px 18px; font-size:14px; cursor:pointer; }
nav button:first-child { border-radius:8px 0 0 8px; }
nav button:last-child { border-radius:0 8px 8px 0; }
nav button.on { background:#D64541; color:#fff; border-color:#D64541; }
#verdict { background:var(--card); border-left:4px solid #D64541; border-radius:8px;
  padding:12px 16px; font-size:14px; margin-bottom:16px; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:12px; margin-bottom:16px; }
.card { background:var(--card); border-radius:12px; padding:16px; }
.ct { font-size:12px; color:var(--sub); }
.cv { font-size:24px; font-weight:700; margin:4px 0; }
.cs { font-size:11px; color:var(--sub); }
.card.warm { box-shadow:inset 0 0 0 2px #E8A33D; background:rgba(232,163,61,0.10); }
.card.hot { box-shadow:inset 0 0 0 2px #D64541; background:rgba(214,69,65,0.12); }
.card.hot .cv { color:#D64541; } .card.hot .cs { color:#D64541; font-weight:600; }
section { background:var(--card); border-radius:12px; padding:18px; margin-bottom:16px; }
section h2 { font-size:15px; margin-bottom:12px; }
.row { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
@media (max-width:760px) { .row { grid-template-columns:1fr; } }
.ano { padding:10px 0; border-bottom:1px solid var(--line); font-size:13px; }
.ano:last-child { border-bottom:none; }
.ano .when { color:var(--sub); font-size:11px; }
.ok { color:#2E8B57; font-size:14px; }
.empty { color:var(--sub); font-size:13px; padding:24px 0; text-align:center; }
.tlwrap { height:320px; } .smwrap { height:250px; position:relative; }
table.hog { width:100%; border-collapse:collapse; font-size:13px; }
table.hog th { text-align:left; color:var(--sub); font-weight:500; font-size:11px;
  padding:6px 8px; border-bottom:1px solid var(--line); }
table.hog td { padding:7px 8px; border-bottom:1px solid var(--line); }
table.hog tr:last-child td { border-bottom:none; }
table.hog td.num { text-align:right; font-variant-numeric:tabular-nums; }
.badge { display:inline-block; width:8px; height:8px; border-radius:4px; margin-right:7px; }
#optBtn { display:none; background:#2E8B57; color:#fff; border:none; border-radius:8px;
  padding:8px 16px; font-size:14px; cursor:pointer; font-weight:600; }
#optBtn:hover { filter:brightness(1.1); }
#optBtn:disabled { opacity:.65; cursor:wait; filter:none; }
.kbtn { display:none; background:#D64541; color:#fff; border:none; border-radius:6px;
  padding:3px 10px; font-size:11px; cursor:pointer; margin-left:8px; }
.tlbar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap;
  gap:8px; margin-bottom:10px; }
.chips { display:flex; gap:6px; flex-wrap:wrap; }
.chips button, .zctl button { border:1px solid var(--line); background:var(--card); color:var(--fg);
  border-radius:14px; padding:4px 11px; font-size:12px; cursor:pointer; }
.chips button.on { background:#4A6FA5; color:#fff; border-color:#4A6FA5; }
.zctl { display:flex; gap:4px; align-items:center; }
.zctl button { border-radius:8px; padding:4px 9px; font-variant-numeric:tabular-nums; }
.zctl button:hover, .chips button:hover { filter:brightness(0.95); }
#range { font-size:12px; color:var(--sub); margin-right:6px; font-variant-numeric:tabular-nums; }
.tlwrap canvas { cursor:crosshair; }
.hint { font-size:11px; color:var(--sub); margin-top:8px; }
table.ev { width:100%; border-collapse:collapse; font-size:13px; }
table.ev th { text-align:left; color:var(--sub); font-weight:500; font-size:11px;
  padding:6px 8px; border-bottom:1px solid var(--line); }
table.ev td { padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
table.ev tr:last-child td { border-bottom:none; }
table.ev td.when { white-space:nowrap; font-variant-numeric:tabular-nums; }
table.ev td.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
table.ev .sub { color:var(--sub); font-size:11px; }
.evwrap { max-height:340px; overflow-y:auto; }
</style></head><body>
<header>
  <div><h1>🔋 배터리 관리자</h1><div class="meta">생성: __GENTIME__ · 수집 주기 60초 · 완전 로컬</div></div>
  <div style="display:flex;gap:10px;align-items:center">
    <button id="optBtn">🚀 최적화</button><nav id="tabs"></nav></div>
</header>
<div id="verdict"></div>
<div class="grid">__CARDS__</div>
<section><h2>권장 조치 <span class="meta">(종료 버튼은 앱에서 실행할 때 활성화)</span></h2>
  <div id="recs">__RECS__</div></section>
<section><h2>배터리 타임라인 <span class="meta">(빨강 방전 · 초록 전원 · 회색 점선 수면/미수집 · ● 어댑터 연결/분리 · 노랑 소비 전력 W · 붉은 띠·굵은 선 = 평소보다 가파른 급방전 · 파란 점선 = 지금 속도로 0%까지 · 말풍선 = 하락 구간별 평균 기울기, 회색 보통·호박 빠름·빨강 급방전 · 초록 말풍선 +N%/h = 충전 구간별 평균 충전 속도, 호박 🐢 = 평소보다 느린 충전 · 초록 점선 = 평소 충전 곡선으로 100%까지)</span></h2>
  <div class="tlbar">
    <div class="chips" id="chips"></div>
    <div class="zctl"><span id="range"></span>
      <button id="zPrev" title="왼쪽으로 이동">◀</button><button id="zNext" title="오른쪽으로 이동">▶</button>
      <button id="zOut" title="축소">－</button><button id="zIn" title="확대">＋</button>
      <button id="zReset" title="전체 보기">↺ 전체</button></div>
  </div>
  <div class="tlwrap"><canvas id="timeline"></canvas></div>
  <div class="hint">드래그로 구간 선택 확대 · 트랙패드 핀치 또는 ⌘/Ctrl+휠 확대 · Shift+드래그 이동 · 더블클릭 초기화</div>
  <div class="hint" id="thrInfo"></div>
  <div class="hint" id="chgInfo"></div></section>
<section><h2>전원·수면 이벤트 <span class="meta">(수집기 1분 샘플 + 시스템 전원 로그 — 잠든 사이 어댑터가 뽑힌 시각도 여기서 확인)</span></h2>
  <div class="evwrap" id="events"></div></section>
<section><h2>배터리 건강 <span class="meta">(스냅샷 하루 1회 기록 — 장기 용량 추이가 쌓입니다)</span></h2>
  <div class="grid" style="margin-bottom:0">__HEALTH__</div></section>
<section><h2>이상 탐지 내역</h2><div id="anom"></div></section>
<div class="row">
  <section><h2>전력 도둑 Top 10 <span class="meta">(기간 평균 CPU 순)</span></h2>
    <div id="hogs"></div></section>
  <section><h2>수면 방전 추이 <span class="meta">(시간당 %, 2% 초과는 빨강)</span></h2>
    <div class="smwrap"><canvas id="sleep"></canvas><div id="sleepEmpty" class="empty" style="display:none">
    아직 수면 데이터가 없습니다 — 뚜껑 덮고 잔 첫 밤 이후 표시됩니다.</div></div></section>
</div>
<script>
const DATA = __DATA__;
const RED = "#D64541", GREEN = "#2E8B57", BLUE = "#4A6FA5", AMBER = "#E8A33D";
const dark = matchMedia("(prefers-color-scheme: dark)").matches;
Chart.defaults.color = dark ? "#98989e" : "#6e6e73";
Chart.defaults.borderColor = dark ? "#3a3a3c" : "#e5e5ea";
Chart.defaults.animation.duration = 350;
let charts = {};
function fmt(t, withTime) {
  const d = new Date(t * 1000);
  const hm = String(d.getHours()).padStart(2,"0") + ":" + String(d.getMinutes()).padStart(2,"0");
  return withTime === false ? (d.getMonth()+1)+"/"+d.getDate()
    : (d.getMonth()+1)+"/"+d.getDate()+" "+hm;
}
function destroy(id) { if (charts[id]) { charts[id].destroy(); delete charts[id]; } }
function esc(s) { const d = document.createElement("span"); d.textContent = s; return d.innerHTML; }
function mins(m) { return m >= 90 ? (m/60).toFixed(1) + "시간" : m + "분"; }
const crosshair = { id: "crosshair", afterDraw(chart) {
  const tt = chart.tooltip;
  if (!tt || !tt.getActiveElements().length) return;
  const x = tt.getActiveElements()[0].element.x;
  const { ctx, chartArea } = chart;
  ctx.save();
  ctx.strokeStyle = dark ? "rgba(255,255,255,0.28)" : "rgba(0,0,0,0.22)";
  ctx.lineWidth = 1;
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(x, chartArea.top);
  ctx.lineTo(x, chartArea.bottom);
  ctx.stroke();
  ctx.restore();
} };
if (window.ChartZoom) Chart.register(window.ChartZoom);
// 차트 위 글자 라벨끼리 겹치지 않게: 그린 라벨 상자를 모아 두고 새 라벨은 겹치면 생략
const labelBoxes = { id: "labelBoxes", beforeDraw(chart) { chart.$boxes = []; } };
function boxHit(chart, b) {
  return (chart.$boxes || []).some(o => b.x < o.x + o.w && b.x + b.w > o.x && b.y < o.y + o.h && b.y + b.h > o.y); }
function addBox(chart, b) { (chart.$boxes = chart.$boxes || []).push(b); }
const GRAY = dark ? "#8e8e93" : "#a1a1a6";
const STEEP = dark ? "#FF453A" : "#A61E1E";
const CHG = dark ? "#30D158" : "#1E7A46";  // 충전 기울기 (전원 선 초록보다 진하게)
// 미래(예측) 여백: 0% 예상 시각까지, 단 보이는 폭의 20% 이하
function futurePad(d, span) {
  const until = d.proj ? d.proj.empty : (d.cproj ? d.cproj.full : null);
  return until ? Math.max(0, Math.min(until - d.end, span * 0.2)) : 0; }
const FONT = "'Apple SD Gothic Neo',-apple-system,sans-serif";
function fmtHM(t) { const d = new Date(t * 1000);
  return String(d.getHours()).padStart(2,"0") + ":" + String(d.getMinutes()).padStart(2,"0"); }
function fmtMD(t) { const d = new Date(t * 1000); return (d.getMonth()+1) + "/" + d.getDate(); }
function sameDay(a, b) { const x = new Date(a*1000), y = new Date(b*1000);
  return x.getFullYear() === y.getFullYear() && x.getMonth() === y.getMonth() && x.getDate() === y.getDate(); }
function fmtDur(sec) { const m = Math.round(sec / 60);
  if (m < 60) return m + "분";
  const h = Math.floor(m / 60), r = m % 60;
  return r ? h + "시간 " + r + "분" : h + "시간"; }
function sleepReason(r) {
  if (!r) return "";
  if (/Clamshell/.test(r)) return "덮개 닫음";
  if (/Idle/.test(r)) return "유휴 잠자기";
  if (/Low Power/.test(r)) return "배터리 소진(저전력 수면)";
  if (/Software/.test(r)) return "잠자기 메뉴";
  if (/Maintenance|Sleep Service/.test(r)) return "유지보수 후 재수면";
  return r; }
function wakeReason(r) {
  if (!r) return "기록 없음";
  if (/USB-C_plug/.test(r)) return "어댑터 연결";
  if (/\blid\b/.test(r)) return "덮개 열림";
  if (/pwrbtn/.test(r)) return "전원 버튼";
  if (/HID|UserActivity/.test(r)) return "키보드·트랙패드";
  if (/BatteryStateChange/.test(r)) return "배터리 상태 변화";
  if (/Hibernate/.test(r)) return "최대 절전에서 복귀";
  if (/Notification/.test(r)) return "알림";
  if (/rtc|SleepService|Maintenance/.test(r)) return "예약 작업";
  if (/wifibt|bluetooth/.test(r)) return "네트워크·블루투스";
  return r.split(" ")[0]; }
// ── 시간축 눈금: 시계 경계(정각·자정)에 맞춘 10개 이하 ──
const STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800];
function niceTicks(min, max) {
  const step = STEPS.find(s => (max - min) / s <= 10) || 172800;
  const off = new Date(min * 1000).getTimezoneOffset() * 60;
  const out = [];
  for (let v = Math.ceil((min - off) / step) * step + off; v <= max; v += step) out.push(v);
  return out;
}
function tickLabel(v, i, ticks) {
  const step = ticks.length > 1 ? ticks[1].value - ticks[0].value : 3600;
  const d = new Date(v * 1000);
  if (step >= 86400) return fmtMD(v);
  const midnight = d.getHours() === 0 && d.getMinutes() === 0;
  return (midnight || i === 0) ? fmtMD(v) + " " + fmtHM(v) : fmtHM(v);
}
// ── 배경: 전원 연결(초록) + 수면(회색 💤) ──
function makeShade(d) {
  const s = d.series;
  return { id: "shade", beforeDatasetsDraw(chart) {
    const {ctx, chartArea: ca, scales: {x}} = chart;
    ctx.save();
    ctx.beginPath(); ctx.rect(ca.left, ca.top, ca.right - ca.left, ca.bottom - ca.top); ctx.clip();
    ctx.fillStyle = dark ? "rgba(46,139,87,0.16)" : "rgba(46,139,87,0.10)";
    // 구간 [i-1, i] 는 양 끝이 모두 전원이거나, 전환 시각이 확실(90초 이내)할 때만 초록.
    // 잠든 사이 언제 꽂혔는지 모르는 긴 구간을 통째로 초록칠하지 않기 위함.
    const ac = i => !s[i].b && (!s[i - 1].b || s[i].t - s[i - 1].t <= 90);
    for (let i = 1; i < s.length; i++) {
      if (!ac(i)) continue;
      let j = i;
      while (j + 1 < s.length && ac(j + 1)) j++;
      const x0 = x.getPixelForValue(s[i - 1].t), x1 = x.getPixelForValue(s[j].t);
      if (x1 >= ca.left && x0 <= ca.right)
        ctx.fillRect(x0, ca.top, Math.max(x1 - x0, 1), ca.bottom - ca.top);
      i = j;
    }
    const band = dark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.07)";
    const txt = dark ? "rgba(255,255,255,0.5)" : "rgba(0,0,0,0.45)";
    ctx.font = "11px " + FONT; ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (const iv of d.sleep_iv) {
      const x0 = x.getPixelForValue(iv.t0), x1 = x.getPixelForValue(iv.t1);
      if (x1 < ca.left || x0 > ca.right) continue;
      ctx.fillStyle = band;
      ctx.fillRect(x0, ca.top, Math.max(x1 - x0, 1), ca.bottom - ca.top);
      if (x1 - x0 > 56) {
        ctx.fillStyle = txt;
        const t = "💤 " + fmtDur(iv.t1 - iv.t0), tw = ctx.measureText(t).width;
        ctx.fillText(t, (x0 + x1) / 2, ca.top + 4);
        addBox(chart, { x: (x0 + x1) / 2 - tw / 2, y: ca.top + 2, w: tw, h: 16 });
      }
    }
    // 급방전 구간: 붉은 띠 + 최고 속도
    for (const iv of d.steep) {
      const x0 = x.getPixelForValue(iv.t0), x1 = x.getPixelForValue(iv.t1);
      if (x1 < ca.left || x0 > ca.right) continue;
      ctx.fillStyle = dark ? "rgba(255,69,58,0.16)" : "rgba(214,69,65,0.12)";
      ctx.fillRect(x0, ca.top, Math.max(x1 - x0, 2), ca.bottom - ca.top);
      const ratio = d.base ? " · 평소 " + (iv.peak / d.base).toFixed(1) + "배" : "";
      const full = "⚡ " + iv.peak + "%/h" + ratio;
      ctx.fillStyle = STEEP; ctx.font = "bold 11px " + FONT;
      const w = ctx.measureText(full).width;
      if (x1 - x0 > w + 8) { ctx.fillText(full, (x0 + x1) / 2, ca.top + 4);
        addBox(chart, { x: (x0 + x1) / 2 - w / 2, y: ca.top + 2, w: w, h: 16 }); }
      else if (x1 - x0 > 14) ctx.fillText("⚡", (x0 + x1) / 2, ca.top + 4);
      ctx.font = "11px " + FONT;
    }
    // 지금 이후(예측) 영역
    const xn = x.getPixelForValue(d.end);
    if (xn < ca.right) {
      ctx.fillStyle = dark ? "rgba(74,111,165,0.10)" : "rgba(74,111,165,0.06)";
      ctx.fillRect(xn, ca.top, ca.right - xn, ca.bottom - ca.top);
      ctx.strokeStyle = dark ? "rgba(255,255,255,0.35)" : "rgba(0,0,0,0.3)";
      ctx.setLineDash([2, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(xn, ca.top); ctx.lineTo(xn, ca.bottom); ctx.stroke();
      ctx.setLineDash([]);
      if (ca.right - xn > 30) { ctx.fillStyle = txt; ctx.fillText("지금", xn + 16, ca.top + 20); }
    }
    ctx.restore();
  } };
}
// ── 어댑터 연결/분리 마커 ──
function makeMarkers(s) {
  return { id: "markers", afterDatasetsDraw(chart) {
    const {ctx, chartArea: ca, scales: {x, y}} = chart;
    ctx.save();
    ctx.font = "bold 10px " + FONT; ctx.textAlign = "center"; ctx.textBaseline = "alphabetic";
    let lastLx = -1e9;
    for (const p of s) {
      if (!p.ev) continue;
      const px = x.getPixelForValue(p.t), py = y.getPixelForValue(p.pct);
      if (px < ca.left || px > ca.right) continue;
      const col = p.ev.kind === "plug" ? GREEN : RED;
      ctx.beginPath(); ctx.arc(px, py, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = col; ctx.fill();
      ctx.lineWidth = 1.5; ctx.strokeStyle = dark ? "#2c2c2e" : "#fff"; ctx.stroke();
      if (px - lastLx > 60) {
        const label = (p.ev.kind === "plug" ? "연결 " : "분리 ") + (p.ev.exact ? "" : "~") + fmtHM(p.t);
        ctx.fillStyle = col;
        const lw = ctx.measureText(label).width;
        let ly = py < ca.top + 28 ? py + 17 : py - 10;
        if (boxHit(chart, { x: px - lw / 2, y: ly - 11, w: lw, h: 13 })) ly = py + 17;  // 위 라벨과 겹치면 점 아래로
        ctx.fillText(label, px, ly);
        addBox(chart, { x: px - lw / 2, y: ly - 11, w: lw, h: 13 });
        lastLx = px;
      }
    }
    ctx.restore();
  } };
}
// ── 현재 추세선(직전 30분 회귀) + 0% 예측 점선 ──
function makeProj(d) {
  return { id: "proj", afterDatasetsDraw(chart) {
    const p = d.proj; if (!p) return;
    const {ctx, chartArea: ca, scales: {x, y}} = chart;
    const col = p.rate > d.thr ? STEEP : BLUE;
    ctx.save();
    ctx.beginPath(); ctx.rect(ca.left, ca.top, ca.right - ca.left, ca.bottom - ca.top); ctx.clip();
    const X0 = x.getPixelForValue(p.t0), Y0 = y.getPixelForValue(p.p0);
    const X1 = x.getPixelForValue(p.t1), Y1 = y.getPixelForValue(p.p1);
    const XE = x.getPixelForValue(p.empty), YE = y.getPixelForValue(0);
    ctx.strokeStyle = col; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.moveTo(X0, Y0); ctx.lineTo(X1, Y1); ctx.stroke();
    ctx.setLineDash([6, 5]); ctx.lineWidth = 1.8;
    ctx.beginPath(); ctx.moveTo(X1, Y1); ctx.lineTo(XE, YE); ctx.stroke();
    ctx.setLineDash([]);
    if (XE <= ca.right) {
      ctx.beginPath(); ctx.arc(XE, YE - 1, 4, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill();
    }
    if (X1 < ca.left || X1 > ca.right) { ctx.restore(); return; }  // 지금이 화면 밖이면 라벨 생략
    const when = sameDay(p.empty, d.end) ? fmtHM(p.empty) : fmt(p.empty);
    const label = "추세 " + p.rate + "%/h · " + when + " 0% 예상";
    ctx.font = "bold 11px " + FONT; ctx.textBaseline = "bottom";
    const w = ctx.measureText(label).width;
    const bx = Math.min(Math.max(X1 - w / 2, ca.left + 2), ca.right - w - 2);
    const by = Y1 - 12 < ca.top + 30 ? Y1 + 26 : Y1 - 12;
    ctx.fillStyle = dark ? "rgba(28,28,30,0.85)" : "rgba(255,255,255,0.88)";
    ctx.fillRect(bx - 4, by - 15, w + 8, 18);
    ctx.fillStyle = col; ctx.textAlign = "left"; ctx.fillText(label, bx, by);
    addBox(chart, { x: bx - 4, y: by - 15, w: w + 8, h: 18 });
    ctx.restore();
  } };
}
// ── 현재 충전 추세선(직전 10분 회귀) + 평소 충전 곡선을 따른 100% 예상 경로 ──
function makeCProj(d) {
  return { id: "cproj", afterDatasetsDraw(chart) {
    const p = d.cproj; if (!p) return;
    const {ctx, chartArea: ca, scales: {x, y}} = chart;
    const col = p.slow ? AMBER : CHG;
    ctx.save();
    ctx.beginPath(); ctx.rect(ca.left, ca.top, ca.right - ca.left, ca.bottom - ca.top); ctx.clip();
    const X0 = x.getPixelForValue(p.t0), Y0 = y.getPixelForValue(p.p0);
    const X1 = x.getPixelForValue(p.t1), Y1 = y.getPixelForValue(p.p1);
    ctx.strokeStyle = col; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.moveTo(X0, Y0); ctx.lineTo(X1, Y1); ctx.stroke();
    ctx.setLineDash([6, 5]); ctx.lineWidth = 1.8;
    ctx.beginPath(); ctx.moveTo(X1, Y1);
    for (const [t, q] of p.path.slice(1)) ctx.lineTo(x.getPixelForValue(t), y.getPixelForValue(q));
    ctx.stroke(); ctx.setLineDash([]);
    const XF = x.getPixelForValue(p.full), YF = y.getPixelForValue(100);
    if (XF <= ca.right) { ctx.beginPath(); ctx.arc(XF, YF + 1, 4, 0, Math.PI * 2); ctx.fillStyle = col; ctx.fill(); }
    if (X1 < ca.left || X1 > ca.right) { ctx.restore(); return; }
    const when = sameDay(p.full, d.end) ? fmtHM(p.full) : fmt(p.full);
    const label = (p.slow ? "🐢 " : "") + "충전 +" + p.rate + "%/h · " + when + " 완충 예상";
    ctx.font = "bold 11px " + FONT; ctx.textBaseline = "bottom"; ctx.textAlign = "left";
    const w = ctx.measureText(label).width;
    // 오르는 선이라 지금 점의 왼쪽 위가 비어 있다. 위가 모자라면 오른쪽 아래로
    let bx = X1 - w - 10, by = Y1 - 10;
    if (by - 15 < ca.top + 2) { bx = X1 + 10; by = Y1 + 24; }
    bx = Math.min(Math.max(bx, ca.left + 2), ca.right - w - 2);
    ctx.fillStyle = dark ? "rgba(28,28,30,0.85)" : "rgba(255,255,255,0.88)";
    ctx.fillRect(bx - 4, by - 15, w + 8, 18);
    ctx.fillStyle = col; ctx.fillText(label, bx, by);
    addBox(chart, { x: bx - 4, y: by - 15, w: w + 8, h: 18 });
    ctx.restore();
  } };
}
// ── 하락 구간마다 기울기 라벨 (보이는 부분 가운데, 선 위) ──
function pctAt(s, t) {
  let lo = 0, hi = s.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (s[m].t < t) lo = m + 1; else hi = m; }
  const b = s[lo], a = s[Math.max(lo - 1, 0)];
  if (!a || b.t === a.t) return b.pct;
  return a.pct + (b.pct - a.pct) * (t - a.t) / (b.t - a.t);
}
function makeRunLabels(d) {
  const s = d.series;
  return { id: "runLabels", afterDatasetsDraw(chart) {
    const {ctx, chartArea: ca, scales: {x, y}} = chart;
    const items = [];
    const add = (r, chg) => {
      const vx0 = Math.max(x.getPixelForValue(r.t0), ca.left), vx1 = Math.min(x.getPixelForValue(r.t1), ca.right);
      if (vx1 - vx0 < 5) return;  // 화면에서 너무 좁으면 생략 (확대하면 나타남)
      const lvl = chg ? (r.slow ? 1 : 0) : (r.rate > d.thr ? 2 : (d.base && r.rate > d.base * 1.2 ? 1 : 0));
      items.push({ r, vx0, vx1, lvl, chg });
    };
    for (const r of d.runs) add(r, false);
    for (const r of d.cruns || []) add(r, true);
    // 급방전 먼저, 그다음 화면에서 긴 구간 먼저 자리를 잡는다
    items.sort((a, b) => (b.lvl - a.lvl) || ((b.vx1 - b.vx0) - (a.vx1 - a.vx0)));
    ctx.save();
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    for (const it of items) {
      const r = it.r, text = it.chg ? (r.slow ? "🐢 +" : "+") + r.rate + "%/h" : r.rate + "%/h";
      ctx.font = (it.lvl || it.chg ? "bold " : "") + "11px " + FONT;
      const w = ctx.measureText(text).width + 10, h = 17;
      const cx = (it.vx0 + it.vx1) / 2, py = y.getPixelForValue(pctAt(s, x.getValueForPixel(cx)));
      let bx = cx - w / 2, by = py - h - 7;
      if (it.chg) {  // 오르는 선: 가운데 점의 왼쪽 위가 비어 있고, 위가 모자라면 오른쪽 아래
        bx = cx - w - 4; by = py - h - 5;
        if (by < ca.top + 2) { bx = cx + 4; by = py + 5; }
      } else if (by < ca.top + 2) by = py + 7;
      const box = { x: Math.min(Math.max(bx, ca.left), ca.right - w), y: by, w, h };
      if (boxHit(chart, box)) continue;
      const col = it.chg ? (it.lvl ? AMBER : CHG)
        : (it.lvl === 2 ? STEEP : (it.lvl === 1 ? AMBER : (dark ? "#c7c7cc" : "#55555a")));
      ctx.fillStyle = dark ? "rgba(28,28,30,0.88)" : "rgba(255,255,255,0.92)";
      ctx.strokeStyle = col; ctx.lineWidth = 1;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(box.x, box.y, box.w, box.h, 8); else ctx.rect(box.x, box.y, box.w, box.h);
      ctx.fill(); ctx.stroke();
      ctx.fillStyle = col; ctx.fillText(text, box.x + 5, box.y + h / 2 + 0.5);
      addBox(chart, box);
    }
    ctx.restore();
  } };
}
// ── 확대/이동 컨트롤 ──
const CHIPS = [["1시간", 3600], ["3시간", 10800], ["6시간", 21600], ["12시간", 43200], ["전체", 0]];
let curKey = "p24h";
function rangeChanged() {
  const ch = charts.timeline; if (!ch) return;
  const d = DATA[curKey], x = ch.scales.x, mn = x.min, mx = x.max;
  document.getElementById("range").textContent =
    fmt(mn) + " ~ " + (sameDay(mn, mx) ? fmtHM(mx) : fmt(mx)) + " (" + fmtDur(mx - mn) + ")";
  document.querySelectorAll("#chips button").forEach(b => {
    const span = parseInt(b.dataset.span);
    const on = span ? (Math.abs(mn - (d.end - span)) < 30 && Math.abs(mx - (d.end + futurePad(d, span))) < 30)
                    : (Math.abs(mn - d.start) < 30 && Math.abs(mx - d.xmax) < 30);
    b.classList.toggle("on", on);
  });
}
function zoomSpan(span) {
  const ch = charts.timeline, d = DATA[curKey]; if (!ch) return;
  if (!span) ch.resetZoom("default");
  else ch.zoomScale("x", { min: Math.max(d.end - span, d.start), max: d.end + futurePad(d, span) }, "default");
  rangeChanged();
}
{
  const chips = document.getElementById("chips");
  for (const [label, span] of CHIPS) {
    const b = document.createElement("button");
    b.textContent = span ? "최근 " + label : label; b.dataset.span = span;
    b.onclick = () => zoomSpan(span);
    chips.appendChild(b);
  }
  const panBy = dir => { const ch = charts.timeline; if (!ch) return;
    ch.pan({ x: dir * (ch.chartArea.right - ch.chartArea.left) / 4 }, undefined, "default"); rangeChanged(); };
  document.getElementById("zPrev").onclick = () => panBy(1);
  document.getElementById("zNext").onclick = () => panBy(-1);
  document.getElementById("zIn").onclick = () => { charts.timeline && charts.timeline.zoom(1.6); rangeChanged(); };
  document.getElementById("zOut").onclick = () => { charts.timeline && charts.timeline.zoom(0.625); rangeChanged(); };
  document.getElementById("zReset").onclick = () => zoomSpan(0);
  const cv = document.getElementById("timeline");
  cv.addEventListener("dblclick", () => zoomSpan(0));
  // Safari/WKWebView 트랙패드 핀치 (gesture 이벤트) -> 커서 기준 확대
  let gs = 1;
  cv.addEventListener("gesturestart", e => { e.preventDefault(); gs = 1; });
  cv.addEventListener("gesturechange", e => {
    e.preventDefault();
    const ch = charts.timeline; if (!ch || !e.scale) return;
    const f = e.scale / gs; gs = e.scale;
    const r = cv.getBoundingClientRect();
    ch.zoom({ x: f, focalPoint: { x: e.clientX - r.left, y: e.clientY - r.top } }, "none");
    rangeChanged();
  });
  cv.addEventListener("gestureend", e => e.preventDefault());
}
function renderEvents(d) {
  const el = document.getElementById("events");
  if (!d.events.length) {
    el.innerHTML = '<div class="empty">이 기간에 기록된 전원·수면 이벤트가 없습니다.</div>';
    return;
  }
  el.innerHTML = '<table class="ev"><tr><th>시각</th><th>이벤트</th>' +
    '<th style="text-align:right">배터리</th><th>출처 · 비고</th></tr>' +
    d.events.map(e => {
      if (e.kind === "sleep") {
        const dur = e.t1 - e.t, drop = (e.pct1 == null) ? null : e.pct - e.pct1;
        const rate = (drop != null && dur >= 1200) ? (drop / (dur / 3600)).toFixed(1) : null;
        const rateTxt = rate == null ? '' : (rate > 0 ? ' · 시간당 ' + rate + '% 감소' + (rate > 2 ? ' ⚠️' : '')
          : rate < 0 ? ' · 시간당 ' + (-rate) + '% 충전' : ' · 변화 없음');
        return '<tr><td class="when">' + fmt(e.t) + ' → ' + (sameDay(e.t, e.t1) ? fmtHM(e.t1) : fmt(e.t1)) +
          '</td><td>💤 수면 ' + fmtDur(dur) + '<div class="sub">' + esc(sleepReason(e.reason)) +
          ' → 깨어남: ' + esc(wakeReason(e.wake)) + '</div></td><td class="num">' + e.pct + '% → ' +
          (e.pct1 == null ? '?' : e.pct1 + '%') + '</td><td class="sub">시스템 로그' +
          (rate != null ? ' · 시간당 ' + rate + '% 감소' + (rate > 2 ? ' ⚠️' : '') : '') + '</td></tr>';
      }
      const name = e.kind === "plug" ? "🔌 어댑터 연결" : "🔌 어댑터 분리";
      const sub = e.exact ? '' : '<div class="sub">정확한 시각 미상 — ' + fmt(e.since) + ' ~ ' +
        (sameDay(e.since, e.t) ? fmtHM(e.t) : fmt(e.t)) + ' 사이</div>';
      return '<tr><td class="when">' + (e.exact ? '' : '~') + fmt(e.t) + '</td><td>' + name + sub +
        '</td><td class="num">' + e.pct + '%</td><td class="sub">' +
        (e.src ? '시스템 로그' : '수집기 (±1분)') + '</td></tr>';
    }).join("") + '</table>';
}
function show(key) {
  curKey = key;
  document.querySelectorAll("#tabs button").forEach(b =>
    b.classList.toggle("on", b.dataset.k === key));
  const d = DATA[key], s = d.series;
  document.getElementById("verdict").textContent = d.label + ": " + d.verdict;
  destroy("timeline");
  const hasW = s.some(x => x.w);
  const quiet = c => s[c.p0DataIndex].asl || s[c.p1DataIndex].gap;
  const steepSeg = c => s[c.p1DataIndex].st && s[c.p1DataIndex].b;
  d.xmax = d.end + futurePad(d, d.end - d.start);
  document.getElementById("thrInfo").textContent = "급방전 기준: 직전 30분 추세가 시간당 " + d.thr + "% 초과로 10분 이상" +
    (d.base ? " (최근 2주 평소 " + d.base + "%의 1.5배, 하한 15%)" : " (평소 데이터 부족 — 고정 기준)");
  document.getElementById("chgInfo").textContent = d.cprof || "";
  const dsets = [{
    data: s.map(x => ({ x: x.t, y: x.pct })), yAxisID: "y", pointRadius: 0, borderWidth: 2,
    cubicInterpolationMode: "monotone", fill: false,
    pointHoverRadius: 4, pointHitRadius: 10, pointHoverBackgroundColor: "#fff",
    segment: {
      borderColor: c => quiet(c) ? GRAY : (steepSeg(c) ? STEEP : (s[c.p1DataIndex].b ? RED : GREEN)),
      borderDash: c => s[c.p0DataIndex].asl ? [5,4] : (s[c.p1DataIndex].gap ? [2,4] : undefined),
      borderWidth: c => quiet(c) ? 1.5 : (steepSeg(c) ? 3.5 : 2) } }];
  if (hasW) dsets.push({
    data: s.map(x => ({ x: x.t, y: x.w == null ? null : x.w })), yAxisID: "yW",
    pointRadius: 0, borderWidth: 1.2, borderColor: AMBER, cubicInterpolationMode: "monotone",
    fill: false, pointHoverRadius: 3, spanGaps: 300 });
  charts.timeline = new Chart(document.getElementById("timeline"), {
    type: "line",
    plugins: [labelBoxes, makeShade(d), makeMarkers(s), makeProj(d), makeCProj(d), makeRunLabels(d), crosshair],
    data: { datasets: dsets },
    options: { maintainAspectRatio: false, parsing: false, normalized: true,
      interaction: { mode: "index", intersect: false, axis: "x" },
      plugins: { legend: { display: false },
        tooltip: {
          padding: 10, cornerRadius: 8,
          filter: item => item.datasetIndex === 0 || s[item.dataIndex].w != null,
          callbacks: {
            title: items => { const p = s[items[0].dataIndex];
              return fmt(p.t) + (p.src ? " · 시스템 로그" : ""); },
            label: c => { const p = s[c.dataIndex];
              if (c.datasetIndex === 1) return "소비 전력 " + p.w + "W";
              let l = p.pct + "%" + (p.b ? " · 배터리" : " · 전원");
              if (p.ct) l += " · CPU " + p.ct + "°C";
              return l; },
            afterBody: items => { const p = s[items[0].dataIndex], out = [];
              if (p.ev) out.push((p.ev.kind === "plug" ? "🔌 어댑터 연결" : "🔌 어댑터 분리") +
                (p.ev.exact ? " " + fmtHM(p.t) : " (" + fmtHM(p.ev.since) + " ~ " + fmtHM(p.t) + " 사이)"));
              if (p.asl) out.push("💤 수면 중" + (p.k === "darkwake" ? " (잠깐 깸)" : p.k === "sleep" ? " (잠들기)" : ""));
              if (p.gap) out.push("⚠️ 이전 관측 " + fmtDur(p.gap) + " 전 — 미수집 구간");
              if (p.b && p.r != null) out.push((p.st ? "⚡ 급방전 · " : "📉 ") + "30분 추세 시간당 " + p.r + "%" +
                (d.base ? " (평소의 " + (p.r / d.base).toFixed(1) + "배)" : ""));
              if (!p.b && p.cr != null) out.push("📈 충전 10분 추세 시간당 +" + p.cr + "%");
              return out; } } },
        zoom: {
          zoom: { wheel: { enabled: true, speed: 0.15 }, pinch: { enabled: true },
            drag: { enabled: true, threshold: 6, backgroundColor: "rgba(74,111,165,0.18)",
              borderColor: BLUE, borderWidth: 1 },
            mode: "x",
            // 휠은 ⌘/Ctrl/⌥ 를 같이 눌러야 확대 — 그냥 스크롤은 페이지 스크롤 유지
            onZoomStart: ({event}) => !(event.type === "wheel"
              && !(event.ctrlKey || event.metaKey || event.altKey)),
            onZoomComplete: rangeChanged },
          pan: { enabled: true, mode: "x", modifierKey: "shift", onPanComplete: rangeChanged },
          limits: { x: { min: "original", max: "original", minRange: 600 } } } },
      scales: { y: { min: 0, max: 100, ticks: { callback: v => v + "%" } },
        yW: { display: hasW, position: "right", min: 0, suggestedMax: 40,
          grid: { drawOnChartArea: false }, ticks: { callback: v => v + "W" } },
        x: { type: "linear", min: d.start, max: d.xmax,
          afterBuildTicks: sc => { sc.ticks = niceTicks(sc.min, sc.max).map(v => ({ value: v })); },
          ticks: { autoSkip: false, maxRotation: 0, callback: tickLabel } } } }
  });
  rangeChanged();
  renderEvents(d);
  const an = document.getElementById("anom");
  an.innerHTML = d.anomalies.length === 0
    ? '<div class="ok">✅ 이 기간에 감지된 이상 없음</div>'
    : d.anomalies.map(a =>
        '<div class="ano"><b>' + esc(a.label) + '</b> <span class="when">' + fmt(a.t) +
        '</span><br>' + esc(a.detail) + '</div>').join("");
  const hog = document.getElementById("hogs");
  if (d.top.length === 0) {
    hog.innerHTML = '<div class="empty">수집된 프로세스 데이터가 없습니다.</div>';
  } else {
    hog.innerHTML = '<table class="hog"><tr><th>프로세스</th>' +
      '<th style="text-align:right">평균 CPU</th><th style="text-align:right">최대</th>' +
      '<th style="text-align:right">관측</th></tr>' +
      d.top.map(x => {
        const c = x.avg >= 50 ? RED : (x.avg >= 20 ? AMBER : GREEN);
        return '<tr><td><span class="badge" style="background:' + c + '"></span>' +
          esc(x.comm) + '</td><td class="num">' + x.avg + '%</td><td class="num">' +
          x.peak + '%</td><td class="num">' + mins(x.mins) + '</td></tr>';
      }).join("") + '</table>';
  }
  destroy("sleep");
  const se = document.getElementById("sleepEmpty"),
        sc = document.getElementById("sleep");
  if (d.sleeps.length === 0) {
    se.style.display = "block"; sc.style.display = "none";
  } else {
    se.style.display = "none"; sc.style.display = "block";
    charts.sleep = new Chart(sc, {
      type: "bar",
      data: { labels: d.sleeps.map(x => fmt(x.t, false) + " 기상"),
        datasets: [{ data: d.sleeps.map(x => x.rate),
          backgroundColor: d.sleeps.map(x => x.rate > 2 ? RED : BLUE) }] },
      options: { maintainAspectRatio: false,
        plugins: { legend: { display: false }, tooltip: { callbacks: {
          label: c => { const x = d.sleeps[c.dataIndex];
            return "수면 " + x.gap_h + "시간 · " + x.drop + "% 감소 · 시간당 " + x.rate + "%"; } } } },
        scales: { y: { ticks: { callback: v => v + "%" } } } }
    });
  }
}
const tabs = document.getElementById("tabs");
for (const [k, v] of Object.entries(DATA)) {
  const b = document.createElement("button");
  b.textContent = v.label; b.dataset.k = k; b.onclick = () => show(k);
  tabs.appendChild(b);
}
const NATIVE = !!(window.webkit && window.webkit.messageHandlers
  && window.webkit.messageHandlers.battman);
function native(m) { if (NATIVE) window.webkit.messageHandlers.battman.postMessage(m); }
if (NATIVE) {
  const ob = document.getElementById("optBtn");
  ob.style.display = "inline-block";
  ob.onclick = () => {
    ob.disabled = true; ob.textContent = "⏳ 분석 중…";
    native({ action: "optimize" });
  };
  window.optReset = () => { ob.disabled = false; ob.textContent = "🚀 최적화"; };
  document.querySelectorAll(".kbtn").forEach(b => {
    b.style.display = "inline-block";
    b.onclick = () => native({ action: "kill", pid: parseInt(b.dataset.pid),
                               exe: b.dataset.exe, name: b.dataset.name });
  });
}
show("p24h");
</script></body></html>
"""


def profile_text(prof):
    """평소 충전 곡선 한 줄 설명."""
    if not prof:
        return "평소 충전 곡선: 아직 충전 기록이 부족합니다 (최근 30일)."
    parts = ["%d%%대 %.0f" % (k, v) for k, v in sorted(prof.items())]
    return ("평소 충전 속도 (최근 30일, %/h): " + " · ".join(parts)
            + " — 충전 구간이 같은 % 범위 평소의 " + "%.0f%%" % (slope.CHG_SLOW * 100)
            + " 미만이면 느린 충전(호박), 90% 이상 마무리 충전은 판정 제외")


def main():
    now = int(time.time())
    if not os.path.exists(DB):
        sys.stderr.write("battman.db 없음 — collector 가 아직 실행되지 않았습니다.\n")
        sys.exit(1)
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True, timeout=30)
    try:
        base = slope.baseline(conn, now)  # 최근 2주 평소 기울기 — 한 번만 계산
        prof = slope.charge_profile(conn, now)  # 최근 30일 평소 충전 곡선 — 한 번만 계산
        data = build_data(conn, now, base, prof)
        for v in data.values():
            v["cprof"] = profile_text(prof)
        h = get_health(conn)
        cards = build_cards(conn, now, h, base, prof)
        health = build_health_html(h)
        recs = build_recs(conn, now, h)
    finally:
        conn.close()
    html = (TEMPLATE
            .replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__CARDS__", cards)
            .replace("__HEALTH__", health)
            .replace("__RECS__", recs)
            .replace("__GENTIME__", time.strftime("%Y-%m-%d %H:%M", time.localtime(now))))
    with open(OUT, "w") as f:
        f.write(html)
    if not os.environ.get("BATTMAN_NO_OPEN"):  # 네이티브 앱은 WKWebView 로 직접 띄움
        subprocess.run([OPEN, OUT])
    return 0


if __name__ == "__main__":
    sys.exit(main())
