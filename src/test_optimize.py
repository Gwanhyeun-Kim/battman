#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""최적화 버튼(optimize.py) 단위 테스트 — '맨날 없다고만 함' 회귀 방지."""
import os
import plistlib
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collector import init_db
import optimize
from optimize import (admin_script, analyze, apply_items, live_stats,
                      parse_cputime, parse_ps)

MY_UID = 501
TEK = "/Library/Application Support/raonsecure/TouchEn nxKey/TEK_Daemon"
PREVIEW = "/System/Applications/Preview.app/Contents/MacOS/Preview"
ECO = "/System/Library/PrivateFrameworks/Ecosystem.framework/Support/ecosystemd"


def fresh():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def history(conn, rows_by_min):
    """rows_by_min: 최근 10분 각 샘플의 [(pid, pcpu, comm), ...] (오래된 것부터)."""
    for i, rows in enumerate(rows_by_min):
        ts = 1_800_000_000 + i * 60
        conn.execute("INSERT INTO samples(ts, pct, on_battery, is_charging) "
                     "VALUES (?, 50, 1, 0)", (ts,))
        for pid, pcpu, comm in rows:
            conn.execute("INSERT INTO proc_samples(ts, pid, ppid, pcpu, comm) "
                         "VALUES (?,?,1,?,?)", (ts, pid, pcpu, comm))


def proc(ppid, uid, cpu, exe):
    return {"ppid": ppid, "uid": uid, "cpu": cpu, "exe": exe}


def no_job(exe, pid, uid):
    return None


def arm(exe):
    return False


class ParseTest(unittest.TestCase):
    def test_parse_cputime(self):
        self.assertAlmostEqual(parse_cputime("27:32.85"), 1652.85)
        self.assertAlmostEqual(parse_cputime("2435:56.47"), 146156.47)
        self.assertAlmostEqual(parse_cputime("0:00.01"), 0.01)
        self.assertAlmostEqual(parse_cputime("1-02:03:04.50"), 93784.5)

    def test_parse_ps_keeps_spaces_in_path(self):
        snap = parse_ps("  6451     1     0   1:27.85 %s\n  99  6451   0   0:00.01 sh\n" % TEK)
        self.assertEqual(snap[6451]["exe"], TEK)
        self.assertEqual(snap[6451]["uid"], 0)
        self.assertEqual(snap[99]["ppid"], 6451)


class LiveStatsTest(unittest.TestCase):
    def test_cpu_from_cputime_delta_and_new_children(self):
        s0 = {10: proc(1, 501, 100.0, "/a"), 20: proc(10, 501, 0.0, "sh")}
        s1 = {10: proc(1, 501, 101.5, "/a"), 21: proc(10, 501, 0.0, "sh")}
        s2 = {10: proc(1, 501, 103.0, "/a"), 22: proc(10, 501, 0.0, "sh")}
        cpu, spawned = live_stats([s0, s1, s2], 3.0)
        self.assertAlmostEqual(cpu[10], 100.0)
        # 20 은 측정 시작 전부터 있던 자식이라 '새로 띄운' 것이 아님
        self.assertEqual(spawned[10], {21, 22})

    def test_grandchildren_count_toward_long_lived_ancestor(self):
        """sh 가 순식간에 끝나도 그 밑의 ps·grep 이 보이면 원래 부모 몫으로 센다."""
        s0 = {10: proc(1, 0, 0.0, "/a")}
        s1 = {10: proc(1, 0, 0.0, "/a"), 21: proc(10, 0, 0.0, "sh"),
              31: proc(21, 0, 0.0, "/bin/ps")}
        s2 = {10: proc(1, 0, 0.0, "/a"), 32: proc(22, 0, 0.0, "/usr/bin/grep")}
        cpu, spawned = live_stats([s0, s1, s2], 3.0)
        self.assertEqual(spawned[10], {21, 31})
        # 부모(22)를 한 번도 못 본 고아 손자는 직계 ppid 로만 기록
        self.assertEqual(spawned[22], {32})


BUN = "/usr/bin/bun"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RENDERER = ("/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/"
            "Helpers/Google Chrome Helper (Renderer).app/Contents/MacOS/Google Chrome Helper (Renderer)")


