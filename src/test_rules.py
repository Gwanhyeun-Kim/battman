#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""battman 이상 감지 규칙 단위 테스트 (수용 기준 3, 6번)."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import init_db
from rules import check_rules, convert_amperage
import slope

NOW = 1_800_000_000


class Notif:
    def __init__(self):
        self.calls = []

    def __call__(self, body):
        self.calls.append(body)


def fresh():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def add_sample(conn, ts, pct, on_batt=1, gap_min=1.0):
    conn.execute(
        "INSERT OR REPLACE INTO samples(ts, pct, on_battery, is_charging, watts, gap_min) "
        "VALUES (?,?,?,0,NULL,?)", (ts, pct, on_batt, gap_min))


def add_proc(conn, ts, pid, ppid, pcpu, comm):
    conn.execute(
        "INSERT INTO proc_samples(ts, pid, ppid, pcpu, comm) VALUES (?,?,?,?,?)",
        (ts, pid, ppid, pcpu, comm))


class TestAmperage(unittest.TestCase):
    def test_discharge_negative(self):
        self.assertEqual(convert_amperage("18446744073709547293"), -4323)

    def test_positive_passthrough(self):
        self.assertEqual(convert_amperage("1500"), 1500)


def drain(conn, end_ts, minutes, start_pct, rate_h, on_batt=1):
    """end_ts 에서 끝나는 minutes 분 동안 시간당 rate_h% 로 선형 감소하는 1분 샘플."""
    for i in range(minutes + 1):
        ts = end_ts - (minutes - i) * 60
        add_sample(conn, ts, round(start_pct - rate_h * (i / 60.0)), on_batt)


def history(conn, rate_h, days_ago=1, hours=3):
    """평소 기울기용 과거 이력 (days_ago 일 전, hours 시간 연속 배터리 사용)."""
    drain(conn, NOW - days_ago * 86400, hours * 60, 95, rate_h)


class TestR1RapidDrain(unittest.TestCase):
    def run_rules(self, conn):
        n = Notif()
        fired = check_rules(conn, NOW, tempfile.mktemp(), notify_fn=n)
        return fired, n

    def test_fires_on_rapid_without_history(self):
        conn = fresh()
        drain(conn, NOW, 40, 95, 54)  # 이력 없음 -> 고정 기준 30%/h
        fired, n = self.run_rules(conn)
        self.assertEqual([f[0] for f in fired], ["rapid_drain"])
        self.assertEqual(len(n.calls), 1)
        self.assertIn("뒤 방전", fired[0][1])

    def test_no_fire_normal(self):
        conn = fresh()
        drain(conn, NOW, 40, 95, 12)
        fired, _ = self.run_rules(conn)
        self.assertEqual(fired, [])

    def test_no_fire_on_ac(self):
        conn = fresh()
        drain(conn, NOW, 40, 95, 54, on_batt=0)
        fired, _ = self.run_rules(conn)
        self.assertEqual(fired, [])

    def test_no_fire_short_spike(self):
        # 평소 10%/h(기준 15). 30분 평범하게 쓰다 마지막 6분만 급강하 -> 지금 창은 기준을 넘지만
        # 10분 전 창은 정상이라 무시
        conn = fresh()
        history(conn, 10)
        drain(conn, NOW - 360, 34, 85, 10)
        drain(conn, NOW, 6, 79, 80)
        thr = slope.threshold(slope.baseline(conn, NOW))
        self.assertGreater(slope.current_rate(conn, NOW), thr)        # 전제: 지금은 초과
        self.assertLess(slope.current_rate(conn, NOW - 600), thr)     # 전제: 10분 전은 정상
        fired, _ = self.run_rules(conn)
        self.assertEqual(fired, [])

    def test_too_little_data(self):
        conn = fresh()
        drain(conn, NOW, 15, 95, 54)  # 15분뿐 -> 창 부족
        fired, _ = self.run_rules(conn)
        self.assertEqual(fired, [])

    def test_relative_to_personal_baseline(self):
        # 평소 10%/h 인 사람 -> 기준 15%/h. 22%/h 는 고정 기준(30)으론 안 잡히지만 여기선 잡힘
        conn = fresh()
        history(conn, 10)
        drain(conn, NOW, 40, 90, 22)
        fired, _ = self.run_rules(conn)
        self.assertEqual([f[0] for f in fired], ["rapid_drain"])
        self.assertIn("평소", fired[0][1])
        self.assertIn("배", fired[0][1])

    def test_high_baseline_raises_bar(self):
        # 평소 25%/h -> 기준 37.5%/h. 32%/h 는 고정 기준(30)이면 걸렸겠지만 이 사람에겐 정상
        conn = fresh()
        history(conn, 25, hours=3)
        drain(conn, NOW, 40, 95, 32)
        fired, _ = self.run_rules(conn)
        self.assertEqual(fired, [])

    def test_no_fire_without_baseline_at_22(self):
        conn = fresh()
        drain(conn, NOW, 40, 90, 22)
        fired, _ = self.run_rules(conn)
        self.assertEqual(fired, [])

    def test_suspect_named(self):
        conn = fresh()
        drain(conn, NOW, 40, 95, 54)
        add_proc(conn, NOW, 777, 1, 99.0, "bun")
        fired, _ = self.run_rules(conn)
        self.assertIn("bun", fired[0][1])


