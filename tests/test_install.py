#!/usr/bin/python3
"""Tests for install.sh and uninstall.sh. Each test copies the working tree into a
temporary folder and runs the scripts there with a temporary HOME, fake claude,
terminal-notifier, and herdr-app, and launchd calls turned off, so nothing touches
the real ~/.claude, ~/.config, ~/Library, or launchd.
Run: /usr/bin/python3 -B tests/test_install.py"""
import fcntl
import glob
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = shutil.ignore_patterns(".git", ".superpowers", "__pycache__", ".DS_Store", "gpui-kit.d.ts", "jsconfig.json")
MARKER = ".installed-by-herdr-scheduled-jobs"

FAKE = "#!/bin/bash\nexit 0\n"
FAKE_HERDR = """#!/bin/bash
printf '%s\\n' "$*" >>"$FAKE_HERDR_LOG"
if [ "$1" = check ] && [ -n "$FAKE_HERDR_FAIL" ]; then echo "main.js: boom"; exit 1; fi
exit 0
"""


def sha256(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def read(p):
    with open(p, "rb") as f:
        return f.read()


class Install(unittest.TestCase):
    def setUp(self, home_name="home", clone="clone"):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.join(self.tmp, home_name)
        os.makedirs(self.home)
        self.clone = os.path.join(self.tmp, clone)
        shutil.copytree(REPO, self.clone, ignore=SKIP, symlinks=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("claude", FAKE), ("terminal-notifier", FAKE), ("herdr-app", FAKE_HERDR)):
            self.write_exe(os.path.join(self.bin, name), body)
        self.herdr_log = os.path.join(self.tmp, "herdr.log")
        self.env = {"HOME": self.home, "PATH": f"{self.bin}:/usr/bin:/bin", "SCHEDULED_JOBS_NO_LAUNCHD": "1",
                    "CLAUDE_BIN": os.path.join(self.bin, "claude"),
                    "NOTIFIER_BIN": os.path.join(self.bin, "terminal-notifier"),
                    "FAKE_HERDR_LOG": self.herdr_log}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    # ---------- helpers ----------

    @staticmethod
    def write_exe(p, body):
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)

    def run_script(self, name, *args, **env):
        e = dict(self.env)
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        cmd = ["/usr/bin/env", "-i"] + [f"{k}={v}" for k, v in e.items()] + ["/bin/bash", os.path.join(self.clone, name), *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        r.out = r.stdout + r.stderr
        return r

    def install(self, *args, **env):
        return self.run_script("install.sh", *args, **env)

    def uninstall(self, *args, **env):
        return self.run_script("uninstall.sh", *args, **env)

    @property
    def jobs_py(self):
        return os.path.join(self.clone, "skills", "scheduling-agent-jobs", "scripts", "jobs.py")

    @property
    def skill_link(self):
        return os.path.join(self.home, ".claude", "skills", "scheduling-agent-jobs")

    @property
    def mods(self):
        return os.path.join(self.home, ".config", "herdr-app", "mods")

    @property
    def mod(self):
        return os.path.join(self.mods, "schedules")

    def plist(self, slug="triage"):
        return os.path.join(self.home, "Library", "LaunchAgents", f"local.scheduled-jobs.{slug}.plist")

    def add_job(self, slug="triage", old_path="/old/jobs.py"):
        workdir = os.path.join(self.tmp, "work")
        os.makedirs(workdir, exist_ok=True)
        spec = {"name": "Morning triage", "slug": slug, "schedule": "0 5 * * *", "workdir": workdir,
                "prompt": "do it", "timeoutSeconds": 20}
        p = os.path.join(self.tmp, "spec.json")
        with open(p, "w") as f:
            json.dump(spec, f)
        env = dict(self.env)
        r = subprocess.run(["/usr/bin/python3", "-B", self.jobs_py, "add", p], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        if old_path:  # as if the job had been added from another copy of jobs.py
            with open(self.plist(slug), "rb") as f:
                plist = plistlib.load(f)
            plist["ProgramArguments"][1] = old_path
            with open(self.plist(slug), "wb") as f:
                plistlib.dump(plist, f)
        return workdir

    def plist_target(self, slug="triage"):
        with open(self.plist(slug), "rb") as f:
            return plistlib.load(f)["ProgramArguments"][1]

    def jobs_path(self):
        with open(os.path.join(self.home, ".config", "scheduled-jobs", "jobs-path")) as f:
            return f.read().strip()

    def assert_ok(self, r, code=0):
        self.assertEqual(r.returncode, code, r.out)

    def assert_mod_matches_clone(self):
        for name in ("main.js", "mod.json"):
            self.assertEqual(read(os.path.join(self.mod, name)), read(os.path.join(self.clone, "mods", "schedules", name)))
        with open(os.path.join(self.mod, MARKER)) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines[0], "commit unknown")  # the copied tree has no .git
        self.assertEqual(sorted(lines[1:]), sorted(f"{sha256(os.path.join(self.mod, n))}  {n}" for n in ("main.js", "mod.json")))

    def snapshot(self, folder):
        out = {}
        for root, _, files in os.walk(folder):
            for n in files:
                p = os.path.join(root, n)
                out[os.path.relpath(p, folder)] = read(p)
        return out

    def backups(self, folder):
        return glob.glob(os.path.join(folder, "*.bak*"))

    @property
    def backup_root(self):
        return os.path.join(self.home, ".config", "herdr-scheduled-jobs", "backups")

    def assert_no_backups_in_live_folders(self):
        self.assertEqual(self.backups(os.path.dirname(self.skill_link)), [])
        self.assertEqual(self.backups(self.mods), [])

    # ---------- install.sh ----------

    def test_fresh_install(self):
        r = self.install()
        self.assert_ok(r)
        self.assertTrue(os.path.islink(self.skill_link))
        self.assertEqual(os.path.realpath(self.skill_link), os.path.join(self.clone, "skills", "scheduling-agent-jobs"))
        self.assert_mod_matches_clone()
        with open(self.herdr_log) as f:
            self.assertIn(f"check {self.mod}", f.read())
        self.assertEqual(os.path.realpath(self.jobs_path()), self.jobs_py)
        self.assertIn("No jobs to relink.", r.out)
        self.assertIn("smoke-test", r.out)
        self.assertIn("herdr-app do tab.new --name Schedules --mod schedules", r.out)

    def test_rerun_is_idempotent_and_updates_mod(self):
        self.assert_ok(self.install())
        # herdr-app check writes these into the mod folder; they aren't the installer's.
        for n in ("jsconfig.json", "gpui-kit.d.ts"):
            with open(os.path.join(self.mod, n), "w") as f:
                f.write("generated\n")
        with open(os.path.join(self.clone, "mods", "schedules", "main.js"), "a") as f:
            f.write("\n// a newer version\n")
        r = self.install()
        self.assert_ok(r)
        self.assertIn("skill: already linked", r.out)
        self.assert_mod_matches_clone()
        self.assertTrue(read(os.path.join(self.mod, "main.js")).endswith(b"// a newer version\n"))
        self.assertEqual(read(os.path.join(self.mod, "jsconfig.json")), b"generated\n")
        self.assert_no_backups_in_live_folders()
        self.assertFalse(os.path.exists(self.backup_root))

    def test_foreign_skill_folder_stops(self):
        os.makedirs(self.skill_link)
        with open(os.path.join(self.skill_link, "x"), "w") as f:
            f.write("mine\n")
        r = self.install()
        self.assert_ok(r, 2)
        self.assertIn("--replace-skill", r.out)
        self.assertFalse(os.path.islink(self.skill_link))
        self.assertEqual(os.listdir(self.skill_link), ["x"])
        self.assertEqual(read(os.path.join(self.skill_link, "x")), b"mine\n")
        # Nothing else changed either.
        self.assertFalse(os.path.exists(self.mods))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "scheduled-jobs")))

    def test_foreign_skill_link_stops(self):
        os.makedirs(os.path.dirname(self.skill_link))
        os.symlink("/elsewhere/scheduling-agent-jobs", self.skill_link)
        r = self.install()
        self.assert_ok(r, 2)
        self.assertIn("(/elsewhere/scheduling-agent-jobs)", r.out)
        self.assertEqual(os.readlink(self.skill_link), "/elsewhere/scheduling-agent-jobs")

    def test_replace_skill_backs_up(self):
        os.makedirs(self.skill_link)
        with open(os.path.join(self.skill_link, "x"), "w") as f:
            f.write("mine\n")
        r = self.install("--replace-skill")
        self.assert_ok(r)
        self.assertTrue(os.path.islink(self.skill_link))
        self.assertEqual(os.path.realpath(self.skill_link), os.path.join(self.clone, "skills", "scheduling-agent-jobs"))
        baks = glob.glob(os.path.join(self.backup_root, "[0-9]*-[0-9]*", "scheduling-agent-jobs"))
        self.assertEqual(len(baks), 1, baks)
        self.assertEqual(read(os.path.join(baks[0], "x")), b"mine\n")
        self.assertIn(baks[0], r.out)
        self.assert_no_backups_in_live_folders()

    def test_unmarked_mod_stops(self):
        os.makedirs(self.mod)
        for n in ("main.js", "mod.json"):
            with open(os.path.join(self.mod, n), "w") as f:
                f.write(f"my {n}\n")
        before = self.snapshot(self.mod)
        r = self.install()
        self.assert_ok(r, 2)
        self.assertIn("--replace-mod", r.out)
        self.assertEqual(self.snapshot(self.mod), before)
        self.assertFalse(os.path.lexists(self.skill_link))  # stopped before changing anything

    def test_edited_mod_stops(self):
        self.assert_ok(self.install())
        with open(os.path.join(self.mod, "main.js"), "a") as f:
            f.write("// my tweak\n")
        before = self.snapshot(self.mod)
        r = self.install()
        self.assert_ok(r, 2)
        self.assertIn("main.js", r.out)
        self.assertNotIn("mod.json", r.out)
        self.assertEqual(self.snapshot(self.mod), before)
        self.assert_no_backups_in_live_folders()
        self.assertFalse(os.path.exists(self.backup_root))

    def test_replace_mod_backs_up(self):
        os.makedirs(self.mod)
        with open(os.path.join(self.mod, "main.js"), "w") as f:
            f.write("mine\n")
        r = self.install("--replace-mod")
        self.assert_ok(r)
        baks = glob.glob(os.path.join(self.backup_root, "[0-9]*-[0-9]*", "schedules"))
        self.assertEqual(len(baks), 1, baks)
        self.assertEqual(read(os.path.join(baks[0], "main.js")), b"mine\n")
        self.assertIn(baks[0], r.out)
        self.assert_mod_matches_clone()
        self.assert_no_backups_in_live_folders()

    def test_replace_both_share_a_backup_folder(self):
        os.makedirs(self.skill_link)
        os.makedirs(self.mod)
        with open(os.path.join(self.mod, "main.js"), "w") as f:
            f.write("mine\n")
        r = self.install("--replace-skill", "--replace-mod")
        self.assert_ok(r)
        stamps = os.listdir(self.backup_root)
        self.assertEqual(len(stamps), 1, stamps)
        self.assertRegex(stamps[0], r"^[0-9]{8}-[0-9]{6}$")
        self.assertEqual(sorted(os.listdir(os.path.join(self.backup_root, stamps[0]))), ["schedules", "scheduling-agent-jobs"])
        self.assert_no_backups_in_live_folders()

    def test_clone_in_documents_refused(self):
        shutil.rmtree(self.tmp)
        self.setUp(clone=os.path.join("home", "Documents", "x"))
        r = self.install()
        self.assert_ok(r, 1)
        self.assertIn(f"install: this clone is in {os.path.join(self.home, 'Documents')}, which macOS doesn't let "
                      "scheduled jobs read; move it (for example to ~/git/herdr-scheduled-jobs) and run install.sh again", r.out)
        self.assertEqual(os.listdir(self.home), ["Documents"])
        self.assertEqual(os.listdir(os.path.join(self.home, "Documents")), ["x"])

    def test_missing_notifier_changes_nothing(self):
        r = self.install(NOTIFIER_BIN=os.path.join(self.tmp, "missing", "terminal-notifier"))
        self.assert_ok(r, 1)
        self.assertIn("brew install terminal-notifier", r.out)
        self.assertFalse(os.path.lexists(self.skill_link))
        self.assertEqual(os.listdir(self.home), [])

    def test_missing_claude_changes_nothing(self):
        r = self.install(CLAUDE_BIN=os.path.join(self.tmp, "missing", "claude"))
        self.assert_ok(r, 1)
        self.assertIn("https://docs.claude.com/en/docs/claude-code/setup", r.out)
        self.assertEqual(os.listdir(self.home), [])

    def test_no_skills_dir(self):
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude")))
        r = self.install()
        self.assert_ok(r)
        self.assertTrue(os.path.islink(self.skill_link))

    def test_no_herdr_skips_mod(self):
        os.remove(os.path.join(self.bin, "herdr-app"))
        r = self.install()
        self.assert_ok(r)
        self.assertIn("herdr: not installed", r.out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "herdr-app")))
        self.assertNotIn("tab.new", r.out)
        self.assertTrue(os.path.islink(self.skill_link))

    def test_no_mod_flag_skips_mod(self):
        r = self.install("--no-mod")
        self.assert_ok(r)
        self.assertIn("herdr: skipped (--no-mod)", r.out)
        self.assertFalse(os.path.exists(self.mods))
        self.assertFalse(os.path.exists(self.herdr_log))
        self.assertNotIn("tab.new", r.out)
        self.assertTrue(os.path.islink(self.skill_link))

    def test_herdr_folder_without_binary(self):
        os.remove(os.path.join(self.bin, "herdr-app"))
        os.makedirs(os.path.join(self.home, ".config", "herdr-app"))
        r = self.install()
        self.assert_ok(r)
        self.assert_mod_matches_clone()
        self.assertIn("herdr: herdr-app not found on PATH, so herdr-app check was skipped; run it yourself", r.out)
        self.assertFalse(os.path.exists(self.herdr_log))

    def test_herdr_app_bin_override(self):
        os.remove(os.path.join(self.bin, "herdr-app"))
        elsewhere = os.path.join(self.tmp, "apps", "herdr-app")
        os.makedirs(os.path.dirname(elsewhere))
        self.write_exe(elsewhere, FAKE_HERDR)
        r = self.install(HERDR_APP_BIN=elsewhere)
        self.assert_ok(r)
        with open(self.herdr_log) as f:
            self.assertIn(f"check {self.mod}", f.read())

    def test_failed_mod_check_reports(self):
        self.add_job()
        r = self.install(FAKE_HERDR_FAIL="1")
        self.assert_ok(r, 4)
        self.assertIn("main.js: boom", r.out)
        self.assertEqual(self.plist_target(), self.jobs_py)  # later steps still ran

    def test_path_with_spaces(self):
        shutil.rmtree(self.tmp)
        self.setUp(home_name="Home Dir", clone=os.path.join("My Repos", "hsj"))
        self.add_job()
        r = self.install()
        self.assert_ok(r)
        repos = os.path.join(self.tmp, "My Repos") + os.sep
        self.assertTrue(os.path.realpath(self.skill_link).startswith(repos))
        self.assertEqual(os.path.realpath(self.skill_link), os.path.join(self.clone, "skills", "scheduling-agent-jobs"))
        self.assertEqual(self.jobs_path(), self.jobs_py)
        self.assertTrue(self.plist_target().startswith(repos))
        self.assertEqual(self.plist_target(), self.jobs_py)
        self.assert_mod_matches_clone()

    def test_relinks_existing_job(self):
        self.add_job()
        self.assertEqual(self.plist_target(), "/old/jobs.py")
        r = self.install()
        self.assert_ok(r)
        self.assertEqual(self.plist_target(), self.jobs_py)
        self.assertIn(f"relinked triage (scheduled): /old/jobs.py -> {self.jobs_py}", r.out)

    def test_relink_failure_exits_3(self):
        workdir = self.add_job()
        os.rmdir(workdir)  # the job's spec no longer validates
        r = self.install()
        self.assert_ok(r, 3)
        self.assertIn("failed triage:", r.out)
        self.assertIn("relink", r.out)
        self.assertTrue(os.path.islink(self.skill_link))  # the other steps still ran
        self.assert_mod_matches_clone()

    def test_skipped_job_exits_5(self):
        self.add_job()
        lock = os.path.join(self.home, ".config", "scheduled-jobs", "locks", "triage.lock")
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = self.install()
        self.assert_ok(r, 5)
        self.assertIn("skipped triage: running now", r.out)
        self.assertIn("jobs: some jobs were running and weren't re-pointed; run ", r.out)
        self.assertIn("jobs.py relink after they finish", r.out)
        self.assertEqual(self.plist_target(), "/old/jobs.py")
        self.assertTrue(os.path.islink(self.skill_link))  # the other steps still ran
        self.assert_mod_matches_clone()

    # ---------- uninstall.sh ----------

    def test_uninstall_leaves_foreign_and_jobs(self):
        self.assert_ok(self.install())
        self.add_job(old_path=None)
        with open(os.path.join(self.mod, "main.js"), "a") as f:
            f.write("// my tweak\n")
        r = self.uninstall()
        self.assert_ok(r)
        self.assertFalse(os.path.lexists(self.skill_link))
        self.assertTrue(read(os.path.join(self.mod, "main.js")).endswith(b"// my tweak\n"))  # edited: left alone
        self.assertIn("main.js", r.out)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config", "scheduled-jobs", "jobs", "triage.json")))
        self.assertTrue(os.path.exists(self.plist()))
        self.assertIn("jobs.py remove triage", r.out)

    def test_uninstall_leaves_foreign_skill(self):
        os.makedirs(self.skill_link)
        r = self.uninstall()
        self.assert_ok(r)
        self.assertTrue(os.path.isdir(self.skill_link))

    def test_uninstall_removes_clean_mod(self):
        self.assert_ok(self.install())
        for n in ("jsconfig.json", "gpui-kit.d.ts"):  # what herdr-app check leaves behind
            with open(os.path.join(self.mod, n), "w") as f:
                f.write("generated\n")
        r = self.uninstall()
        self.assert_ok(r)
        self.assertFalse(os.path.exists(self.mod))
        self.assertTrue(os.path.isdir(self.mods))

    def test_uninstall_keeps_unknown_files_in_mod(self):
        self.assert_ok(self.install())
        with open(os.path.join(self.mod, "notes.txt"), "w") as f:
            f.write("mine\n")
        r = self.uninstall()
        self.assert_ok(r)
        self.assertEqual(os.listdir(self.mod), ["notes.txt"])
        self.assertIn("notes.txt", r.out)

    def test_uninstall_remove_jobs(self):
        self.assert_ok(self.install())
        self.add_job(old_path=None)
        r = self.uninstall("--remove-jobs")
        self.assert_ok(r)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config", "scheduled-jobs", "jobs", "triage.json")))
        self.assertFalse(os.path.exists(self.plist()))
        self.assertIn("Removed triage", r.out)


if __name__ == "__main__":
    unittest.main()