class AnalyzeTest(unittest.TestCase):
    def test_notified_background_hog_is_found(self):
        """회귀: 알림(R2: 80%×5분)이 잡은 CPU 도둑을 버튼이 '없다'고 하던 버그.
        세션이 끝나고 남은 bun 좀비처럼 창 없이 뒤에서 도는 것."""
        conn = fresh()
        history(conn, [[]] * 5 + [[(4242, c, "bun")] for c in (99, 100, 98, 100, 99)])
        s0 = {4242: proc(1, MY_UID, 0.0, BUN)}
        s1 = {4242: proc(1, MY_UID, 3.0, BUN)}
        res = analyze(conn, [s0, s1], 3.0, MY_UID, set(), no_job, arm)
        self.assertEqual([i["comm"] for i in res["items"]], ["bun"])
        self.assertEqual(res["items"][0]["kind"], "hog")

    def test_foreground_app_is_never_offered_only_reported(self):
        """사용자가 쓰는 앱(Dock 앱)은 CPU 를 많이 써도 끄자고 하지 않는다."""
        conn = fresh()
        history(conn, [[]] * 5 + [[(91524, c, "Preview")] for c in (122, 117, 281, 282, 136)])
        s0 = {91524: proc(1, MY_UID, 0.0, PREVIEW)}
        s1 = {91524: proc(1, MY_UID, 3.3, PREVIEW)}
        res = analyze(conn, [s0, s1], 3.0, MY_UID, set(), no_job, arm,
                      foreground={91524})
        self.assertEqual(res["items"], [])
        self.assertEqual([a["comm"] for a in res["apps"]], ["Preview"])

    def test_helper_of_foreground_app_is_protected(self):
        """Chrome 탭(렌더러)처럼 앱에 딸린 자식도 앱의 일부로 본다."""
        conn = fresh()
        history(conn, [[(700, 90, "Google Chrome Helper (Renderer)")]] * 10)
        s0 = {600: proc(1, MY_UID, 0.0, CHROME), 700: proc(600, MY_UID, 0.0, RENDERER)}
        s1 = {600: proc(1, MY_UID, 0.3, CHROME), 700: proc(600, MY_UID, 2.7, RENDERER)}
        res = analyze(conn, [s0, s1], 3.0, MY_UID, set(), no_job, arm, foreground={600})
        self.assertEqual(res["items"], [])
        self.assertEqual([a["comm"] for a in res["apps"]], ["Google Chrome Helper (Renderer)"])

    def test_spawning_under_foreground_app_is_protected(self):
        conn = fresh()
        snaps = [{600: proc(1, MY_UID, 0.0, CHROME),
                  650: proc(600, MY_UID, 0.0, "/usr/bin/python3"),
                  8000 + i: proc(650, MY_UID, 0.0, "/bin/ps")} for i in range(12)]
        res = analyze(conn, snaps, 3.0, MY_UID, set(), no_job, arm, foreground={600})
        self.assertEqual(res["items"], [])

    def test_spawn_storm_found_and_mapped_to_launchd_service(self):
        """TouchEn nxKey: 초당 수 개씩 ps 를 띄워 시스템 데몬을 달구는 패턴."""
        conn = fresh()
        history(conn, [[(4962, 55, "ecosystemd")]] * 10)
        snaps = []
        for i in range(12):
            snaps.append({6451: proc(1, 0, 87.0, TEK),
                          4962: proc(1, 0, 10800.0 + i * 0.15, ECO),
                          9000 + i: proc(6451, 0, 0.0, "sh")})

        def job(exe, pid, uid):
            return {"domain": "system", "label": "com.raon.touchen.nxkey"} if pid == 6451 else None

        res = analyze(conn, snaps, 3.0, MY_UID, set(), job, lambda e: e == TEK)
        storm = [i for i in res["items"] if i["kind"] == "storm"]
        self.assertEqual(len(storm), 1)
        self.assertEqual(storm[0]["pid"], 6451)
        self.assertEqual(storm[0]["job"]["label"], "com.raon.touchen.nxkey")
        self.assertTrue(storm[0]["admin"])
        self.assertTrue(storm[0]["intel"])
        self.assertGreaterEqual(storm[0]["rate"], 3.0)
        # 시스템 데몬은 직접 끄는 대상이 아니라 '같이 바빠진 것'으로만 보고
        self.assertNotIn("ecosystemd", [i["comm"] for i in res["items"]])
        self.assertIn("ecosystemd", [b["comm"] for b in res["busy"]])
        self.assertIn("ecosystemd", storm[0]["why"])

    def test_storm_blames_only_rosetta_related_daemons(self):
        """PerfPowerServices 처럼 Rosetta 와 무관한 바쁜 데몬을 인과로 엮지 않는다."""
        conn = fresh()
        perf = "/System/Library/PrivateFrameworks/PerfPowerServices.framework/perfpowerservicesd"
        snaps = [{6451: proc(1, 0, 87.0, TEK),
                  300: proc(1, 0, 100.0 + i * 0.13, perf),
                  9000 + i: proc(6451, 0, 0.0, "sh")} for i in range(12)]
        res = analyze(conn, snaps, 3.0, MY_UID, set(), no_job, lambda e: e == TEK)
        self.assertIn("perfpowerservicesd", [b["comm"] for b in res["busy"]])
        self.assertNotIn("perfpowerservicesd", res["items"][0]["why"])

    def test_system_daemon_hog_reported_not_killable(self):
        conn = fresh()
        history(conn, [[(4962, 60, "ecosystemd")]] * 10)
        s0 = {4962: proc(1, 0, 100.0, ECO)}
        s1 = {4962: proc(1, 0, 101.8, ECO)}
        res = analyze(conn, [s0, s1], 3.0, MY_UID, set(), no_job, arm)
        self.assertEqual(res["items"], [])
        self.assertEqual(res["busy"][0]["comm"], "ecosystemd")
        self.assertAlmostEqual(res["busy"][0]["cpu"], 60.0)

    def test_hog_that_already_stopped_is_not_listed(self):
        conn = fresh()
        history(conn, [[(777, 95, "Preview")]] * 10)
        s0 = {777: proc(1, MY_UID, 50.0, PREVIEW)}
        s1 = {777: proc(1, MY_UID, 50.01, PREVIEW)}
        res = analyze(conn, [s0, s1], 3.0, MY_UID, set(), no_job, arm)
        self.assertEqual(res["items"], [])

    def test_pid_reused_by_other_program_is_not_listed(self):
        conn = fresh()
        history(conn, [[(777, 95, "Preview")]] * 10)
        s0 = {777: proc(1, MY_UID, 0.0, "/usr/bin/other")}
        s1 = {777: proc(1, MY_UID, 3.0, "/usr/bin/other")}
        res = analyze(conn, [s0, s1], 3.0, MY_UID, set(), no_job, arm)
        self.assertEqual(res["items"], [])

    def test_tmux_server_is_never_a_storm(self):
        """tmux 서버를 죽이면 안의 claude·codex 세션이 전부 날아간다."""
        conn = fresh()
        snaps = [{700: proc(1, MY_UID, 0.0, "/opt/homebrew/bin/tmux"),
                  8000 + i: proc(700, MY_UID, 0.0, "/bin/zsh")} for i in range(12)]
        res = analyze(conn, snaps, 3.0, MY_UID, set(), no_job, arm)
        self.assertEqual(res["items"], [])

    def test_other_users_gui_job_needs_admin(self):
        conn = fresh()
        snaps = [{700: proc(1, 0, 0.0, "/opt/x/agent"),
                  8000 + i: proc(700, 0, 0.0, "/bin/ps")} for i in range(12)]
        res = analyze(conn, snaps, 3.0, MY_UID, set(),
                      lambda e, p, u: {"domain": "gui/0", "label": "x.agent"}, arm)
        self.assertTrue(res["items"][0]["admin"])

    def test_own_process_tree_and_shells_are_not_storms(self):
        conn = fresh()
        snaps = []
        for i in range(12):
            snaps.append({500: proc(1, MY_UID, 0.0, "/usr/bin/python3"),
                          600: proc(1, MY_UID, 0.0, "/bin/zsh"),
                          8000 + i: proc(500, MY_UID, 0.0, "/bin/ps"),
                          8100 + i: proc(600, MY_UID, 0.0, "/bin/ls")})
        res = analyze(conn, snaps, 3.0, MY_UID, {500}, no_job, arm)
        self.assertEqual(res["items"], [])


