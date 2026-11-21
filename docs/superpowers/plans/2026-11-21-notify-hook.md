# Notify Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a job spec name a `notify` command that `jobs.py` runs after each finished run instead of terminal-notifier, and ship a Postmark sender that emails the run's output or error.

**Architecture:** `cmd_exec` keeps appending the run record as it does today, then builds an outcome (`run_outcome()`) and hands it to `deliver()`. That calls the job's hook with a JSON payload on stdin, or terminal-notifier, and falls back to terminal-notifier when the hook fails. The Postmark sender is a separate stdlib script that reads that payload.

**Tech Stack:** Python 3.9 stdlib (`/usr/bin/python3`), unittest, launchd (off in tests).

**Spec:** `docs/superpowers/specs/2026-11-21-notify-hook-design.md`

## Global Constraints

- `jobs.py`, `notify_postmark.py`, and their tests use the stdlib only and run on `/usr/bin/python3` (3.9.6): no `match`, no `X | Y` type hints, and no third-party imports.
- Tests that touch jobs set `SCHEDULED_JOBS_NO_LAUNCHD=1` and use a temporary `HOME` or `SCHEDULED_JOBS_HOME`. No test may load anything into the real launchd domain. Sender tests talk only to a local fake server.
- The run record and the terminal-notifier text for jobs without `notify` stay exactly as they are today. Every existing test in `test_jobs.py` must keep passing unchanged.
- The spec and the hook's arguments never hold a secret. Neither `jobs.py` nor the sender may write the Postmark token to stdout, stderr, or the log.
- Use the Oxford comma. Commits carry no attribution trailers.
- **Gate for every commit:**
  1. Run `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`, and add `&& /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py` once that file exists. All must exit 0.
  2. Run `find . -name __pycache__ -prune -exec rm -rf {} +`.
  3. Stage the task's files.
  4. Run `git diff --cached | grep -nE "/Users/|$(id -un)" || echo clean` and read the staged diff for private project names, internal domains, and email addresses other than `example.com`. Fix anything found before committing.

All paths below are relative to the repo root, `herdr-scheduled-jobs/`. Work on the `notify-hook` branch. `JOBS_PY` means `skills/scheduling-agent-jobs/scripts/jobs.py`, and `TEST_JOBS` means `skills/scheduling-agent-jobs/tests/test_jobs.py`.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `JOBS_PY` | Modify | `notify` validation, `run_outcome()`, `deliver()`, `run_tail()`, `build_payload()`, and `run_hook()` |
| `TEST_JOBS` | Modify | Outcome, tail, validation, hook, and fallback tests |
| `skills/scheduling-agent-jobs/scripts/notify_postmark.py` | Create | Reads the payload, builds the email, and sends it through Postmark |
| `skills/scheduling-agent-jobs/tests/test_notify_postmark.py` | Create | Tests the sender against a local fake Postmark |
| `README.md`, `skills/scheduling-agent-jobs/SKILL.md`, `AGENTS.md` | Modify | Docs and the pre-commit test list |

---

### Task 1: Separate the outcome from its delivery

This is a refactor plus one fix: when the count can't be read, the message falls back to "Finished." instead of raising. The run record and the notification text don't change.

**Files:**
- Modify: `JOBS_PY`, adding functions after `notify()` and replacing the delivery block at the end of `cmd_exec()`.
- Test: `TEST_JOBS`

**Interfaces:**
- Produces:
  - `run_outcome(job: dict, status: str, error: str|None, expected: str|None) -> dict` with keys `title`, `message` (without the click hint), `action` (`list[str]`), and `hint`.
  - `deliver(job: dict, outcome: dict, payload: dict|None, tx: Transcript) -> None`. Task 3 extends its body, and its signature stays the same.

- [ ] **Step 1: Write the failing tests.** Add this class to `TEST_JOBS`, directly before `class Exec(unittest.TestCase):`:

```python
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
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py Outcome`
Expected: three errors with `AttributeError: module 'jobs' has no attribute 'run_outcome'`.

- [ ] **Step 3: Add `run_outcome()` and `deliver()`** directly after the `notify()` function in `JOBS_PY`:

```python
def run_outcome(job, status, error, expected):
    """What a finished run reports: a title, a message without the click hint, the click
    action, and the hint terminal-notifier adds."""
    label = job.get("label", job["name"])
    show_log = open_action(path("log", job["slug"]))
    if status in ("timeout", "stopped"):
        return {"title": f"{label} {'timed out' if status == 'timeout' else 'stopped'}",
                "message": "The run was stopped.", "action": show_log, "hint": "Click to see the log."}
    if status == "failed":
        return {"title": f"{label} failed", "message": f"{error[0].upper()}{error[1:]}.",
                "action": show_log, "hint": "Click to see the log."}
    message = "Finished."
    if job.get("countRegex") and expected:
        try:
            with open(expected, errors="replace") as f:
                m = re.search(job["countRegex"], f.read())
        except OSError:
            m = None
        n = re.search(r"\d+", m.group(0)) if m else None
        if n:
            message = f'{n.group(0)} {job.get("countNoun", "items")}.'
    target = job.get("open") or expected
    if target:
        return {"title": f"{label} ready", "message": message, "action": open_action(target),
                "hint": "Click to open it."}
    return {"title": f"{label} done", "message": message, "action": show_log, "hint": "Click to see the log."}


def deliver(job, outcome, payload, tx):
    """Tell the user how the run went."""
    notify(outcome["title"], f'{outcome["message"]} {outcome["hint"]}', "scheduled-" + job["slug"],
           outcome["action"], tx)
```

- [ ] **Step 4: Use them in `cmd_exec()`.** Replace everything from the line `label = job.get("label", job["name"])` through the `return code` at the end of `cmd_exec()` with:

```python
    outcome = run_outcome(job, status, error, expected)
    deliver(job, outcome, None, tx)
    return code
```

