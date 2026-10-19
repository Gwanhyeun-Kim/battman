#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battman 방전·충전 기울기 계산 단위 테스트."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import init_db
import slope

NOW = 1_800_000_000


def fresh():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def add(conn, ts, pct, ob=1):
    conn.execute("INSERT OR REPLACE INTO samples(ts, pct, on_battery, is_charging, watts, gap_min) "
                 "VALUES (?,?,?,0,NULL,1.0)", (ts, pct, ob))


def drain(conn, end_ts, minutes, start_pct, rate_h, ob=1):
    for i in range(minutes + 1):
        add(conn, end_ts - (minutes - i) * 60, round(start_pct - rate_h * i / 60.0), ob)


class TestFit(unittest.TestCase):
    def test_exact_line(self):
        pts = [(i * 60, 90 - i * 0.5) for i in range(30)]  # 0.5%/분 = 30%/h
        self.assertAlmostEqual(slope.fit_rate(pts), 30.0, places=6)

    def test_integer_steps_close(self):
        pts = [(i * 60, round(90 - i * 0.2)) for i in range(31)]  # 12%/h, 정수 계단
        self.assertAlmostEqual(slope.fit_rate(pts), 12.0, delta=1.0)

    def test_degenerate(self):
        self.assertIsNone(slope.fit_rate([(0, 50)]))
        self.assertIsNone(slope.fit_rate([(5, 50), (5, 49)]))

    def test_charging_negative(self):
        pts = [(i * 60, 40 + i) for i in range(20)]
        self.assertLess(slope.fit_rate(pts), 0)