class TestR2CpuHog(unittest.TestCase):
    def build(self, streak):
        conn = fresh()
        for i in range(5):
            ts = NOW - (4 - i) * 60
            add_sample(conn, ts, 50, on_batt=0)  # 전원 연결 중에도 발동해야 함
            if i >= 5 - streak:
                add_proc(conn, ts, 999, 1, 99.0, "yes")
        return conn

    def test_fires_after_5(self):
        conn = self.build(5)
        n = Notif()
        fired = check_rules(conn, NOW, tempfile.mktemp(), notify_fn=n)
        self.assertEqual([f[0] for f in fired], ["cpu_hog"])
        self.assertIn("고아", fired[0][1])
        self.assertIn("kill -9 999", fired[0][1])

    def test_no_fire_at_4(self):
        conn = self.build(4)
        fired = check_rules(conn, NOW, tempfile.mktemp(), notify_fn=Notif())
        self.assertEqual(fired, [])


class TestR3SleepDrain(unittest.TestCase):
    def build(self, drop, gap_min=480.0):
        conn = fresh()
        add_sample(conn, NOW - int(gap_min * 60), 33, gap_min=1.0)
        add_sample(conn, NOW, 33 - drop, gap_min=gap_min)
        return conn

    def test_fires(self):
        conn = self.build(20)  # 8시간 20% = 2.5%/h
        fired = check_rules(conn, NOW, tempfile.mktemp(), notify_fn=Notif())
        self.assertEqual([f[0] for f in fired], ["sleep_drain"])

    def test_no_fire_normal_sleep(self):
        conn = self.build(8)  # 1.0%/h
        fired = check_rules(conn, NOW, tempfile.mktemp(), notify_fn=Notif())
        self.assertEqual(fired, [])

    def test_boundary_exactly_2(self):
        conn = self.build(16)  # 정확히 2.0%/h → 초과 아님
        fired = check_rules(conn, NOW, tempfile.mktemp(), notify_fn=Notif())
        self.assertEqual(fired, [])


class TestCooldown(unittest.TestCase):
    def test_second_trigger_records_but_no_notify(self):
        conn = fresh()
        st = tempfile.mktemp()
        n = Notif()
        drain(conn, NOW, 40, 95, 54)
        check_rules(conn, NOW, st, notify_fn=n)
        add_sample(conn, NOW + 60, 58)  # 1분 뒤에도 같은 속도
        check_rules(conn, NOW + 60, st, notify_fn=n)
        self.assertEqual(len(n.calls), 1)  # 알림은 1번
        cnt = conn.execute("SELECT COUNT(*) FROM anomalies").fetchone()[0]
        self.assertEqual(cnt, 2)  # 기록은 2번

    def test_notifies_again_after_cooldown(self):
        conn = fresh()
        st = tempfile.mktemp()
        n = Notif()
        drain(conn, NOW, 40, 95, 54)
        check_rules(conn, NOW, st, notify_fn=n)
        later = NOW + 2 * 3600 + 660
        drain(conn, later, 40, 95, 54)
        check_rules(conn, later, st, notify_fn=n)
        self.assertEqual(len(n.calls), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