- [ ] **Step 5: Run the whole suite**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all pass, including every existing notification-text test.

- [ ] **Step 6: Commit,** after the Global Constraints gate.

```bash
git add skills/scheduling-agent-jobs/scripts/jobs.py skills/scheduling-agent-jobs/tests/test_jobs.py
git commit -m "Separate a run's outcome from its delivery"
```

---

### Task 2: Validate the `notify` spec field

**Files:**
- Modify: `JOBS_PY`, in `OPTIONAL` and `validate()`.
- Test: `TEST_JOBS`, in the fixtures at the top and `class Exec`.

**Interfaces:**
- Produces: a stored job's `job["notify"]` is exactly `{"command": [...]}`, a non-empty `list[str]` with each leading `~` expanded and no symlinks resolved. The first item is absolute. The file may not exist yet.
- Produces (tests): the `FAKE_HOOK` fixture and `self.hook` and `self.payload_path` on `Exec`, which Task 3 uses.

- [ ] **Step 1: Add the fake hook.** In `TEST_JOBS`, directly after the `FAKE_NOTIFIER = """…"""` block, add:

```python
FAKE_HOOK = """#!/bin/bash
cat >"$FAKE_HOOK_OUT"
echo "hook says hi"
if [ -n "${FAKE_HOOK_SLEEP:-}" ]; then exec /bin/sleep "$FAKE_HOOK_SLEEP"; fi
exit "${FAKE_HOOK_EXIT:-0}"
"""
```

- [ ] **Step 2: Extend `Exec.setUp()`.** Change its first line from `for name, body in (("claude", FAKE_CLAUDE), ("notifier", FAKE_NOTIFIER)):` to:

```python
        for name, body in (("claude", FAKE_CLAUDE), ("notifier", FAKE_NOTIFIER), ("hook", FAKE_HOOK)):
```

Then, directly after the closing `FAKE_NOTIFIED=self.notified)` line of the `self.env = dict(...)` call, add:

```python
        self.hook = os.path.join(self.tmp, "hook")
        self.payload_path = os.path.join(self.tmp, "hook-payload.json")
        self.env["FAKE_HOOK_OUT"] = self.payload_path
        for key in ("NOTIFY_HOOK_BIN", "SCHEDULED_JOBS_HOOK_TIMEOUT", "FAKE_HOOK_SLEEP", "FAKE_HOOK_EXIT"):
            self.env.pop(key, None)  # never inherit a real hook or test knobs from the shell
```

- [ ] **Step 3: Let `Exec.add()` return a refused add.** Change `def add(self, **over):` to `def add(self, check=True, **over):`, and replace its last three lines:

```python
        r = self.jobs("add", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r
```

with:

```python
        r = self.jobs("add", p)
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r
```

- [ ] **Step 4: Write the failing tests.** Add these to `class Exec`:

```python
    def test_notify_must_be_a_command_list(self):
        for bad in ({"command": []}, {"command": self.hook}, {"cmd": [self.hook]},
                    {"command": [self.hook], "extra": 1}, {"command": [self.hook, 3]}):
            r = self.add(check=False, notify=bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn('"notify" must be {"command"', r.stderr, bad)
        r = self.add(check=False, notify=[self.hook])
        self.assertEqual(r.returncode, 1)
        self.assertIn('"notify" must be a dict', r.stderr)

    def test_notify_command_must_start_with_an_absolute_path(self):
        r = self.add(check=False, notify={"command": ["hook"]})
        self.assertEqual(r.returncode, 1)
        self.assertIn('"notify" command must start with an absolute path', r.stderr)

    def test_missing_hook_file_is_accepted_at_add(self):
        self.add(notify={"command": [os.path.join(self.tmp, "not-yet")]})

    def test_notify_arguments_expand_home(self):
        self.env["HOME"] = self.tmp
        self.add(notify={"command": ["~/hook", "--env-file", "~/.env"]})
        with open(os.path.join(self.tmp, "home", "jobs", "triage.json")) as f:
            command = json.load(f)["notify"]["command"]
        self.assertEqual(command, [os.path.join(self.tmp, "hook"), "--env-file", os.path.join(self.tmp, ".env")])
```

- [ ] **Step 5: Run them and confirm they fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py Exec.test_notify_must_be_a_command_list Exec.test_notify_command_must_start_with_an_absolute_path Exec.test_missing_hook_file_is_accepted_at_add Exec.test_notify_arguments_expand_home`
Expected: all four fail. `jobs.py` reports `unknown field "notify"`, so the stderr assertions fail and the two adds that should succeed fail in `add()`.

- [ ] **Step 6: Implement.** In `JOBS_PY`, change `OPTIONAL` to:

```python
OPTIONAL = {"label": str, "expectedFile": str, "open": (str, list), "countRegex": str, "countNoun": str,
            "model": str, "paused": bool, "claudePath": str, "createdAt": str, "notify": dict}
```

In `validate()`, directly after the `if isinstance(spec.get("open"), list) and not all(...)` check and its `die(...)` line, add:

```python
    if "notify" in spec:
        cmd = spec["notify"].get("command")
        if (set(spec["notify"]) != {"command"} or not isinstance(cmd, list) or not cmd
                or not all(isinstance(a, str) for a in cmd)):
            die('"notify" must be {"command": [...]}, a non-empty list of strings')
        spec["notify"] = {"command": [expand(a) for a in cmd]}
        # The file isn't checked here, so relink never fails on a missing hook; the run falls back.
        if not os.path.isabs(spec["notify"]["command"][0]):
            die('"notify" command must start with an absolute path')