class TestWindows(unittest.TestCase):
    def test_needs_span(self):
        rows = [(NOW + i * 60, 90 - i // 5, 1) for i in range(40)]
        r = slope.window_rates(rows)
        self.assertIsNone(r[10])       # 10분: 창 부족
        self.assertIsNotNone(r[25])    # 25분: 충분

    def test_gap_breaks_run(self):
        rows = [(NOW + i * 60, 90, 1) for i in range(30)]
        rows += [(NOW + 3600 + i * 60, 80, 1) for i in range(10)]  # 30분 공백 뒤 10분
        r = slope.window_rates(rows)
        self.assertIsNone(r[-1])  # 공백 앞 샘플과 섞지 않음

    def test_ac_breaks_run(self):
        rows = [(NOW + i * 60, 90, 1) for i in range(30)]
        rows += [(NOW + 1800, 90, 0)]
        rows += [(NOW + 1860 + i * 60, 90, 1) for i in range(10)]
        r = slope.window_rates(rows)
        self.assertIsNone(r[30])   # 전원 연결 샘플
        self.assertIsNone(r[-1])   # 분리 후 10분은 새 구간이라 부족

    def test_ac_sample_not_in_window(self):
        # 전원 연결 중 100% 찍힌 샘플 바로 뒤에 분리 -> 그 100% 는 창에 안 들어가야
        rows = [(NOW, 100, 0)] + [(NOW + 60 + i * 60, 80 - i // 6, 1) for i in range(30)]
        j, rate = slope._windows(rows)[-1]
        self.assertGreaterEqual(j, 1)
        self.assertLess(rate, 15)  # 10%/h 근처. 100% 가 섞이면 크게 튐

    def test_projection_uses_same_window(self):
        conn = fresh()
        add(conn, NOW - 2400, 100, ob=0)
        drain(conn, NOW, 39, 80, 20)
        p = slope.projection(conn, NOW)
        self.assertAlmostEqual(p["rate"], slope.current_rate(conn, NOW), places=1)
        self.assertGreater(p["t0"], NOW - 2400)  # 전원 샘플이 추세선 시작이 아님


class TestBaseline(unittest.TestCase):
    def test_none_without_history(self):
        self.assertIsNone(slope.baseline(fresh(), NOW))
        self.assertEqual(slope.threshold(None), slope.FALLBACK)

    def test_median_and_threshold(self):
        conn = fresh()
        drain(conn, NOW - 86400, 180, 95, 10)
        b = slope.baseline(conn, NOW)
        self.assertAlmostEqual(b, 10.0, delta=0.5)
        self.assertEqual(slope.threshold(b), max(slope.FLOOR, b * slope.FACTOR))

    def test_floor(self):
        self.assertEqual(slope.threshold(4.0), slope.FLOOR)

    def test_old_history_ignored(self):
        conn = fresh()
        drain(conn, NOW - (slope.BASE_DAYS + 1) * 86400, 180, 95, 10)
        self.assertIsNone(slope.baseline(conn, NOW))


class TestCurrent(unittest.TestCase):
    def test_stale_returns_none(self):
        conn = fresh()
        drain(conn, NOW - 600, 40, 95, 30)  # 마지막 샘플이 10분 전 -> 지금 기울기 아님
        self.assertIsNone(slope.current_rate(conn, NOW))

    def test_live(self):
        conn = fresh()
        drain(conn, NOW, 40, 95, 30)
        self.assertAlmostEqual(slope.current_rate(conn, NOW), 30.0, delta=1.5)


class TestSteep(unittest.TestCase):
    def test_merge_and_peak(self):
        pts = [{"t": 0, "r": 10}, {"t": 60, "r": 40}, {"t": 120, "r": 55},
               {"t": 180, "r": None}, {"t": 5000, "r": 50}, {"t": 5060}]
        iv = slope.steep_intervals(pts, 30)
        self.assertEqual(len(iv), 2)
        self.assertEqual(iv[0]["t1"], 120)
        self.assertEqual(iv[0]["t0"], 60 - slope.WINDOW // 2)
        self.assertEqual(iv[0]["peak"], 55)

    def test_overlapping_bands_merge(self):
        # 기준 밑으로 1분 내려갔다 다시 오름 -> 앞당긴 띠 시작이 앞 구간과 겹치므로 하나로
        pts = [{"t": 0, "r": 40}, {"t": 600, "r": 45}, {"t": 660, "r": 10},
               {"t": 720, "r": 50}, {"t": 1500, "r": 35}]
        iv = slope.steep_intervals(pts, 30)
        self.assertEqual(len(iv), 1)
        self.assertEqual((iv[0]["t1"], iv[0]["peak"]), (1500, 50))

    def test_far_apart_stay_separate(self):
        pts = [{"t": 0, "r": 40}, {"t": 60, "r": 10}, {"t": 5000, "r": 40}]
        self.assertEqual(len(slope.steep_intervals(pts, 30)), 2)

    def test_open_tail(self):
        iv = slope.steep_intervals([{"t": 0, "r": 40}, {"t": 60, "r": 41}], 30)
        self.assertEqual(iv[0]["t1"], 60)


class TestProjection(unittest.TestCase):
    def test_empty_time(self):
        conn = fresh()
        drain(conn, NOW, 40, 60, 30)  # 끝 = 40%, 30%/h -> 약 80분 뒤 0%
        p = slope.projection(conn, NOW)
        self.assertAlmostEqual(p["rate"], 30.0, delta=1.5)
        self.assertAlmostEqual((p["empty"] - NOW) / 60.0, 80, delta=8)
        self.assertGreater(p["p0"], p["p1"])

    def test_none_on_ac(self):
        conn = fresh()
        drain(conn, NOW, 40, 60, 30, ob=0)
        self.assertIsNone(slope.projection(conn, NOW))

    def test_none_when_flat(self):
        conn = fresh()
        drain(conn, NOW, 40, 60, 0)
        self.assertIsNone(slope.projection(conn, NOW))


class TestRuns(unittest.TestCase):
    def test_split_on_ac_and_gap(self):
        rows = [(NOW + i * 60, 90 - i // 5, 1) for i in range(40)]          # 40분, 12%/h
        rows += [(NOW + 2400, 82, 0)]                                       # 전원
        rows += [(NOW + 2460 + i * 60, 82 - i // 2, 1) for i in range(5)]    # 4분짜리 -> 제외
        rows += [(NOW + 9999 + i * 60, 80 - i // 3, 1) for i in range(30)]  # 공백 뒤 29분, 20%/h
        runs = slope.drain_runs(rows)
        self.assertEqual(len(runs), 2)
        self.assertAlmostEqual(runs[0]["rate"], 12, delta=1)
        self.assertAlmostEqual(runs[1]["rate"], 20, delta=1.5)
        self.assertEqual((runs[1]["t0"], runs[1]["pct0"]), (NOW + 9999, 80))

    def test_flat_run_skipped(self):
        rows = [(NOW + i * 60, 90 if i < 30 else 89, 1) for i in range(60)]  # 1시간에 1%
        self.assertEqual(slope.drain_runs(rows), [])

    def test_open_run_at_end(self):
        rows = [(NOW + i * 60, 50 - i // 3, 1) for i in range(20)]
        runs = slope.drain_runs(rows)
        self.assertEqual(runs[0]["t1"], NOW + 19 * 60)


def addc(conn, ts, pct, chg=1):
    """어댑터 연결 샘플. chg=1 충전 중, 0 연결만(완충·대기)."""
    conn.execute("INSERT OR REPLACE INTO samples(ts, pct, on_battery, is_charging, watts, gap_min) "
                 "VALUES (?,?,0,?,NULL,1.0)", (ts, pct, chg))


def charge(conn, end_ts, minutes, start_pct, rate_h, chg=1):
    for i in range(minutes + 1):
        addc(conn, end_ts - (minutes - i) * 60, min(round(start_pct + rate_h * i / 60.0), 100), chg)


PROF = {k: 60.0 for k in range(0, 80, 10)}
PROF.update({80: 40.0, 90: 20.0})


class TestChargeWindows(unittest.TestCase):
    def test_rate_positive_10min(self):
        rows = [(NOW + i * 60, 30 + round(i * 1.2), 0, 1) for i in range(15)]  # 72%/h
        r = slope.window_rates(rows, charge=True)
        self.assertIsNone(r[4])                 # 4분: 창 부족
        self.assertAlmostEqual(r[-1], 72, delta=3)

    def test_not_charging_breaks(self):
        rows = [(NOW + i * 60, 80, 0, 0) for i in range(10)]            # 80% 대기
        rows += [(NOW + 600 + i * 60, 80 + i, 0, 1) for i in range(4)]  # 다시 충전 4분
        r = slope.window_rates(rows, charge=True)
        self.assertTrue(all(v is None for v in r))

    def test_battery_rows_ignored(self):
        rows = [(NOW + i * 60, 90 - i // 3, 1, 0) for i in range(20)]
        self.assertTrue(all(v is None for v in slope.window_rates(rows, charge=True)))

    def test_drain_mode_unchanged_with_extra_column(self):
        rows = [(NOW + i * 60, 90 - i // 5, 1, 0) for i in range(40)]
        self.assertAlmostEqual(slope.window_rates(rows)[-1], 12, delta=1)

    def test_current_rate_charge(self):
        conn = fresh()
        charge(conn, NOW, 12, 40, 66)
        self.assertAlmostEqual(slope.current_rate(conn, NOW, charge=True), 66, delta=3)
        self.assertIsNone(slope.current_rate(conn, NOW))  # 방전 기울기는 없음


class TestChargeProfile(unittest.TestCase):
    def test_bands(self):
        conn = fresh()
        charge(conn, NOW - 7200, 60, 20, 60)   # 20 -> 80, 60%/h
        charge(conn, NOW - 3000, 30, 80, 30)   # 80 -> 95, 30%/h
        prof = slope.charge_profile(conn, NOW)
        for k in (20, 30, 40, 50, 60, 70):
            self.assertAlmostEqual(prof[k], 60, delta=6)
        self.assertAlmostEqual(prof[80], 30, delta=4)
        self.assertNotIn(10, prof)

    def test_thin_band_dropped(self):
        conn = fresh()
        charge(conn, NOW, 5, 50, 60)  # 5분뿐 -> CHG_BAND_MIN_SEC 미달
        self.assertEqual(slope.charge_profile(conn, NOW), {})

    def test_hold_and_battery_excluded(self):
        conn = fresh()
        charge(conn, NOW - 3600, 30, 80, 0, chg=0)  # 꽂아만 둠
        drain(conn, NOW, 30, 70, 20)
        self.assertEqual(slope.charge_profile(conn, NOW), {})

    def test_nearest_band(self):
        self.assertEqual(slope._band_rate({30: 50.0, 80: 40.0}, 95), 40.0)
        self.assertEqual(slope._band_rate({30: 50.0, 80: 40.0}, 55), 50.0)  # 같은 거리면 낮은 쪽
        self.assertEqual(slope._band_rate(PROF, 100), 20.0)

    def test_degenerate_inputs(self):
        self.assertIsNone(slope._band_rate({}, 50))
        self.assertEqual(slope._line([(100, 50), (100, 60)])(200), 55)

    def test_expected_rate_harmonic(self):
        # 70 -> 100: 10/60 + 10/40 + 10/20 = 0.9167h -> 30/0.9167
        self.assertAlmostEqual(slope.expected_charge_rate(PROF, 70, 100), 32.73, delta=0.05)
        self.assertAlmostEqual(slope.expected_charge_rate(PROF, 20, 60), 60.0, delta=0.01)
        self.assertIsNone(slope.expected_charge_rate({}, 20, 60))
        self.assertIsNone(slope.expected_charge_rate(PROF, 60, 60))


class TestChargeProjection(unittest.TestCase):
    def test_follows_curve(self):
        conn = fresh()
        charge(conn, NOW, 12, 58, 60)  # 끝 = 70%, 평소와 같은 속도
        p = slope.charge_projection(conn, NOW, PROF)
        self.assertAlmostEqual(p["rate"], 60, delta=3)
        self.assertAlmostEqual(p["usual"], 60, delta=0.5)
        self.assertEqual(p["path"][-1][1], 100)
        self.assertEqual([q for _, q in p["path"][1:]], [80, 90, 100])
        # 70->100 평소 곡선 = 10/60+10/40+10/20 h = 55분 (직선이면 30분)
        self.assertAlmostEqual((p["full"] - NOW) / 60.0, 55, delta=4)
        self.assertEqual(p["slow"], 0)

    def test_scaled_when_slower(self):
        conn = fresh()
        charge(conn, NOW, 12, 46, 30)  # 끝 = 52%, 평소 60의 절반 -> 느림
        p = slope.charge_projection(conn, NOW, PROF)
        self.assertEqual(p["slow"], 1)
        # 52->100 평소 = 28/60+10/40+10/20 = 1.2167h, 0.5배 속도라 2배 = 약 146분
        self.assertAlmostEqual((p["full"] - NOW) / 60.0, 146, delta=10)

    def test_linear_without_profile(self):
        conn = fresh()
        charge(conn, NOW, 12, 58, 60)
        p = slope.charge_projection(conn, NOW, {})
        self.assertIsNone(p["usual"])
        self.assertAlmostEqual((p["full"] - NOW) / 60.0, 30, delta=4)

    def test_none_when_not_charging(self):
        conn = fresh()
        charge(conn, NOW, 12, 80, 0, chg=0)
        self.assertIsNone(slope.charge_projection(conn, NOW, PROF))
        conn = fresh()
        drain(conn, NOW, 40, 60, 30)
        self.assertIsNone(slope.charge_projection(conn, NOW, PROF))

    def test_none_when_full(self):
        conn = fresh()
        charge(conn, NOW, 12, 95, 60)  # 100% 에서 충전 표시만 남음
        self.assertIsNone(slope.charge_projection(conn, NOW, PROF))


class TestChargeRuns(unittest.TestCase):
    def test_split_on_hold_and_slow_flag(self):
        rows = [(NOW + i * 60, 30 + i, 0, 1) for i in range(30)]               # 30->59, 60%/h
        rows += [(NOW + 1800 + i * 60, 59, 0, 0) for i in range(20)]           # 대기
        rows += [(NOW + 3000 + i * 60, 59 + i // 4, 0, 1) for i in range(40)]  # 15%/h -> 느림
        rows += [(NOW + 9000 + i * 60, 59 - i // 3, 1, 0) for i in range(20)]  # 배터리
        runs = slope.charge_runs(rows, PROF)
        self.assertEqual(len(runs), 2)
        self.assertAlmostEqual(runs[0]["rate"], 60, delta=2)
        self.assertEqual((runs[0]["pct0"], runs[0]["pct1"], runs[0]["slow"]), (30, 59, 0))
        self.assertAlmostEqual(runs[1]["rate"], 15, delta=2)
        self.assertEqual(runs[1]["slow"], 1)

    def test_short_or_flat_skipped(self):
        rows = [(NOW + i * 60, 40 + i, 0, 1) for i in range(4)]  # 3분
        rows += [(NOW + 999 + i * 60, 90, 0, 1) for i in range(20)]  # 안 오름
        self.assertEqual(slope.charge_runs(rows, PROF), [])

    def test_no_profile_no_usual(self):
        rows = [(NOW + i * 60, 30 + i, 0, 1) for i in range(10)]
        r = slope.charge_runs(rows)[0]
        self.assertIsNone(r["usual"])
        self.assertEqual(r["slow"], 0)

    def test_slow_rule_skips_top_off(self):
        self.assertTrue(slope.is_slow_charge(20, 60, 50))
        self.assertFalse(slope.is_slow_charge(40, 60, 50))
        self.assertFalse(slope.is_slow_charge(5, 20, 92))  # 90% 이상 마무리는 판정 안 함
        self.assertFalse(slope.is_slow_charge(5, None, 50))


class TestFmt(unittest.TestCase):
    def test_hours(self):
        self.assertEqual(slope.fmt_hours(0.5), "약 30분")
        self.assertEqual(slope.fmt_hours(2.0), "약 2시간")
        self.assertEqual(slope.fmt_hours(1 + 40 / 60.0), "약 1시간 40분")


if __name__ == "__main__":
    unittest.main(verbosity=2)