class ProtectTest(unittest.TestCase):
    def test_in_app_walks_up_to_foreground_ancestor(self):
        parent = {700: 600, 600: 1, 800: 1}
        self.assertTrue(optimize.in_app(700, parent, {600}))
        self.assertFalse(optimize.in_app(800, parent, {600}))

    def test_respawn_check_ignores_unrelated_same_exe(self):
        """같은 Python 이라도 다른 부모가 띄운 건 '다시 살아남'이 아니다."""
        state = {"alive": True}
        msg = apply_items(
            [{"pid": 4242, "exe": BUN, "comm": "bun"}], MY_UID,
            ps_one=lambda pid: {"uid": MY_UID, "exe": BUN, "ppid": 1} if state["alive"] else None,
            job_finder=no_job,
            kill=lambda pid, sig: state.update(alive=False),
            run_admin=lambda script: (True, ""), launchctl=lambda args: True,
            sleep=lambda s: None,
            pids_of=lambda exe: [] if state["alive"] else [(5555, 86394)])
        self.assertIn("✅ bun — 종료됨", msg)


class ApplyRoutingTest(unittest.TestCase):
    def test_other_users_gui_job_goes_through_admin(self):
        scripts = []
        msg = apply_items(
            [{"pid": 700, "exe": "/opt/x/agent", "comm": "agent"}], MY_UID,
            ps_one=lambda pid: {"uid": 0, "ppid": 1, "exe": "/opt/x/agent"} if not scripts else None,
            job_finder=lambda e, p, u: {"domain": "gui/0", "label": "x.agent"},
            kill=lambda pid, sig: self.fail("권한 없는 kill 시도"),
            run_admin=lambda script: scripts.append(script) or (True, ""),
            launchctl=lambda args: self.fail("권한 없는 bootout 시도"),
            sleep=lambda s: None, pids_of=lambda exe: [])
        self.assertIn("bootout gui/0/x.agent", scripts[0])
        self.assertIn("서비스 중지됨", msg)


