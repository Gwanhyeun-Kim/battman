#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pmset 전원 로그 흡수(수면/기상/어댑터) 단위 테스트."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import init_db, parse_pm_events, store_pm_events  # noqa: E402
from report import derive_events, merge_series, sleep_intervals  # noqa: E402

# 실제 pmset -g log 발췌 (KST). 도메인 열은 20자 패딩 + 탭.
LOG = """\
2026-09-08 13:15:11 +0900 Notification        \tDisplay is turned on
2026-09-08 13:15:11 +0900 Wake                \tWake from Hibernate [CDNVA] : due to pwrbtn/UserActivity Assertion Using AC (Charge:4%)
2026-09-08 13:56:19 +0900 Sleep               \tEntering Sleep state due to 'Maintenance Sleep':TCPKeepAlive=active Using Batt (Charge:41%) 1057 secs
2026-09-08 13:56:21 +0900 Wake Requests       \t[*process=dasd request=SleepService deltaSecs=1054 wakeAt=2026-09-08 14:13:56]
2026-09-08 14:13:56 +0900 DarkWake            \tDarkWake from Deep Idle [CDNP] : due to NUB.SPMI0Sw3IRQ nub-spmi0.0x02 rtc/SleepService Using BATT (Charge:41%) 2 secs
2026-09-08 14:13:58 +0900 Sleep               \tEntering Sleep state due to 'Sleep Service Back to Sleep':TCPKeepAlive=active Using Batt (Charge:40%) 2358 secs
2026-09-08 14:53:16 +0900 DarkWake            \tDarkWake from Deep Idle [CDNPB] : due to smc.sysState.Wake(0x70070000) USB-C_plug SMC.OutboxNotEmpty nub-spmi-a0.0x0B/ Using AC (Charge:40%) 0 secs
2026-09-08 14:53:16 +0900 Wake                \tDarkWake to FullWake from Deep Idle [CDNVAPB] : due to Notification Using AC (Charge:40%)
2026-09-08 14:53:16 +0900 Assertions          \tPID 337(powerd) Created InternalPreventSleep "com.apple.powermanagement.acwakelinger" Using AC(Charge: 40)
"""


def kst(s):
    """'2026-09-08 13:15:11' -> epoch (KST)."""
    import datetime
    return int(datetime.datetime.strptime(s + " +0900", "%Y-%m-%d %H:%M:%S %z").timestamp())


class ParseTest(unittest.TestCase):
    def test_parses_sleep_wake_darkwake_only(self):
        ev = parse_pm_events(LOG, 0, 4_000_000_000)
        kinds = [e[1] for e in ev]
        self.assertEqual(kinds, ["wake", "sleep", "darkwake", "sleep", "darkwake", "wake"])

    def test_fields(self):
        ev = parse_pm_events(LOG, 0, 4_000_000_000)
        ts, kind, pct, ob, detail = ev[0]
        self.assertEqual(ts, kst("2026-09-08 13:15:11"))
        self.assertEqual((kind, pct, ob), ("wake", 4, 0))
        self.assertIn("pwrbtn", detail)
        ts, kind, pct, ob, detail = ev[1]
        self.assertEqual((kind, pct, ob), ("sleep", 41, 1))
        self.assertEqual(detail, "Maintenance Sleep")
        ts, kind, pct, ob, detail = ev[4]  # BATT 대문자, USB-C_plug 사유
        self.assertEqual((kind, pct, ob), ("darkwake", 40, 0))
        self.assertIn("USB-C_plug", detail)

    def test_window_filter(self):
        lo, hi = kst("2026-09-08 14:00:00"), kst("2026-09-08 14:30:00")
        ev = parse_pm_events(LOG, lo, hi)
        self.assertEqual([e[1] for e in ev], ["darkwake", "sleep"])

    def test_store_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        init_db(conn)
        ev = parse_pm_events(LOG, 0, 4_000_000_000)
        self.assertEqual(store_pm_events(conn, ev), 6)
        self.assertEqual(store_pm_events(conn, ev), 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM pm_events").fetchone()[0], 6)


class IntervalTest(unittest.TestCase):
    def setUp(self):
        self.pm = parse_pm_events(LOG, 0, 4_000_000_000)

    def test_one_interval_from_sleep_to_full_wake(self):
        ivs = sleep_intervals(self.pm, [], kst("2026-09-08 15:00:00"))
        self.assertEqual(len(ivs), 1)
        iv = ivs[0]
        self.assertEqual(iv["t0"], kst("2026-09-08 13:56:19"))
        self.assertEqual(iv["t1"], kst("2026-09-08 14:53:16"))
        self.assertEqual((iv["pct0"], iv["pct1"]), (41, 40))
        self.assertEqual(iv["reason"], "Maintenance Sleep")

    def test_open_interval_closes_at_steady_samples(self):
        pm = [e for e in self.pm if e[1] != "wake"]  # 기상 기록 소실
        t = kst("2026-09-08 14:53:16")
        samples = [(t, 40, 0, None, None, None), (t + 60, 40, 0, None, None, None),
                   (t + 120, 41, 0, None, None, None)]
        ivs = sleep_intervals(pm, samples, t + 600)
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["t1"], t)
        self.assertIsNone(ivs[0]["wake"])

    def test_short_interval_dropped(self):
        t = kst("2026-09-08 10:00:00")
        pm = [(t, "sleep", 50, 1, "Clamshell Sleep"), (t + 30, "wake", 50, 1, "lid")]
        self.assertEqual(sleep_intervals(pm, [], t + 100), [])