```

- [ ] **Step 7: Run the whole suite**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all pass.

- [ ] **Step 8: Commit,** after the gate.

```bash
git add skills/scheduling-agent-jobs/scripts/jobs.py skills/scheduling-agent-jobs/tests/test_jobs.py
git commit -m "Validate the notify spec field"
```

---

### Task 3: Run the hook, with a payload and a fallback

**Files:**
- Modify: `JOBS_PY`, covering the constants, new functions before `deliver()`, `deliver()` itself, and the end of `cmd_exec()`.
- Test: `TEST_JOBS`

**Interfaces:**
- Consumes: `run_outcome()` and `deliver(job, outcome, payload, tx)` from Task 1, `job["notify"]["command"]` from Task 2, and the `FAKE_HOOK`, `self.hook`, and `self.payload_path` fixtures from Task 2.
- Produces:
  - `run_tail(log_path: str, run_id: str) -> str`.
  - `build_payload(job, entry, outcome, tx, resume, expected) -> dict`, payload version 1 as the spec defines it. Task 4 reads this.
  - `run_hook(job, payload, tx) -> bool`.

- [ ] **Step 1: Write the failing unit tests for the tail.** Add this class to `TEST_JOBS`, after `class Outcome`:

```python
class Tail(unittest.TestCase):
    def write_log(self, lines):
        f = tempfile.NamedTemporaryFile("w", delete=False)
        f.write("\n".join(lines) + "\n")
        f.close()
        self.addCleanup(os.remove, f.name)
        return f.name

    def test_only_this_run_without_skip_lines(self):
        p = self.write_log(["##### Started a · manual · run old1 #####", "old line",
                            "##### Started b · manual · run new2 #####", "new line",
                            "##### Skipped c: the previous run is still going #####", "last line"])
        self.assertEqual(jobs.run_tail(p, "new2"),
                         "##### Started b · manual · run new2 #####\nnew line\nlast line")

    def test_line_cap_and_missing_marker(self):
        p = self.write_log(["##### Started a · manual · run r1 #####"] + [f"line {i}" for i in range(80)])
        tail = jobs.run_tail(p, "r1").splitlines()
        self.assertEqual((len(tail), tail[-1]), (50, "line 79"))
        self.assertEqual(jobs.run_tail(p, "nope"), "")

    def test_needs_the_started_banner(self):
        p = self.write_log(["##### Started a · manual · run r1 #####", "one", "echo · run r1 #####", "two"])
        self.assertEqual(jobs.run_tail(p, "r1"),
                         "##### Started a · manual · run r1 #####\none\necho · run r1 #####\ntwo")