class ContextTest(unittest.TestCase):
    def test_foreground_pids_from_lsappinfo(self):
        out = ('61) "미리보기" ASN:0x0-0x54c94c4:\n'
               '    pid = 91524 type="Foreground" flavor=3 Version="1113.3.1"\n'
               '105) "TEK_UserAgent" ASN:0x0-0x76ec6e5:\n'
               '    pid = 6403 type="UIElement" flavor=3 Version="1"\n')
        self.assertEqual(optimize.parse_lsappinfo(out), {91524})

    def test_power_now_vs_usual(self):
        conn = fresh()
        base = 1_800_000_000
        # 지난 며칠 배터리 사용 중 평소 10W, 방금 10분은 25W
        for i in range(200):
            conn.execute("INSERT INTO samples(ts, pct, on_battery, is_charging, watts) "
                         "VALUES (?, 60, 1, 0, 10.0)", (base - 86400 + i * 60,))
        for i in range(10):
            conn.execute("INSERT INTO samples(ts, pct, on_battery, is_charging, watts) "
                         "VALUES (?, 50, 1, 0, 25.0)", (base + i * 60,))
        p = optimize.power_context(conn, base + 9 * 60)
        self.assertEqual(p, {"now_w": 25.0, "usual_w": 10.0})

    def test_power_unknown_on_ac(self):
        conn = fresh()
        conn.execute("INSERT INTO samples(ts, pct, on_battery, is_charging, watts) "
                     "VALUES (1800000000, 80, 0, 1, NULL)")
        self.assertIsNone(optimize.power_context(conn, 1_800_000_000))


