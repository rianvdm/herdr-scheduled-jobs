#!/usr/bin/python3
"""Tests for jobs.py. Nothing real runs: launchd calls are off, and Claude Code and
terminal-notifier are fakes. Run: /usr/bin/python3 tests/test_jobs.py"""
import datetime as dt
import fcntl
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JOBS = os.path.join(HERE, "..", "scripts", "jobs.py")
spec = importlib.util.spec_from_file_location("jobs", JOBS)
jobs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jobs)

FAKE_CLAUDE = r"""#!/bin/bash
echo '{"type":"system","subtype":"init","session_id":"sess-1","model":"fake","mcp_servers":[{"name":"notes-server","status":"failed"}]}'
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"gh auth status"}}]}}'
case "$FAKE_MODE" in
  write) sleep 1; echo '**7 emails marked as read.**' >"$FAKE_OUT"
         echo '{"type":"result","subtype":"success","is_error":false,"num_turns":3,"total_cost_usd":0.12,"result":"Done.","session_id":"sess-1"}' ;;
  noop)  echo '{"type":"result","subtype":"success","is_error":false,"num_turns":1,"result":"Auth failed, stopping.","session_id":"sess-1"}' ;;
  fail)  exit 3 ;;
  partial) echo '**3 emails marked as read.**' >"$FAKE_OUT"
           echo '{"type":"result","subtype":"success","is_error":false,"num_turns":4,"result":"Wrote the file.\nPARTIAL: 2 threads failed to mark as read","session_id":"sess-1"}' ;;
  agent) echo '{"type":"result","subtype":"error_max_turns","is_error":true,"num_turns":9,"session_id":"sess-1"}' ;;
  hang)  sleep 30 ;;
esac
"""

FAKE_NOTIFIER = """#!/bin/bash
printf '%s\\n' "$*" >>"$FAKE_NOTIFIED"
echo @CLOSED
"""


class Cron(unittest.TestCase):
    def test_next_run_daily(self):
        self.assertEqual(jobs.next_run("0 5 * * *", dt.datetime(2026, 11, 16, 6, 0)), dt.datetime(2026, 11, 17, 5, 0))
        self.assertEqual(jobs.next_run("0 5 * * *", dt.datetime(2026, 11, 16, 4, 59, 30)), dt.datetime(2026, 11, 16, 5, 0))

    def test_next_run_is_strictly_after(self):
        self.assertEqual(jobs.next_run("30 5,14 * * *", dt.datetime(2026, 11, 16, 5, 30)), dt.datetime(2026, 11, 16, 14, 30))

    def test_weekdays_and_sunday_as_7(self):
        # 2026-11-20 is a Friday.
        self.assertEqual(jobs.next_run("0 16 * * 5", dt.datetime(2026, 11, 16)), dt.datetime(2026, 11, 20, 16, 0))
        self.assertEqual(jobs.next_run("0 9 * * 7", dt.datetime(2026, 11, 16)), dt.datetime(2026, 11, 22, 9, 0))

    def test_day_or_weekday(self):
        # Both restricted: the 1st of the month OR any Monday (cron's rule).
        self.assertEqual(jobs.next_run("0 9 1 * 1", dt.datetime(2026, 11, 16, 10)), dt.datetime(2026, 11, 23, 9, 0))
        cals = jobs.launchd_calendars("0 9 1 * 1")
        self.assertIn({"Minute": 0, "Hour": 9, "Day": 1}, cals)
        self.assertIn({"Minute": 0, "Hour": 9, "Weekday": 1}, cals)
        self.assertEqual(len(cals), 2)

    def test_calendars(self):
        self.assertEqual(jobs.launchd_calendars("0 5 * * *"), [{"Minute": 0, "Hour": 5}])
        self.assertEqual(len(jobs.launchd_calendars("30 5,14 * * *")), 2)
        self.assertEqual(jobs.launchd_calendars("*/15 * * * *"), [{"Minute": m} for m in (0, 15, 30, 45)])
        self.assertEqual(jobs.launchd_calendars("0 */12 * * *"), [{"Minute": 0, "Hour": 0}, {"Minute": 0, "Hour": 12}])
        self.assertEqual(jobs.launchd_calendars("* * * * *"), [{}])
        self.assertEqual(jobs.next_run("*/15 * * * *", dt.datetime(2026, 11, 16, 6, 1)), dt.datetime(2026, 11, 16, 6, 15))
        self.assertEqual(len(jobs.launchd_calendars("0 9 * * 1-5")), 5)

    def test_bad_cron(self):
        for bad in ("0 5 * *", "60 5 * * *", "0 5 * * mon", "*/0 * * * *", "5-1 * * * *"):
            with self.assertRaises(ValueError, msg=bad):
                jobs.launchd_calendars(bad)

    def test_describe(self):
        self.assertEqual(jobs.describe("0 5 * * *"), "daily at 5am")
        self.assertEqual(jobs.describe("30 5,14 * * *"), "daily at 5:30am, 2:30pm")
        self.assertEqual(jobs.describe("0 16 * * 5"), "Fri at 4pm")
        self.assertEqual(jobs.describe("0 9 * * 1-5"), "weekdays at 9am")
        self.assertEqual(jobs.describe("*/15 * * * *"), "*/15 * * * *")