class MergeTest(unittest.TestCase):
    def test_merge_marks_sleep_gap_and_source(self):
        pm = parse_pm_events(LOG, 0, 4_000_000_000)
        t0 = kst("2026-09-08 13:15:16")
        # 수집기: 13:15~13:56 1분 간격(요약: 3개), 수면 중 darkwake 1개(14:13:56+2s), 기상 후 1개
        samples = [(t0, 4, 0, None, None, None), (t0 + 60, 5, 0, None, None, None),
                   (kst("2026-09-08 13:55:53"), 40, 0, None, None, None),
                   (kst("2026-09-08 14:13:58"), 41, 1, None, None, None),
                   (kst("2026-09-08 14:53:42"), 41, 0, None, None, None)]
        now = kst("2026-09-08 15:00:00")
        ivs = sleep_intervals(pm, samples, now)
        pts = merge_series(samples, pm, ivs, 0, now)
        ts = [p["t"] for p in pts]
        self.assertEqual(ts, sorted(ts))
        # 45초 이내 수집기 샘플이 있는 pm 이벤트는 중복 삽입 안 함 (13:15:11 wake, 14:13:56 darkwake)
        self.assertNotIn(kst("2026-09-08 13:15:11"), ts)
        self.assertNotIn(kst("2026-09-08 14:13:56"), ts)
        # 수면 진입(13:56:19)과 기상(14:53:16)은 pm 출처로 들어감
        by_t = {p["t"]: p for p in pts}
        self.assertEqual(by_t[kst("2026-09-08 13:56:19")]["src"], 1)
        self.assertEqual(by_t[kst("2026-09-08 14:53:16")]["src"], 1)
        # 수면 구간 안 포인트는 asl=1, 기상 포인트부터 0
        self.assertEqual(by_t[kst("2026-09-08 13:56:19")]["asl"], 1)
        self.assertEqual(by_t[kst("2026-09-08 14:13:58")]["asl"], 1)
        self.assertEqual(by_t[kst("2026-09-08 14:53:16")]["asl"], 0)
        # 13:16 -> 13:55 는 40분 공백인데 수면 아님 -> gap 표시
        self.assertGreater(by_t[kst("2026-09-08 13:55:53")]["gap"], 0)
        self.assertEqual(by_t[t0 + 60]["gap"], 0)

    def test_events_plug_unplug_precision(self):
        now = kst("2026-09-08 15:00:00")
        a = kst("2026-09-08 13:28:36")
        samples = [(a, 19, 0, None, None, None), (a + 60, 19, 1, None, None, None)]
        pts = merge_series(samples, [], [], 0, now)
        ev = derive_events(pts, [])
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["kind"], "unplug")
        self.assertEqual(ev[0]["t"], a + 60)
        self.assertTrue(ev[0]["exact"])

    def test_events_plug_during_sleep_is_exact_via_usbc(self):
        pm = parse_pm_events(LOG, 0, 4_000_000_000)
        now = kst("2026-09-08 15:00:00")
        ivs = sleep_intervals(pm, [], now)
        pts = merge_series([], pm, ivs, 0, now)
        ev = derive_events(pts, ivs)
        plugs = [e for e in ev if e["kind"] == "plug"]
        self.assertEqual(len(plugs), 1)
        self.assertEqual(plugs[0]["t"], kst("2026-09-08 14:53:16"))
        self.assertTrue(plugs[0]["exact"])
        sleeps = [e for e in ev if e["kind"] == "sleep"]
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(sleeps[0]["t1"], kst("2026-09-08 14:53:16"))

    def test_events_unplug_bracketed_when_gap(self):
        now = kst("2026-09-08 15:00:00")
        a = kst("2026-09-08 13:00:00")
        samples = [(a, 80, 0, None, None, None), (a + 1800, 79, 1, None, None, None)]
        pts = merge_series(samples, [], [], 0, now)
        ev = derive_events(pts, [])
        self.assertEqual(ev[0]["kind"], "unplug")
        self.assertFalse(ev[0]["exact"])
        self.assertEqual(ev[0]["since"], a)


if __name__ == "__main__":
    unittest.main()