class ApplyTest(unittest.TestCase):
    def test_admin_script_boots_out_system_service(self):
        s = admin_script([("bootout", "system", "com.raon.touchen.nxkey"),
                          ("kill", 4321, "/opt/x y/it's")])
        self.assertIn("/bin/launchctl bootout system/com.raon.touchen.nxkey", s)
        self.assertIn("/bin/kill -KILL 4321", s)
        # 암호 입력하는 사이 pid 가 다른 프로세스로 재사용됐으면 root 가 엉뚱한 걸 죽이지 않게
        self.assertIn("/bin/ps -p 4321 -o comm=", s)
        self.assertIn("'/opt/x y/it'\"'\"'s'", s)

    def test_admin_kill_guard_really_protects_reused_pid(self):
        import subprocess
        sleeper = subprocess.Popen(["/bin/sleep", "30"])
        try:
            script = admin_script([("kill", sleeper.pid, "/usr/bin/not-sleep")])
            subprocess.run(["/bin/sh", "-c", script], timeout=10)
            self.assertIsNone(sleeper.poll(), "exe 가 다른데 죽임")
            script = admin_script([("kill", sleeper.pid, "/bin/sleep")])
            subprocess.run(["/bin/sh", "-c", script], timeout=10)
            self.assertIsNotNone(sleeper.wait(timeout=5))
        finally:
            if sleeper.poll() is None:
                sleeper.kill()

    def test_admin_script_rejects_unsafe_label(self):
        with self.assertRaises(ValueError):
            admin_script([("bootout", "system", "x; rm -rf /")])

    def test_apply_skips_when_pid_now_belongs_to_other_program(self):
        calls = []
        msg = apply_items(
            [{"pid": 777, "exe": PREVIEW, "comm": "Preview"}], MY_UID,
            ps_one=lambda pid: {"uid": MY_UID, "exe": "/usr/bin/other"},
            job_finder=no_job, kill=lambda pid, sig: calls.append((pid, sig)),
            run_admin=lambda script: calls.append(script) or (True, ""),
            launchctl=lambda args: calls.append(args) or True,
            sleep=lambda s: None, pids_of=lambda exe: [])
        self.assertEqual(calls, [])
        self.assertIn("건너뜀", msg)

    def test_apply_uses_one_admin_prompt_for_root_service(self):
        scripts = []
        state = {"alive": True}

        def ps_one(pid):
            return {"uid": 0, "exe": TEK} if state["alive"] else None

        def run_admin(script):
            scripts.append(script)
            state["alive"] = False
            return True, ""

        msg = apply_items(
            [{"pid": 6451, "exe": TEK, "comm": "TEK_Daemon"}], MY_UID,
            ps_one=ps_one,
            job_finder=lambda e, p, u: {"domain": "system", "label": "com.raon.touchen.nxkey"},
            kill=lambda pid, sig: self.fail("root 프로세스를 권한 없이 kill 시도"),
            run_admin=run_admin, launchctl=lambda args: True, sleep=lambda s: None,
            pids_of=lambda exe: [])
        self.assertEqual(len(scripts), 1)
        self.assertIn("bootout system/com.raon.touchen.nxkey", scripts[0])
        self.assertIn("TEK_Daemon", msg)
        self.assertIn("중지", msg)

    def test_apply_reports_cancelled_password_honestly(self):
        msg = apply_items(
            [{"pid": 6451, "exe": TEK, "comm": "TEK_Daemon"}], MY_UID,
            ps_one=lambda pid: {"uid": 0, "exe": TEK},
            job_finder=lambda e, p, u: {"domain": "system", "label": "com.raon.touchen.nxkey"},
            kill=lambda pid, sig: None,
            run_admin=lambda script: (False, "User canceled. (-128)"),
            launchctl=lambda args: True, sleep=lambda s: None,
            pids_of=lambda exe: [])
        self.assertIn("취소", msg)
        self.assertNotIn("중지됨", msg)


class LaunchdTest(unittest.TestCase):
    def test_find_job_matches_program_and_running_pid(self):
        with tempfile.TemporaryDirectory() as d:
            daemons = os.path.join(d, "LaunchDaemons")
            os.mkdir(daemons)
            with open(os.path.join(daemons, "com.raon.touchen.nxkey.plist"), "wb") as f:
                plistlib.dump({"Label": "com.raon.touchen.nxkey", "Program": TEK,
                               "KeepAlive": True}, f)
            printed = {"system/com.raon.touchen.nxkey": "\tstate = running\n\tpid = 6451\n"}
            job = optimize.find_launchd_job(
                TEK, 6451, 0, dirs=[(daemons, "system")],
                printer=lambda target: printed.get(target))
            self.assertEqual(job, {"domain": "system", "label": "com.raon.touchen.nxkey"})
            self.assertIsNone(optimize.find_launchd_job(
                TEK, 6454, 0, dirs=[(daemons, "system")],
                printer=lambda target: printed.get(target)))


if __name__ == "__main__":
    unittest.main()
