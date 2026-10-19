#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battman 방전·충전 기울기 공용 계산. rules(급방전 판정)와 report(추세선·급방전 띠·카드)가 같이 쓴다.

기울기 = 직전 WINDOW초 안의 연속(샘플 간격 MAX_GAP 이하) 배터리 샘플에 최소제곱 직선을 맞춘 값.
pct 가 정수라 10분 창에서는 1% 단위로 뚝뚝 끊기므로 끝점 차이 대신 회귀를 쓰고 창을 30분으로 둔다.
기준(threshold) = 최근 BASE_DAYS 일 창별 기울기의 중앙값 × FACTOR (하한 FLOOR). 이력이 없으면 FALLBACK."""
import statistics

WINDOW = 1800         # 추세 창(초)
MIN_SPAN = 1200       # 창 안 최소 관찰 시간(초)
MIN_SAMPLES = 15      # 창 안 최소 샘플 수
MAX_GAP = 180         # 이보다 벌어지면 연속이 아님(수면·미수집)
BASE_DAYS = 14        # 평소 기울기 산출 기간
BASE_MIN_POINTS = 60  # 평소 기울기를 믿으려면 유효 창이 이만큼은 있어야
FACTOR = 1.5          # 평소의 몇 배부터 급방전 (실데이터 9/26~29 TouchEn 폭주 3건을 잡는 값)
FLOOR = 15.0          # 기준 하한 %/h (평소가 아주 낮아도 이 밑으론 안 내림)
FALLBACK = 30.0       # 이력 없을 때 고정 기준 %/h
MIN_PROJ_RATE = 0.5   # 이보다 느리면 0% 예측 안 함
RUN_MIN_SEC = 600     # 하락 구간 라벨: 최소 길이(초)
RUN_MIN_DROP = 2      # 하락 구간 라벨: 최소 하락(%p)

# 충전: 방전보다 3~5배 빠르고 80%를 넘으면 느려지므로(정전압 구간) 창을 10분으로 짧게 둔다.
# 직선 하나로 100%를 예측하면 너무 이르므로, 이 맥·이 충전기의 평소 충전 곡선(10% 구간별 속도)을 따라간다.
CHG_WINDOW = 600         # 충전 추세 창(초)
CHG_MIN_SPAN = 360       # 창 안 최소 관찰 시간(초)
CHG_MIN_SAMPLES = 6      # 창 안 최소 샘플 수
CHG_PROFILE_DAYS = 30    # 평소 충전 곡선 산출 기간
CHG_BAND_MIN_SEC = 600   # 10% 구간 평균을 믿으려면 그 구간 충전을 이만큼은 봤어야
CHG_SLOW = 0.6           # 평소 같은 % 범위 속도의 이 배수 미만이면 느린 충전
CHG_SLOW_BELOW = 90      # 90% 이상 마무리 충전은 원래 들쭉날쭉해서 느린 충전 판정에서 뺀다
CHG_SCALE = (0.25, 1.5)  # 지금 속도/평소 비율을 남은 구간에 적용할 때 허용 범위
CHG_TARGET = 100
CHG_RUN_MIN_SEC = 300    # 충전 구간 라벨: 최소 길이(초)
CHG_RUN_MIN_RISE = 2     # 충전 구간 라벨: 최소 상승(%p)


def fit_rate(pts):
    """[(ts, pct)] 오름차순 -> 방전 속도 %/h (감소가 양수). 2점 미만이거나 시각이 같으면 None."""
    n = len(pts)
    if n < 2:
        return None
    t0 = pts[0][0]
    xs = [p[0] - t0 for p in pts]
    ys = [p[1] for p in pts]
    mx = sum(xs) / float(n)
    my = sum(ys) / float(n)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return -sxy / sxx * 3600.0


def _windows(rows, charge=False):
    """rows: [(ts, pct, on_battery, ...)] 오름차순 -> 각 행의 (창 시작 인덱스, 기울기). 없으면 (None, None).
    연속 배터리 구간은 전원 연결 샘플이나 MAX_GAP 초과 공백에서 끊기고, 전원 샘플은 창에 넣지 않는다.
    charge=True 면 rows[3] = is_charging 이 필요하고, 어댑터 연결 + 충전 중 샘플만으로 CHG_WINDOW 창을
    잡아 충전 속도(상승이 양수)를 낸다. 완충·충전 대기 샘플은 구간을 끊는다."""
    if charge:
        win, span, nmin, sign = CHG_WINDOW, CHG_MIN_SPAN, CHG_MIN_SAMPLES, -1.0
        ok = lambda r: not r[2] and r[3]
    else:
        win, span, nmin, sign = WINDOW, MIN_SPAN, MIN_SAMPLES, 1.0
        ok = lambda r: r[2]
    out = [(None, None)] * len(rows)
    run = 0  # 현재 연속 구간의 시작 인덱스
    j = 0    # 창 시작 인덱스
    for i, r in enumerate(rows):
        if not ok(r):
            run = i + 1
            continue
        if i > 0 and r[0] - rows[i - 1][0] > MAX_GAP:
            run = i
        j = max(j, run)
        while r[0] - rows[j][0] > win:
            j += 1
        if i - j + 1 >= nmin and r[0] - rows[j][0] >= span:
            rate = fit_rate([(x[0], x[1]) for x in rows[j:i + 1]])
            out[i] = (j, None if rate is None else sign * rate)
    return out


def window_rates(rows, charge=False):
    """각 행의 직전 창 기울기 %/h (None 가능). 방전은 30분 창·감소가 양수, 충전은 10분 창·상승이 양수."""
    return [w[1] for w in _windows(rows, charge)]


def _recent(conn, now, span):
    return conn.execute(
        "SELECT ts, pct, on_battery, is_charging FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (now - span, now)).fetchall()


def current_rate(conn, now, charge=False):
    """지금 이 순간의 기울기 %/h. 마지막 샘플이 MAX_GAP 안에 없거나 창이 부족하면 None."""
    rows = _recent(conn, now, CHG_WINDOW if charge else WINDOW)
    if not rows or now - rows[-1][0] > MAX_GAP:
        return None
    return window_rates(rows, charge)[-1]


def _line(win):
    """[(ts, pct)] -> 최소제곱 직선 p(t) 함수."""
    n = len(win)
    t0 = win[0][0]
    xs = [w[0] - t0 for w in win]
    mx = sum(xs) / float(n)
    my = sum(w[1] for w in win) / float(n)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:  # 시각이 모두 같으면 평균값 수평선
        return lambda t: my
    k = sum((x - mx) * (w[1] - my) for x, w in zip(xs, win)) / sxx
    return lambda t: my + k * ((t - t0) - mx)


def baseline(conn, now):
    """최근 BASE_DAYS 일 배터리 사용 중 30분 창 기울기의 중앙값. 유효 창이 부족하면 None."""
    rows = conn.execute(
        "SELECT ts, pct, on_battery FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (now - BASE_DAYS * 86400, now)).fetchall()
    vals = [v for v in window_rates(rows) if v is not None]
    if len(vals) < BASE_MIN_POINTS:
        return None
    return statistics.median(vals)


def threshold(base):
    """평소 기울기 -> 급방전 기준 %/h."""
    if base is None:
        return FALLBACK
    return max(FLOOR, base * FACTOR)


def steep_intervals(pts, thr):
    """시계열 포인트(dict: t, r 선택) 중 기울기가 thr 초과인 연속 구간 -> [{t0, t1, peak}].
    창 기울기는 직전 30분을 반영하므로 띠 시작을 반 창 앞당긴다."""
    out, cur = [], None
    for p in pts:
        r = p.get("r")
        if r is not None and r > thr:
            if cur is None:
                cur = {"t0": p["t"] - WINDOW // 2, "t1": p["t"], "peak": r}
            else:
                cur["t1"] = p["t"]
                cur["peak"] = max(cur["peak"], r)
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    merged = []  # 잠깐 기준 밑으로 내려갔다 다시 오른 경우(띠가 겹침)는 한 구간으로
    for iv in out:
        if merged and iv["t0"] <= merged[-1]["t1"]:
            merged[-1]["t1"] = iv["t1"]
            merged[-1]["peak"] = max(merged[-1]["peak"], iv["peak"])
        else:
            merged.append(iv)
    for iv in merged:
        iv["peak"] = round(iv["peak"], 1)
    return merged


def projection(conn, now):
    """지금 배터리로 쓰는 중이고 기울기가 있으면 추세선 양끝과 0% 도달 예상 시각.
    {t0, p0, t1, p1, rate, empty} 또는 None."""
    rows = _recent(conn, now, WINDOW)
    if not rows or now - rows[-1][0] > MAX_GAP or not rows[-1][2]:
        return None
    j, rate = _windows(rows)[-1]
    if rate is None or rate < MIN_PROJ_RATE:
        return None
    win = [(r[0], r[1]) for r in rows[j:]]
    t0 = win[0][0]
    p_at = _line(win)
    p1 = p_at(rows[-1][0])
    return {"t0": t0, "p0": round(p_at(t0), 2), "t1": rows[-1][0], "p1": round(p1, 2),
            "rate": round(rate, 1), "empty": int(rows[-1][0] + max(p1, 0) / rate * 3600)}


def drain_runs(rows, min_sec=RUN_MIN_SEC, min_drop=RUN_MIN_DROP):
    """rows: [(ts, pct, on_battery, ...)] 오름차순 -> 하락 구간 [{t0, t1, pct0, pct1, rate}].
    하락 구간 = 전원 연결이나 MAX_GAP 초과 공백(수면·미수집) 없이 이어진 배터리 사용 구간.
    rate 는 구간 전체에 맞춘 회귀 기울기 %/h. 짧거나 거의 안 떨어진 구간은 뺀다."""
    runs = []
    cur = []

    def close():
        if len(cur) >= 2 and cur[-1][0] - cur[0][0] >= min_sec \
                and cur[0][1] - cur[-1][1] >= min_drop:
            rate = fit_rate(cur)
            if rate is not None and rate > 0:
                runs.append({"t0": cur[0][0], "t1": cur[-1][0], "pct0": cur[0][1],
                             "pct1": cur[-1][1], "rate": round(rate, 1)})

    for r in rows:
        if not r[2] or (cur and r[0] - cur[-1][0] > MAX_GAP):
            close()
            cur = []
        if r[2]:
            cur.append((r[0], r[1]))
    close()
    return runs


# ── 충전 ──

def charge_profile(conn, now):
    """최근 CHG_PROFILE_DAYS 일, 충전 중 연속 샘플 쌍으로 낸 10% 구간별 평균 충전 속도 {0: %/h, 10: ..., 90: ...}.
    이 맥·평소 충전기의 충전 곡선이다 (보통 80%를 넘으면 느려진다). 관찰이 부족한 구간은 뺀다."""
    rows = conn.execute(
        "SELECT ts, pct, on_battery, is_charging FROM samples WHERE ts >= ? AND ts <= ? ORDER BY ts",
        (now - CHG_PROFILE_DAYS * 86400, now)).fetchall()
    rise, secs = {}, {}
    for a, b in zip(rows, rows[1:]):
        dt = b[0] - a[0]
        if a[2] or b[2] or not a[3] or dt <= 0 or dt > MAX_GAP or a[1] >= 100:
            continue
        k = min(a[1] // 10 * 10, 90)
        rise[k] = rise.get(k, 0) + (b[1] - a[1])
        secs[k] = secs.get(k, 0) + dt
    return {k: rise[k] * 3600.0 / secs[k] for k in secs
            if secs[k] >= CHG_BAND_MIN_SEC and rise[k] > 0}


def _band_rate(prof, pct):
    """pct 가 속한 10% 구간의 평소 충전 속도. 그 구간 기록이 없으면 가장 가까운 구간 값."""
    if not prof:
        return None
    k = min(int(pct) // 10 * 10, 90)
    if k in prof:
        return prof[k]
    return prof[min(prof, key=lambda b: (abs(b - k), b))]


def _charge_steps(prof, p0, p1, scale=1.0):
    """p0% -> p1% 를 10% 경계로 나눠 [(도착 %, 걸린 시간 h)]. 각 구간은 평소 속도 × scale."""
    out, p = [], float(p0)
    while p < p1 - 1e-9:
        nb = min((int(p) // 10 + 1) * 10, p1)
        out.append((nb, (nb - p) / (_band_rate(prof, p) * scale)))
        p = float(nb)
    return out


def is_slow_charge(rate, usual, pct0):
    """평소 같은 % 범위 속도의 CHG_SLOW 배 미만이면 느린 충전 (저출력 충전기·과열·고부하 의심)."""
    return bool(usual and pct0 < CHG_SLOW_BELOW and rate < CHG_SLOW * usual)


def expected_charge_rate(prof, p0, p1):
    """평소 곡선대로라면 p0% -> p1% 충전의 평균 속도 %/h. 곡선이 없거나 범위가 없으면 None."""
    if not prof or p1 <= p0:
        return None
    hours = sum(h for _, h in _charge_steps(prof, p0, p1))
    return (p1 - p0) / hours if hours > 0 else None


def charge_projection(conn, now, prof):
    """지금 충전 중이고 10분 추세가 있으면 추세선 양끝 + 100% 까지 예상 경로.
    경로는 평소 충전 곡선을 따르되, 지금 속도가 평소와 다른 만큼(scale) 전체를 늘이거나 줄인다.
    {t0, p0, t1, p1, rate, usual, path: [[t, pct]...], full, slow} 또는 None."""
    rows = _recent(conn, now, CHG_WINDOW)
    if not rows or now - rows[-1][0] > MAX_GAP or rows[-1][2] or not rows[-1][3]:
        return None
    j, rate = _windows(rows, charge=True)[-1]
    if rate is None or rate < MIN_PROJ_RATE:
        return None
    win = [(r[0], r[1]) for r in rows[j:]]
    p_at = _line(win)
    t0, t1 = win[0][0], rows[-1][0]
    p0, p1 = p_at(t0), min(p_at(t1), CHG_TARGET)
    if p1 >= CHG_TARGET:  # 이미 다 찼는데 잠깐 충전 표시만 남은 경우
        return None
    usual = expected_charge_rate(prof, max(p0, 0), p1)
    if usual:
        scale = min(max(rate / usual, CHG_SCALE[0]), CHG_SCALE[1])
        steps = _charge_steps(prof, p1, CHG_TARGET, scale)
    else:  # 평소 곡선이 없으면 지금 속도 직선
        steps = [(CHG_TARGET, (CHG_TARGET - p1) / rate)] if p1 < CHG_TARGET else []
    path, t = [[t1, round(p1, 2)]], float(t1)
    for nb, h in steps:
        t += h * 3600
        path.append([int(t), nb])
    return {"t0": t0, "p0": round(p0, 2), "t1": t1, "p1": round(p1, 2), "rate": round(rate, 1),
            "usual": round(usual, 1) if usual else None, "path": path, "full": path[-1][0],
            "slow": 1 if is_slow_charge(rate, usual, p0) else 0}


def charge_runs(rows, prof=None, min_sec=CHG_RUN_MIN_SEC, min_rise=CHG_RUN_MIN_RISE):
    """rows: [(ts, pct, on_battery, is_charging)] 오름차순 -> 충전 구간 [{t0, t1, pct0, pct1, rate, usual, slow}].
    충전 구간 = 어댑터 연결 + 충전 중 샘플이 MAX_GAP 넘는 공백 없이 이어진 구간. 완충 뒤 꽂아만 둔
    시간이나 80% 대기는 빠진다. rate 는 구간 전체 회귀 기울기(상승이 양수), usual 은 같은 % 범위의 평소 속도."""
    runs, cur = [], []

    def close():
        if len(cur) >= 2 and cur[-1][0] - cur[0][0] >= min_sec \
                and cur[-1][1] - cur[0][1] >= min_rise:
            rate = fit_rate(cur)
            if rate is not None and rate < 0:
                usual = expected_charge_rate(prof, cur[0][1], cur[-1][1]) if prof else None
                runs.append({"t0": cur[0][0], "t1": cur[-1][0], "pct0": cur[0][1],
                             "pct1": cur[-1][1], "rate": round(-rate, 1),
                             "usual": round(usual, 1) if usual else None,
                             "slow": 1 if is_slow_charge(-rate, usual, cur[0][1]) else 0})

    for r in rows:
        charging = not r[2] and r[3]
        if not charging or (cur and r[0] - cur[-1][0] > MAX_GAP):
            close()
            cur = []
        if charging:
            cur.append((r[0], r[1]))
    close()
    return runs


def fmt_hours(h):
    """시간(float) -> '약 1시간 40분' 식 문구."""
    m = int(round(h * 60))
    if m < 60:
        return "약 %d분" % m
    hh, mm = divmod(m, 60)
    return "약 %d시간" % hh if mm == 0 else "약 %d시간 %d분" % (hh, mm)