```

- [ ] **Step 2: Write the failing hook tests.** Add these to `class Exec`:

```python
    def payload(self):
        with open(self.payload_path) as f:
            return json.load(f)

    FALLBACK = "The notify hook failed; see the log."

    def test_notify_hook_replaces_notifier(self):
        self.add(notify={"command": [self.hook]})
        r = self.jobs("exec", "triage")
        self.assertEqual(r.returncode, 0, r.stderr)
        p, run = self.payload(), self.last_run()
        self.assertEqual((p["version"], p["status"], p["title"], p["message"], p["resultText"]),
                         (1, "success", "Triage ready", "7 emails triaged.", "Done."))
        self.assertEqual((p["runId"], p["slug"], p["expectedFile"], p["exitCode"]),
                         (run["runId"], "triage", self.out, 0))
        self.assertIn("→ Bash: gh auth status", p["logTail"])
        self.assertIn("claude --resume sess-1", p["resumeHint"])
        self.assertFalse(os.path.exists(self.notified))
        self.assertIn("hook says hi", self.log())
        self.assertIn("notify hook: done", self.log())

    def test_notify_hook_gets_failures_and_timeouts(self):
        self.add(notify={"command": [self.hook]})
        self.jobs("exec", "triage", mode="fail")
        p = self.payload()
        self.assertEqual((p["status"], p["title"], p["error"], p["exitCode"]),
                         ("failed", "Triage failed", "exit code 3, out.md not updated", 3))
        self.add(notify={"command": [self.hook]}, timeoutSeconds=1)
        self.jobs("exec", "triage", mode="hang")
        self.assertEqual((self.payload()["status"], self.payload()["exitCode"]), ("timeout", 124))

    def test_failing_hook_falls_back_to_notifier(self):
        self.add(notify={"command": [self.hook]})
        self.env["FAKE_HOOK_EXIT"] = "1"
        r = self.jobs("exec", "triage")
        self.assertEqual(r.returncode, 0)
        self.assertIn(f"7 emails triaged. Click to open it. {self.FALLBACK}", self.notification())
        self.assertIn("notify hook failed (exit 1)", self.log())
        self.assertEqual(self.last_run()["status"], "success")

    def test_hanging_hook_is_killed_and_falls_back(self):
        self.add(notify={"command": [self.hook]})
        self.env.update(FAKE_HOOK_SLEEP="10", SCHEDULED_JOBS_HOOK_TIMEOUT="1")
        start = time.monotonic()
        self.jobs("exec", "triage")
        self.assertLess(time.monotonic() - start, 8)
        self.assertIn(self.FALLBACK, self.notification())
        self.assertIn("notify hook failed (timed out after 1s)", self.log())

    def test_hook_deleted_after_add_falls_back(self):
        self.add(notify={"command": [self.hook]})
        os.remove(self.hook)
        self.jobs("exec", "triage")
        self.assertIn(self.FALLBACK, self.notification())
        self.assertIn("notify hook failed (could not start: FileNotFoundError)", self.log())

    def test_nul_in_hook_argument_falls_back(self):
        self.add(notify={"command": [self.hook, "a\u0000b"]})
        self.jobs("exec", "triage")
        self.assertIn(self.FALLBACK, self.notification())
        self.assertIn("notify hook failed (could not start: ValueError)", self.log())

    def test_hook_bin_override(self):
        self.add(notify={"command": ["/usr/bin/false"]})
        self.env["NOTIFY_HOOK_BIN"] = self.hook
        self.jobs("exec", "triage")
        self.assertEqual(self.payload()["status"], "success")
        self.assertIn("notify hook: done", self.log())

    def test_empty_hook_bin_does_not_fall_through(self):
        self.add(notify={"command": [self.hook]})
        self.env["NOTIFY_HOOK_BIN"] = ""
        self.jobs("exec", "triage")
        self.assertFalse(os.path.exists(self.payload_path))
        self.assertIn(self.FALLBACK, self.notification())

    def test_relative_expected_file_resolves_against_workdir(self):
        self.add(notify={"command": [self.hook]}, expectedFile="out.md")
        r = self.jobs("exec", "triage")  # runs from the test's folder, not the job's
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.payload()["expectedFile"], self.out)

    def test_bad_hook_timeout_is_ignored(self):
        self.env["SCHEDULED_JOBS_HOOK_TIMEOUT"] = "soon"
        self.add(notify={"command": [self.hook]})
        self.jobs("exec", "triage")
        self.assertIn("notify hook: done", self.log())

    def test_skipped_runs_do_not_call_hook(self):
        self.add(notify={"command": [self.hook]}, paused=True)
        self.jobs("exec", "triage")
        self.add(notify={"command": [self.hook]})
        lock = os.path.join(self.tmp, "home", "locks", "triage.lock")
        with open(lock, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            self.jobs("exec", "triage")
        self.assertFalse(os.path.exists(self.payload_path))
        self.assertFalse(os.path.exists(self.notified))
```

`test_skipped_runs_do_not_call_hook` already passes before this task. It's a regression guard.

- [ ] **Step 3: Run them and confirm the rest fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py Tail Exec.test_notify_hook_replaces_notifier Exec.test_failing_hook_falls_back_to_notifier`
Expected:
- The `Tail` tests error with `no attribute 'run_tail'`.
- `test_notify_hook_replaces_notifier` errors with `FileNotFoundError` on the payload file, because terminal-notifier was used instead.
- `test_failing_hook_falls_back_to_notifier` fails because the fallback text is missing.

- [ ] **Step 4: Add the constants** in `JOBS_PY`, directly under `KILL_GRACE_SECONDS = 10`:

```python
HOOK_TIMEOUT_DEFAULT = 60
LOG_TAIL_LINES = 50
TEXT_CAP_CHARS = 16384
```

- [ ] **Step 5: Add `run_tail()`, `build_payload()`, and `run_hook()`** directly before `def deliver(`, and replace `deliver()`:

```python
def run_tail(log_path, run_id):
    """This run's part of the log: from its Started line, without other invocations' Skipped
    lines, at most LOG_TAIL_LINES lines and TEXT_CAP_CHARS characters. Empty without the marker."""
    marker = f"· run {run_id} #####"
    try:
        with open(log_path, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    starts = [i for i, line in enumerate(lines) if line.startswith("##### Started") and line.endswith(marker)]
    if not starts:
        return ""
    mine = [line for line in lines[starts[-1]:] if not line.startswith("##### Skipped")]
    return "\n".join(mine[-LOG_TAIL_LINES:])[-TEXT_CAP_CHARS:]


def cap(text):
    return text[:TEXT_CAP_CHARS] if isinstance(text, str) else None


def build_payload(job, entry, outcome, tx, resume, expected):
    """Version 1 of what a notify hook gets on stdin. `expected` is the absolute path cmd_exec checked."""
    result_text = (tx.result or {}).get("result")
    return {"version": 1, "slug": job["slug"], "name": job["name"], "label": job.get("label", job["name"]),
            "runId": entry["runId"], "source": entry["source"], "status": entry["status"],
            "title": outcome["title"], "message": cap(outcome["message"]), "error": cap(entry["error"]),
            "exitCode": entry["exitCode"], "startedAt": entry["startedAt"], "finishedAt": entry["finishedAt"],
            "durationMs": entry["durationMs"], "expectedFile": expected, "logPath": path("log", job["slug"]),
            "logTail": run_tail(path("log", job["slug"]), entry["runId"]), "sessionId": entry["sessionId"],
            "resumeHint": resume,
            "resultText": cap(result_text)}


def hook_timeout():
    """SCHEDULED_JOBS_HOOK_TIMEOUT (tests) when it is a positive integer, else the default."""
    try:
        value = int(os.environ.get("SCHEDULED_JOBS_HOOK_TIMEOUT", ""))
    except ValueError:
        return HOOK_TIMEOUT_DEFAULT
    return value if value > 0 else HOOK_TIMEOUT_DEFAULT


def run_hook(job, payload, tx):
    """Run the job's notify command with the payload on stdin. Returns True when it exited 0.
    Log lines name a fixed reason, never the command line."""
    override = os.environ.get("NOTIFY_HOOK_BIN")  # tests and the skill's fake run
    command = [override] if override is not None else job["notify"]["command"]  # "" fails to start
    timeout = hook_timeout()
    try:
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=tx.log, stderr=tx.log,
                                cwd=job["workdir"], env=child_env(), start_new_session=True)
    except (OSError, ValueError) as e:
        failure = f"could not start: {type(e).__name__}"
    else:
        try:
            proc.communicate(json.dumps(payload).encode("utf-8"), timeout=timeout)
            failure = f"exit {proc.returncode}" if proc.returncode != 0 else None
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            failure = f"timed out after {timeout}s"
    tx.write(f"notify hook failed ({failure})" if failure else "notify hook: done")
    return failure is None


def deliver(job, outcome, payload, tx):
    """Tell the user how the run went: through the job's notify hook, or terminal-notifier.
    A hook that fails, or a payload that couldn't be built (None), falls back to terminal-notifier."""
    extra = ""
    if job.get("notify"):
        if payload is not None and run_hook(job, payload, tx):
            return
        extra = " The notify hook failed; see the log."
    notify(outcome["title"], f'{outcome["message"]} {outcome["hint"]}{extra}', "scheduled-" + job["slug"],
           outcome["action"], tx)
```

- [ ] **Step 6: Wire it into `cmd_exec()`.** First, resolve a relative output path once, so the freshness check, the count, and the payload all use the same file. Replace:

```python
    expected = job.get("expectedFile")
    before = mtime(expected) if expected else 0.0
```

with:

```python
    expected = job.get("expectedFile")
    if expected and not os.path.isabs(expected):
        expected = os.path.abspath(os.path.join(job["workdir"], expected))
    before = mtime(expected) if expected else 0.0
```

Then replace this block, which runs from `record(slug, {"runId": run_id, "slug": slug, "source": source, ...` through the `tx.write(runner.resume_hint(...))` line:

```python
    record(slug, {"runId": run_id, "slug": slug, "source": source, "status": status, "exitCode": code,
                  "error": error, "startedAt": started_at, "finishedAt": finished_at,
                  "durationMs": duration_ms, "sessionId": tx.session_id,
                  "costUsd": (tx.result or {}).get("total_cost_usd")})
    tx.write(f"##### Finished {finished_at} · {status} · exit {code} · {duration_ms // 1000}s"
             + (f" · {error}" if error else "") + " #####")
    if tx.session_id:
        tx.write(runner.resume_hint(job["workdir"], tx.session_id))
```

with:

```python
    entry = {"runId": run_id, "slug": slug, "source": source, "status": status, "exitCode": code,
             "error": error, "startedAt": started_at, "finishedAt": finished_at,
             "durationMs": duration_ms, "sessionId": tx.session_id,
             "costUsd": (tx.result or {}).get("total_cost_usd")}
    record(slug, entry)
    tx.write(f"##### Finished {finished_at} · {status} · exit {code} · {duration_ms // 1000}s"
             + (f" · {error}" if error else "") + " #####")
    resume = runner.resume_hint(job["workdir"], tx.session_id) if tx.session_id else None
    if resume:
        tx.write(resume)
```

Then replace the Task 1 lines:

```python
    outcome = run_outcome(job, status, error, expected)
    deliver(job, outcome, None, tx)
    return code
```

with:

```python
    outcome = run_outcome(job, status, error, expected)
    payload = None
    if job.get("notify"):
        try:
            payload = build_payload(job, entry, outcome, tx, resume, expected)
        except Exception as e:  # a payload bug must not cost the user the result
            tx.write(f"notify hook failed (payload: {type(e).__name__})")
    deliver(job, outcome, payload, tx)
    return code
```

- [ ] **Step 7: Run the whole suite**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all pass.

- [ ] **Step 8: Commit,** after the gate.

```bash
git add skills/scheduling-agent-jobs/scripts/jobs.py skills/scheduling-agent-jobs/tests/test_jobs.py
git commit -m "Run a job's notify hook after each run, falling back to terminal-notifier"
```

---

### Task 4: The Postmark sender

**Files:**
- Create: `skills/scheduling-agent-jobs/scripts/notify_postmark.py` (mode 755)
- Create: `skills/scheduling-agent-jobs/tests/test_notify_postmark.py`

**Interfaces:**
- Consumes: payload version 1 from Task 3, on stdin.
- Produces: a CLI with `--from ADDR`, `--to ADDR` (repeatable), `--token-var NAME` (all three required), `--env-file PATH`, and `--message-stream NAME` (default `outbound`).
  - It exits 0 with `notify_postmark: sent` when Postmark accepts the message.
  - It exits 1 with one `notify_postmark: …` line on stderr otherwise.
  - The `POSTMARK_API_URL` env var overrides the endpoint for tests, and only a local `http://127.0.0.1` or `http://localhost` URL is accepted.
  - It also produces `read_env_file(path) -> dict`.

- [ ] **Step 1: Write the failing tests** in `skills/scheduling-agent-jobs/tests/test_notify_postmark.py`:

```python
#!/usr/bin/python3
"""Tests for notify_postmark.py. A local HTTP server stands in for Postmark and the token is
a dummy, so nothing is sent.
Run: /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py"""
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SENDER = os.path.join(HERE, "..", "scripts", "notify_postmark.py")
TOKEN = "dummy-token-5f1c"
OK = (200, {"ErrorCode": 0, "Message": "OK", "MessageID": "msg-1"}, {})


def load_sender():
    spec = importlib.util.spec_from_file_location("notify_postmark", SENDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePostmark(http.server.BaseHTTPRequestHandler):
    reply = OK
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakePostmark.received.append((self.headers, body))  # self.headers looks names up case-insensitively
        self.answer(*FakePostmark.reply)

    def do_GET(self):  # only reached if a redirect were followed
        FakePostmark.received.append((self.headers, None))
        self.answer(*OK)

    def answer(self, code, reply, headers):
        data = (reply if isinstance(reply, str) else json.dumps(reply)).encode()
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        if "Content-Length" not in headers:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Sender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakePostmark)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/email"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        FakePostmark.received = []
        FakePostmark.reply = OK
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w") as f:
            f.write(f'# keys\n\nOTHER=1\nexport POSTMARK_TOKEN="{TOKEN}" # server token\n')
        self.out = os.path.join(self.tmp, "report.md")
        with open(self.out, "w") as f:
            f.write("# Report\n\n3 new items\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_sender(self, payload, *extra, env_file=True, raw=None, token_env=None, api_url=None):
        args = ["/usr/bin/python3", SENDER, "--from", "jobs@example.com", "--to", "me@example.com",
                "--token-var", "POSTMARK_TOKEN", *extra]
        if env_file:
            args += ["--env-file", self.env_file]
        env = dict(os.environ, POSTMARK_API_URL=api_url or self.url, no_proxy="127.0.0.1", NO_PROXY="127.0.0.1")
        env.pop("POSTMARK_TOKEN", None)
        if token_env:
            env["POSTMARK_TOKEN"] = token_env
        r = subprocess.run(args, input=raw if raw is not None else json.dumps(payload), env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertFalse(TOKEN in r.stdout or TOKEN in r.stderr, "the token appeared in the sender's output")
        return r

    def success(self, **over):
        p = {"version": 1, "slug": "report", "status": "success", "title": "Report ready",
             "message": "3 new items.", "expectedFile": self.out, "error": None,
             "logPath": "/tmp/report.log", "logTail": "line", "resumeHint": None, "resultText": None}
        p.update(over)
        return p

    def test_success_sends_the_output_file(self):
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 0, r.stderr)
        headers, body = FakePostmark.received[0]
        self.assertTrue(headers.get("X-Postmark-Server-Token") == TOKEN, "wrong or missing token header")
        self.assertEqual((body["From"], body["To"], body["Subject"]),
                         ("jobs@example.com", "me@example.com", "Report ready: 3 new items."))
        self.assertEqual(body["TextBody"], "# Report\n\n3 new items\n")
        self.assertEqual((body["MessageStream"], body["Tag"]), ("outbound", "report"))
        self.assertEqual(r.stdout.strip(), "notify_postmark: sent")

    def test_success_without_output_file(self):
        self.assertEqual(self.run_sender(self.success(expectedFile=None, resultText="Summary.")).returncode, 0)
        self.assertEqual(FakePostmark.received[-1][1]["TextBody"], "Summary.")
        self.assertEqual(self.run_sender(self.success(expectedFile=None)).returncode, 0)
        self.assertEqual(FakePostmark.received[-1][1]["TextBody"], "3 new items.")

    def test_failure_sends_error_log_and_tail(self):
        r = self.run_sender(self.success(status="failed", title="Report failed", message="Exit code 3.",
                                         error="exit code 3", logTail="boom\nlast line",
                                         resumeHint="cd /w && claude --resume s-1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        text = FakePostmark.received[0][1]["TextBody"]
        for part in ("exit code 3", "Log: /tmp/report.log", "claude --resume s-1", "boom\nlast line"):
            self.assertIn(part, text)

    def test_several_recipients(self):
        self.run_sender(self.success(), "--to", "other@example.com")
        self.assertEqual(FakePostmark.received[0][1]["To"], "me@example.com, other@example.com")

    def test_token_from_environment_without_env_file(self):
        r = self.run_sender(self.success(), env_file=False, token_env=TOKEN)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_variable(self):
        r = self.run_sender(self.success(), "--token-var", "NOPE")
        self.assertEqual(r.returncode, 1)
        self.assertIn("NOPE isn't set", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_missing_env_file(self):
        self.env_file = os.path.join(self.tmp, "gone.env")
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't read", r.stderr)

    def test_unreadable_output_file_sends_nothing(self):
        r = self.run_sender(self.success(expectedFile=os.path.join(self.tmp, "gone.md")))
        self.assertEqual(r.returncode, 1)
        self.assertIn("can't read the job's output file", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_postmark_error(self):
        FakePostmark.reply = (422, {"ErrorCode": 300, "Message": "Invalid email request"}, {})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertIn("Postmark error 300 (HTTP 422): Invalid email request", r.stderr)

    def test_error_that_echoes_the_token_is_redacted(self):
        FakePostmark.reply = (422, {"ErrorCode": 10, "Message": f"Bad token {TOKEN}"}, {})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertIn("Bad token [token]", r.stderr)

    def test_redirect_is_not_followed(self):
        FakePostmark.reply = (302, {}, {"Location": "/elsewhere"})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(FakePostmark.received), 1)

    def test_only_http_200_with_error_code_0_succeeds(self):
        for reply in ((201, {"ErrorCode": 0}, {}), (200, [1, 2], {}), (200, "not json", {}),
                      (200, {"ErrorCode": "0"}, {})):
            FakePostmark.reply = reply
            self.assertEqual(self.run_sender(self.success()).returncode, 1, reply)

    def test_bad_payloads(self):
        for raw in ("not json", "[1]", json.dumps(self.success(version=2)), json.dumps(self.success(version=True))):
            r = self.run_sender(None, raw=raw)
            self.assertEqual(r.returncode, 1, raw)
            self.assertIn("payload", r.stderr, raw)
        self.assertEqual(FakePostmark.received, [])

    def test_token_with_a_newline_is_refused_without_leaking(self):
        r = self.run_sender(self.success(), env_file=False, token_env=TOKEN + "\nX-Other: 1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has spaces or control characters", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_truncated_error_reply_gives_one_line(self):
        FakePostmark.reply = (422, {"ErrorCode": 300, "Message": "x"}, {"Content-Length": "5000"})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertTrue(r.stderr.startswith("notify_postmark: "), r.stderr)

    def test_api_url_override_must_be_local(self):
        r = self.run_sender(self.success(), api_url="https://elsewhere.example/email")
        self.assertEqual(r.returncode, 1)
        self.assertIn("may only point at a local test server", r.stderr)
        self.assertEqual(FakePostmark.received, [])

    def test_env_file_forms(self):
        p = os.path.join(self.tmp, "forms.env")
        with open(p, "w") as f:
            f.write("# c\n\nA=bare # note\nexport B='single # kept' # note\nC=\"double\"\nD=x\nD=y\nE=a#b\n")
        self.assertEqual(load_sender().read_env_file(p),
                         {"A": "bare", "B": "single # kept", "C": "double", "D": "y", "E": "a#b"})


if __name__ == "__main__":
    unittest.main(verbosity=1)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py`
Expected: every test fails or errors, because `notify_postmark.py` doesn't exist.

- [ ] **Step 3: Write `skills/scheduling-agent-jobs/scripts/notify_postmark.py`:**

```python
#!/usr/bin/python3
"""Email a scheduled job's result through Postmark.

jobs.py runs this as a job's notify hook and passes the finished run on stdin as JSON
(payload version 1). On success it sends the job's output file; on any other result, the
error and the end of the log. Standard library only, for the macOS system Python.

  "notify": {"command": ["/usr/bin/python3",
      "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
      "--from", "reports@example.com", "--to", "me@example.com",
      "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]}

The token is a Postmark server token, read from --env-file (or from the environment when no
file is given). It is never printed.
"""
import argparse
import json
import os
import http.client
import re
import sys
import urllib.error
import urllib.request

API_URL = "https://api.postmarkapp.com/email"
LOCAL_URL = re.compile(r"^http://(127\.0\.0\.1|localhost)(:[0-9]+)?/")
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
SUBJECT_MAX = 200
BODY_MAX = 1_000_000
REPLY_MAX = 65536


def fail(message, token=None):
    text = str(message)
    if token:
        text = text.replace(token, "[token]")
    print(f"notify_postmark: {text}", file=sys.stderr)
    sys.exit(1)


def parse_value(raw):
    raw = raw.strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


def read_env_file(p):
    """KEY=value lines; export, quotes, and # comments allowed; the last assignment wins."""
    values = {}
    with open(os.path.expanduser(p)) as f:
        for line in f:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = LINE.match(line)
            if m:
                values[m.group(1)] = parse_value(m.group(2))
    return values


def find_token(env_file, var):
    if env_file:
        try:
            value = read_env_file(env_file).get(var)
        except OSError:
            fail(f"can't read {env_file}")
        where = env_file
    else:
        value, where = os.environ.get(var), "the environment"
    if not value:
        fail(f"{var} isn't set in {where}")
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        fail(f"{var} has spaces or control characters")
    return value


def api_url():
    """Postmark's endpoint, or POSTMARK_API_URL for tests, which may only point at this Mac."""
    override = os.environ.get("POSTMARK_API_URL")
    if override is None:
        return API_URL
    if not LOCAL_URL.match(override):
        fail("POSTMARK_API_URL may only point at a local test server")
    return override


def subject(run):
    text = f'{run.get("title") or "Scheduled job"}: {run.get("message") or ""}'
    return " ".join(text.split())[:SUBJECT_MAX].rstrip(": ")


def text_body(run):
    if run.get("status") == "success":
        out = run.get("expectedFile")
        if not out:
            return run.get("resultText") or run.get("message") or "Finished."
        try:
            with open(out, errors="replace") as f:
                text = f.read(BODY_MAX + 1)
        except OSError:
            fail("can't read the job's output file")
        return text if len(text) <= BODY_MAX else text[:BODY_MAX] + "\n\n[truncated]"
    parts = [run.get("error") or run.get("message") or run.get("status") or "failed", "",
             f'Log: {run.get("logPath")}']
    if run.get("resumeHint"):
        parts.append(run["resumeHint"])
    if run.get("logTail"):
        parts += ["", "Last lines of the log:", "", run["logTail"]]
    return "\n".join(parts)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # urllib then raises HTTPError, so the token never goes to another URL


def send(token, message):
    url = api_url()
    try:
        req = urllib.request.Request(url, data=json.dumps(message).encode(), method="POST", headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "X-Postmark-Server-Token": token})
        try:
            response = urllib.request.build_opener(NoRedirect).open(req, timeout=20)
        except urllib.error.HTTPError as e:
            response = e
        with response:
            status, raw = response.code, response.read(REPLY_MAX)
    except (OSError, ValueError, http.client.HTTPException) as e:  # URLError is an OSError
        fail(f"couldn't reach Postmark ({type(e).__name__})", token)
    try:
        reply = json.loads(raw)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        fail(f"Postmark returned HTTP {status} without a JSON object")
    code = reply.get("ErrorCode")
    if status == 200 and type(code) is int and code == 0:
        return
    fail(f'Postmark error {code} (HTTP {status}): {reply.get("Message")}', token)


def main():
    p = argparse.ArgumentParser(description="Email a scheduled job's result through Postmark.")
    p.add_argument("--from", dest="sender", required=True)
    p.add_argument("--to", action="append", required=True)
    p.add_argument("--token-var", required=True, help="name of the variable that holds the server token")
    p.add_argument("--env-file", help="KEY=value file to read the token from (default: the environment)")
    p.add_argument("--message-stream", default="outbound")
    args = p.parse_args()
    try:
        run = json.loads(sys.stdin.read())
    except ValueError:
        fail("the payload on stdin isn't JSON")
    if not isinstance(run, dict):
        fail("the payload on stdin isn't a JSON object")
    if type(run.get("version")) is not int or run["version"] != 1:
        fail("unsupported payload version")
    message = {"From": args.sender, "To": ", ".join(args.to), "Subject": subject(run),
               "TextBody": text_body(run), "MessageStream": args.message_stream}
    if run.get("slug"):
        message["Tag"] = run["slug"]
    send(find_token(args.env_file, args.token_var), message)
    print("notify_postmark: sent")


if __name__ == "__main__":
    main()
```

Then run `chmod 755 skills/scheduling-agent-jobs/scripts/notify_postmark.py`.

- [ ] **Step 4: Run all the tests**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py && /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all pass.

- [ ] **Step 5: Commit,** after the gate, now including the sender's tests.

```bash
git add skills/scheduling-agent-jobs/scripts/notify_postmark.py skills/scheduling-agent-jobs/tests/test_notify_postmark.py
git commit -m "Add a Postmark sender for the notify hook"
```

---

### Task 5: Docs

**Files:**
- Modify: `AGENTS.md`. `CLAUDE.md` is a symlink to it.
- Modify: `README.md`
- Modify: `skills/scheduling-agent-jobs/SKILL.md`

- [ ] **Step 1: AGENTS.md.** Replace the first "Before every commit" bullet with:

```markdown
- Run `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py`, `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py`, and `/usr/bin/python3 -B tests/test_install.py`. Delete any `__pycache__` they leave.
```

- [ ] **Step 2: README introduction.** In the first paragraph, replace `When a run finishes, the scheduler tries to send a macOS notification with the result. Click it to open the output on success or the log on failure.` with:

```markdown
When a run finishes, the scheduler tries to send a macOS notification with the result. Click it to open the output on success or the log on failure. A job can send its result somewhere else instead, such as an email (see "Send results somewhere else").
```

- [ ] **Step 3: README Notifications bullet.** In "How it works and what to watch", replace `A run that starts while the last one is still going is skipped, with no notification.` in the **Notifications** bullet with:

```markdown
A run that starts while the last one is still going is skipped, with no notification. A job with a `notify` command sends its result through that command instead, and falls back to a notification if the command fails.
```

- [ ] **Step 3b: README resume line.** In the **Looking into a run** bullet, replace `the run's log ends with a` with `the run's log includes a`, since a hook's output can follow that line.

- [ ] **Step 4: README new section.** Insert this directly before `## Update`:

````markdown
## Send results somewhere else

By default every finished run tries to post a macOS notification. To send the result somewhere else, give the job a `notify` command. `jobs.py` runs it after each finished run in place of the notification, in the job's folder, and passes the run on stdin as JSON. The JSON includes:

- the status (`success`, `failed`, `timeout`, or `stopped`), title, message, and error
- the output file and log path
- the last lines of this run's log
- the agent's final message, and a `claude --resume` line when there is one

The first item must be an absolute path, and a leading `~` in any item is expanded. Skipped runs send nothing.

The repo includes a Postmark sender. On success it emails the job's output file, or the agent's final message when the job has no output file. On any other result it emails the error and the end of the log:

```json
"notify": {
  "command": ["/usr/bin/python3", "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
              "--from", "reports@example.com", "--to", "me@example.com",
              "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]
}
```

- Use a Postmark **server** token, and send from an address or domain verified in that Postmark account.
- Keep the token in a file the job can read, outside `~/Documents`, `~/Desktop`, `~/Downloads`, and iCloud Drive, and out of git. The spec names the file and the variable, never the token.
- The failure email includes the end of the log, which can show commands the run used and local paths.
- If the command exits nonzero, runs longer than 60 seconds, or can't start, the log says `notify hook failed (…)` and the run falls back to the macOS notification, with "The notify hook failed; see the log." added. A delivered result logs `notify hook: done`. Keep terminal-notifier installed for that fallback.
- Older copies of `jobs.py` reject the `notify` field. Remove it from a job before you go back to one.
````

- [ ] **Step 5: SKILL.md introduction.** In the first paragraph, directly after the sentence `If the notification can't be sent, or terminal-notifier isn't installed, the log says so.`, add:

```markdown
 A job with a `notify` command sends its result through that command instead, such as an email through the bundled Postmark sender, and falls back to the notification if the command fails.
```

- [ ] **Step 6: SKILL.md field table.** Add this row after the `open` row:

```markdown
| `notify` | Optional. `{"command": [...]}`: a command `jobs.py` runs after each finished run instead of the macOS notification, with the run as JSON on stdin. The first item is an absolute path, and `~` expands. `scripts/notify_postmark.py` emails the output, or the error and log tail, through Postmark; see the README. Never put a secret in the spec: the Postmark sender takes `--env-file` and `--token-var`. If the command fails, the run falls back to the notification. |
```

- [ ] **Step 7: SKILL.md step 4.** Replace item 2 of step 4, from `2. Run the job against fakes.` through the paragraph that ends `This adds one entry to the job's run history.`, with:

````markdown
2. Run the job against fakes. Nothing real runs, and no notification or message goes out:

   ```bash
   T=$(mktemp -d); printf '#!/bin/bash\nprintf "%%s\\n" "$*" >>%s/n; echo @CLOSED\n' $T >$T/notifier
   printf '#!/bin/bash\nexit 0\n' >$T/claude; printf '#!/bin/bash\ncat >%s/payload.json\n' $T >$T/hook
   chmod +x $T/notifier $T/claude $T/hook
   NOTIFY_HOOK_BIN=$T/hook CLAUDE_BIN=$T/claude NOTIFIER_BIN=$T/notifier /usr/bin/python3 "$J" exec morning-report --manual; echo exit=$?; sleep 1; cat $T/n $T/payload.json 2>/dev/null
   ```

   With `expectedFile` set, expect `exit=1`, because the fake wrote nothing. Without it, expect `exit=0`.
   - For a job without `notify`, the notifier line is `<label> failed … not updated` with `expectedFile`, and `<label> done` without it (`<label> ready` if the job sets `open`).
   - For a job with `notify`, there's no notifier line. Instead, `payload.json` has `"status": "failed"` with `not updated` in `error`, or `"status": "success"`, and the log ends with `notify hook: done`.

   `--manual` runs the job even though it's paused. This adds one entry to the job's run history.
````

- [ ] **Step 8: SKILL.md step 5.** In step 5, replace `naming its one-way effects (for example, a job that marks mail as read can't undo it)` with:

```markdown
naming its one-way effects (for example, a job that marks mail as read can't undo it, and a `notify` command sends a message to its recipients)
```

Then replace `Then check that record, the log, and that the notification appeared.` with:

```markdown
Then check that record and the log. For a job without `notify`, check that the notification appeared. For a job with `notify`, wait until `list --json` shows `running` false, find `notify hook: done` after that run's `##### Started … · run <runId> #####` line, and check that the message arrived. `notify hook failed` means the hook didn't confirm delivery and the runner tried a notification instead.
```

- [ ] **Step 9: Run all tests and commit,** after the gate.

```bash
/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py && /usr/bin/python3 -B tests/test_install.py && \
git add AGENTS.md README.md skills/scheduling-agent-jobs/SKILL.md && \
git commit -m "Document the notify hook and the Postmark sender"
```