class Outcome(unittest.TestCase):
    def job(self, **over):
        j = {"name": "Triage", "slug": "triage", "countRegex": r"[0-9]+ items?", "countNoun": "items"}
        j.update(over)
        return j

    def test_texts(self):
        log = jobs.open_action(jobs.path("log", "triage"))
        self.assertEqual(jobs.run_outcome(self.job(), "failed", "exit code 3", None),
                         {"title": "Triage failed", "message": "Exit code 3.", "action": log,
                          "hint": "Click to see the log."})
        self.assertEqual(jobs.run_outcome(self.job(), "timeout", "Timed out after 1s", None)["title"],
                         "Triage timed out")
        self.assertEqual(jobs.run_outcome(self.job(), "stopped", "Stopped", None)["message"],
                         "The run was stopped.")
        self.assertEqual(jobs.run_outcome(self.job(countRegex=None), "success", None, None),
                         {"title": "Triage done", "message": "Finished.", "action": log,
                          "hint": "Click to see the log."})

    def test_count_and_open_target(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("**4 items** today\n")
        self.addCleanup(os.remove, f.name)
        o = jobs.run_outcome(self.job(), "success", None, f.name)
        self.assertEqual((o["title"], o["message"], o["hint"]), ("Triage ready", "4 items.", "Click to open it."))
        self.assertEqual(o["action"], ["/usr/bin/open", "-t", f.name])

    def test_unreadable_count_file_falls_back(self):
        o = jobs.run_outcome(self.job(), "success", None, "/no/such/report.md")
        self.assertEqual((o["title"], o["message"]), ("Triage ready", "Finished."))


class Exec(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name, body in (("claude", FAKE_CLAUDE), ("notifier", FAKE_NOTIFIER)):
            p = os.path.join(self.tmp, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, 0o755)
        self.out = os.path.join(self.tmp, "out.md")
        with open(self.out, "w") as f:
            f.write("old\n")
        os.utime(self.out, (0, 1_700_000_000))
        self.notified = os.path.join(self.tmp, "notified")
        self.env = dict(os.environ, SCHEDULED_JOBS_HOME=os.path.join(self.tmp, "home"),
                        SCHEDULED_JOBS_LAUNCH_AGENTS=os.path.join(self.tmp, "agents"),
                        SCHEDULED_JOBS_NO_LAUNCHD="1", CLAUDE_BIN=os.path.join(self.tmp, "claude"),
                        NOTIFIER_BIN=os.path.join(self.tmp, "notifier"), FAKE_OUT=self.out,
                        FAKE_NOTIFIED=self.notified)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def jobs(self, *args, mode="write"):
        return subprocess.run(["/usr/bin/python3", JOBS, *args], env=dict(self.env, FAKE_MODE=mode),
                              capture_output=True, text=True, timeout=60)

    def add(self, **over):
        spec = {"name": "Morning triage", "slug": "triage", "schedule": "0 5 * * *", "workdir": self.tmp,
                "prompt": "do it", "timeoutSeconds": 20, "label": "Triage", "expectedFile": self.out,
                "open": ["/usr/bin/true", "clicked"], "countRegex": r"[0-9]+ emails? marked as read",
                "countNoun": "emails triaged"}
        spec.update(over)
        spec = {k: v for k, v in spec.items() if v is not None}
        p = os.path.join(self.tmp, "spec.json")
        with open(p, "w") as f:
            json.dump(spec, f)
        r = self.jobs("add", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def notification(self):
        for _ in range(30):
            if os.path.exists(self.notified) and os.path.getsize(self.notified):
                break
            time.sleep(0.1)
        with open(self.notified) as f:
            return f.read()

    def last_run(self):
        with open(os.path.join(self.tmp, "home", "runs", "triage.jsonl")) as f:
            return json.loads(f.read().splitlines()[-1])

    def log(self):
        with open(os.path.join(self.tmp, "home", "logs", "triage.log")) as f:
            return f.read()

    def plist_path(self):
        return os.path.join(self.tmp, "agents", "local.scheduled-jobs.triage.plist")

    def test_relink_repoints_plists(self):
        self.add()
        with open(self.plist_path(), "rb") as f:
            plist = plistlib.load(f)
        plist["ProgramArguments"][1] = "/old/jobs.py"
        with open(self.plist_path(), "wb") as f:
            plistlib.dump(plist, f)
        r = self.jobs("relink")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.plist_path(), "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"][1], os.path.realpath(JOBS))
        self.assertIn("relinked triage (scheduled): /old/jobs.py", r.stdout)

    def test_relink_keeps_paused(self):
        self.add(paused=True)
        r = self.jobs("relink")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.plist_path()))
        self.assertIn("relinked triage (paused)", r.stdout)

    def test_relink_skips_running_job(self):
        self.add()
        with open(self.plist_path(), "rb") as f:
            before = f.read()
        lock = os.path.join(self.tmp, "home", "locks", "triage.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = self.jobs("relink")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("skipped triage: running now", r.stdout)
        with open(self.plist_path(), "rb") as f:
            self.assertEqual(f.read(), before)

    def protected_home(self):
        home = os.path.join(self.tmp, "fakehome")
        work = os.path.join(home, "Documents", "proj")
        os.makedirs(work)
        self.env["HOME"] = home
        return work

    def test_add_refuses_workdir_in_documents(self):
        work = self.protected_home()
        p = os.path.join(self.tmp, "spec.json")
        with open(p, "w") as f:
            json.dump({"name": "Morning triage", "slug": "triage", "schedule": "0 5 * * *", "workdir": work,
                       "prompt": "do it", "timeoutSeconds": 20}, f)
        r = self.jobs("add", p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Documents", r.stderr)
        self.assertIn("doesn't let scheduled jobs read", r.stderr)

    def test_documents_prefix_lookalike_is_allowed(self):
        work = self.protected_home()
        other = work.replace("Documents", "Documents-old")
        os.makedirs(other)
        self.add(workdir=other)

    def test_relink_reports_protected_workdir_as_failed(self):
        self.add(slug="aaa-bad")
        self.add(slug="bbb-good")
        work = self.protected_home()
        home = os.path.join(self.tmp, "home")
        bad = os.path.join(home, "jobs", "aaa-bad.json")
        with open(bad) as f:
            spec = json.load(f)
        spec["workdir"] = work
        with open(bad, "w") as f:
            json.dump(spec, f)
        r = self.jobs("relink")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("failed aaa-bad:", r.stdout)
        self.assertIn("Documents", r.stdout)
        self.assertIn("relinked bbb-good", r.stdout)

    def test_relink_continues_past_failures(self):
        self.add(slug="aaa-bad")
        self.add(slug="bbb-busy")
        self.add(slug="ccc-good")
        home = os.path.join(self.tmp, "home")
        bad = os.path.join(home, "jobs", "aaa-bad.json")
        with open(bad) as f:
            spec = json.load(f)
        spec["workdir"] = os.path.join(self.tmp, "gone")
        with open(bad, "w") as f:
            json.dump(spec, f)
        with open(os.path.join(home, "jobs", "ddd-garbled.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(home, "locks", "bbb-busy.lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = self.jobs("relink")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("failed aaa-bad: workdir does not exist", r.stdout)
        self.assertIn("skipped bbb-busy: running now", r.stdout)
        self.assertIn("relinked ccc-good (scheduled):", r.stdout)
        self.assertIn("failed ddd-garbled:", r.stdout)
        with open(os.path.join(self.tmp, "agents", "local.scheduled-jobs.ccc-good.plist"), "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"][1], os.path.realpath(JOBS))

    def test_relink_no_jobs_writes_jobs_path(self):
        r = self.jobs("relink")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "No jobs to relink.")
        with open(os.path.join(self.tmp, "home", "jobs-path")) as f:
            self.assertEqual(f.read().strip(), os.path.realpath(JOBS))

    def test_add_writes_plist(self):
        self.add()
        with open(os.path.join(self.tmp, "agents", "local.scheduled-jobs.triage.plist"), "rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(plist["ProgramArguments"][1:], [os.path.realpath(JOBS), "exec", "triage"])
        self.assertEqual(plist["StartCalendarInterval"], {"Minute": 0, "Hour": 5})
        self.assertEqual(plist["WorkingDirectory"], self.tmp)

    def test_success(self):
        self.add()
        r = self.jobs("exec", "triage")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Triage ready -message 7 emails triaged.", self.notification())
        run = self.last_run()
        self.assertEqual((run["status"], run["sessionId"], run["costUsd"]), ("success", "sess-1", 0.12))
        log = self.log()
        self.assertIn("→ Bash: gh auth status", log)
        self.assertIn("MCP server notes-server: failed", log)
        self.assertIn("claude --resume sess-1", log)

    def test_click_runs_open_action_without_waiting(self):
        # A waiting notifier (-action) quits as the click arrives, so macOS shows "not open anymore".
        # -execute lets macOS relaunch it on click instead.
        self.add(open=["/usr/bin/true", "two words"])
        self.jobs("exec", "triage")
        sent = self.notification()
        self.assertIn("-execute /usr/bin/true 'two words'", sent)
        self.assertNotIn("-action", sent)

    def test_exit_zero_but_file_untouched(self):
        self.add()
        r = self.jobs("exec", "triage", mode="noop")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Triage failed -message Out.md not updated.", self.notification())
        self.assertEqual(self.last_run()["status"], "failed")

    def test_nonzero_exit(self):
        self.add()
        r = self.jobs("exec", "triage", mode="fail")
        self.assertEqual(r.returncode, 3)
        self.assertIn("Triage failed -message Exit code 3", self.notification())

    def test_missing_binary_is_recorded_and_notified(self):
        self.add()
        missing = os.path.join(self.tmp, "no-such-claude")
        self.env["CLAUDE_BIN"] = missing
        r = self.jobs("exec", "triage")
        self.assertNotIn("NameError", r.stderr)
        self.assertEqual(self.last_run()["status"], "failed")
        self.assertIn(f"could not start {missing}", self.log())
        self.assertIn("Triage failed", self.notification())
        self.assertIn("Couldn't start the agent (no-such-claude)", self.notification())

    def test_agent_error(self):
        self.add(expectedFile=None, open=None, countRegex=None)
        r = self.jobs("exec", "triage", mode="agent")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Agent error: error_max_turns", self.notification())

    def test_timeout(self):
        self.add(timeoutSeconds=1)
        start = time.monotonic()
        r = self.jobs("exec", "triage", mode="hang")
        self.assertLess(time.monotonic() - start, 15)
        self.assertEqual(r.returncode, 124)
        self.assertIn("Triage timed out", self.notification())
        self.assertEqual(self.last_run()["status"], "timeout")

    def test_no_expected_file(self):
        self.add(expectedFile=None, open=None, countRegex=None)
        r = self.jobs("exec", "triage", mode="noop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Triage done -message Finished.", self.notification())

    def test_overlapping_run_is_skipped(self):
        self.add()
        lock = os.path.join(self.tmp, "home", "locks", "triage.lock")
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = self.jobs("exec", "triage")
            listed = json.loads(self.jobs("list", "--json").stdout)["jobs"][0]
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.last_run()["status"], "skipped")
        self.assertTrue(listed["running"])
        self.assertFalse(os.path.exists(self.notified))

    def test_pause_resume_and_list(self):
        self.add()
        self.assertEqual(self.jobs("pause", "triage").returncode, 0)
        row = json.loads(self.jobs("list", "--json").stdout)["jobs"][0]
        self.assertTrue(row["paused"])
        self.assertIsNone(row["nextRunAt"])
        self.assertEqual(self.jobs("resume", "triage").returncode, 0)
        row = json.loads(self.jobs("list", "--json").stdout)["jobs"][0]
        self.assertFalse(row["paused"])
        self.assertEqual(row["scheduleText"], "daily at 5am")
        self.assertTrue(row["nextRunAt"])

    def test_manual_run_records_source(self):
        self.add()
        self.assertEqual(self.jobs("run", "triage").returncode, 0)
        self.assertIn("Triage ready", self.notification())
        self.assertEqual(self.last_run()["source"], "manual")

    def test_paths_resolve_through_symlinks(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(os.path.realpath(os.path.join(HERE, "..")), link)
        spec_path = os.path.join(self.tmp, "spec.json")
        with open(spec_path, "w") as f:
            json.dump({"name": "Morning triage", "slug": "triage", "schedule": "0 5 * * *",
                       "workdir": self.tmp, "prompt": "do it", "timeoutSeconds": 20}, f)
        r = subprocess.run(["/usr/bin/python3", os.path.join(link, "scripts", "jobs.py"), "add", spec_path],
                           env=dict(self.env, FAKE_MODE="write"), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(os.path.join(self.tmp, "agents", "local.scheduled-jobs.triage.plist"), "rb") as f:
            plist = plistlib.load(f)
        self.assertEqual(plist["ProgramArguments"][1], os.path.realpath(JOBS))
        with open(os.path.join(self.tmp, "home", "jobs-path")) as f:
            self.assertEqual(f.read().strip(), os.path.realpath(JOBS))

    def test_records_claude_path_and_self_location(self):
        self.add()
        with open(os.path.join(self.tmp, "home", "jobs", "triage.json")) as f:
            self.assertEqual(json.load(f)["claudePath"], os.path.join(self.tmp, "claude"))
        with open(os.path.join(self.tmp, "home", "jobs-path")) as f:
            self.assertEqual(f.read().strip(), os.path.realpath(JOBS))

    def test_open_actions(self):
        self.assertEqual(jobs.open_action(["notes-app", "open"]), ["notes-app", "open"])
        self.assertEqual(jobs.open_action("notes://open?file=x"), ["/usr/bin/open", "notes://open?file=x"])
        self.assertEqual(jobs.open_action("/tmp/x.md"), ["/usr/bin/open", "-t", "/tmp/x.md"])

    def test_home_paths_expand(self):
        self.add(expectedFile="~/nowhere/out.md")
        with open(os.path.join(self.tmp, "home", "jobs", "triage.json")) as f:
            self.assertEqual(json.load(f)["expectedFile"], os.path.expanduser("~/nowhere/out.md"))

    def test_smoke_test_runs_and_cleans_up(self):
        out = os.path.join(self.tmp, "home", "smoke-test.md")
        r = subprocess.run(["/usr/bin/python3", JOBS, "smoke-test", "--workdir", self.tmp],
                           env=dict(self.env, FAKE_MODE="write", FAKE_OUT=out), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Result: success", r.stdout)
        self.assertIn("7 emails marked as read", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "home", "jobs", "smoke-test.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "agents", "local.scheduled-jobs.smoke-test.plist")))

    def test_smoke_test_fits_in_ten_minutes(self):
        # An agent runs the smoke test in a Bash call capped at 10 minutes.
        self.assertLessEqual(jobs.SMOKE_WAIT_SECONDS, 9 * 60)
        # The wait outlasts the job's timeout plus the kill grace, so a timed-out run still reports.
        self.assertLess(jobs.SMOKE_TIMEOUT_SECONDS + jobs.KILL_GRACE_SECONDS, jobs.SMOKE_WAIT_SECONDS)

    def test_smoke_prompt_calls_claude_by_absolute_path(self):
        p = jobs.smoke_prompt("/tmp/out.md", "/opt/My Tools/claude")
        self.assertIn("Run `'/opt/My Tools/claude' mcp list`", p)
        self.assertIn("Overwrite /tmp/out.md", p)
        self.assertIn("gh auth status", p)

    def test_paused_jobs_have_no_plist(self):
        plist = os.path.join(self.tmp, "agents", "local.scheduled-jobs.triage.plist")
        self.add(paused=True)
        self.assertFalse(os.path.exists(plist))
        self.assertEqual(self.jobs("resume", "triage").returncode, 0)
        self.assertTrue(os.path.exists(plist))
        self.assertEqual(self.jobs("pause", "triage").returncode, 0)
        self.assertFalse(os.path.exists(plist))

    def test_scheduled_run_of_paused_job_is_skipped(self):
        self.add(paused=True)
        r = self.jobs("exec", "triage")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.last_run()["error"], "paused")
        self.assertFalse(os.path.exists(self.notified))

    def test_manual_run_of_paused_job_runs(self):
        self.add(paused=True)
        self.assertEqual(self.jobs("run", "triage").returncode, 0)
        self.assertIn("Triage ready", self.notification())
        self.assertEqual(self.last_run()["status"], "success")

    def test_partial_marker_fails_the_run(self):
        self.add()
        r = self.jobs("exec", "triage", mode="partial")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Partly done: 2 threads failed to mark as read", self.notification())
        self.assertEqual(self.last_run()["status"], "failed")

    def test_fake_run_of_paused_job_with_manual_flag(self):
        self.add(paused=True)
        r = self.jobs("exec", "triage", "--manual", mode="noop")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Triage failed -message Out.md not updated.", self.notification())
        self.assertEqual(self.last_run()["source"], "manual")

    def test_bad_spec_rejected(self):
        p = os.path.join(self.tmp, "bad.json")
        with open(p, "w") as f:
            json.dump({"name": "x", "slug": "Bad Slug", "schedule": "0 5 * * *", "workdir": self.tmp,
                       "prompt": "p", "timeoutSeconds": 5}, f)
        r = self.jobs("add", p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("slug", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
