# Notify Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a job spec name a `notify` command that `jobs.py` runs after each finished run instead of terminal-notifier, and ship a Postmark sender that emails the run's output or error.

**Architecture:** `cmd_exec` first builds an outcome (title, message, click action, and hint), then hands it to `deliver()`. That calls the job's hook with a JSON payload on stdin, or terminal-notifier, and falls back to terminal-notifier when the hook fails. The Postmark sender is a separate stdlib script that reads that payload.

**Tech Stack:** Python 3.9 stdlib (`/usr/bin/python3`), unittest, launchd (off in tests).

**Spec:** `docs/superpowers/specs/2026-11-21-notify-hook-design.md`

## Global Constraints

- `jobs.py`, `notify_postmark.py`, and their tests use the stdlib only and run on `/usr/bin/python3` (3.9.6): no `match`, no `X | Y` type hints, and no third-party imports.
- Tests that touch jobs set `SCHEDULED_JOBS_NO_LAUNCHD=1` and a temporary `SCHEDULED_JOBS_HOME`. No test may load anything into the real launchd domain or reach the real Postmark API.
- terminal-notifier text for jobs without `notify` stays word for word what it is today. The existing tests in `test_jobs.py` must keep passing unchanged.
- The spec never holds a secret, and no output (stdout, stderr, or the log) may include the Postmark token.
- No personal references (home paths, usernames, and private project names) in code, tests, or docs. Examples use `example.com` and `~/notes`.
- Use the Oxford comma. Commits carry no attribution trailers.
- Before every commit: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py` and `/usr/bin/python3 -B tests/test_install.py` pass (plus `test_notify_postmark.py` once it exists), and no `__pycache__` is left behind.

All paths below are relative to the repo root, `herdr-scheduled-jobs/`. Work on the `notify-hook` branch.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `skills/scheduling-agent-jobs/scripts/jobs.py` | Modify | `notify` validation, `run_outcome()`, `run_tail()`, `run_hook()`, `deliver()`, and the `notified` field on run records |
| `skills/scheduling-agent-jobs/tests/test_jobs.py` | Modify | Hook, fallback, validation, and `notified` tests |
| `skills/scheduling-agent-jobs/scripts/notify_postmark.py` | Create | Reads the payload, builds the email, and sends it through Postmark |
| `skills/scheduling-agent-jobs/tests/test_notify_postmark.py` | Create | Tests the sender against a local fake Postmark |
| `README.md`, `skills/scheduling-agent-jobs/SKILL.md`, `AGENTS.md` | Modify | Docs and the pre-commit test list |

---

### Task 1: Separate the outcome from its delivery, and record `notified`

This is a pure refactor plus one new run-record field. Existing notification text must not change.

**Files:**
- Modify: `skills/scheduling-agent-jobs/scripts/jobs.py`. That's `notify()` around line 713, and the end of `cmd_exec()` from `finished_at = now_iso()` to `return code`.
- Test: `skills/scheduling-agent-jobs/tests/test_jobs.py`

**Interfaces:**
- Produces:
  - `run_outcome(job, status, error, expected) -> dict` with keys `title` (str), `message` (str, no click hint), `action` (list[str]), and `hint` (str).
  - `notify(...) -> bool`, which is True only when terminal-notifier exited 0.
  - `deliver(job, outcome, payload, tx) -> str`, which returns `"terminal-notifier"` or `"none"` in this task. Task 3 adds `"hook"` and `"hook-failed"`.
  - Run records gain `"notified"`.

- [ ] **Step 1: Write the failing tests.** Add these to `class Exec` in `test_jobs.py`, after `test_success`:

```python
    def test_records_how_it_notified(self):
        self.add()
        self.jobs("exec", "triage")
        self.notification()
        self.assertEqual(self.last_run()["notified"], "terminal-notifier")

    def test_records_no_notification_when_notifier_fails(self):
        self.add()
        self.env["NOTIFIER_BIN"] = os.path.join(self.tmp, "no-such-notifier")
        self.jobs("exec", "triage")
        self.assertEqual(self.last_run()["notified"], "none")
        self.assertIn("terminal-notifier failed", self.log())
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py Exec.test_records_how_it_notified Exec.test_records_no_notification_when_notifier_fails`
Expected: both fail with `KeyError: 'notified'`.

- [ ] **Step 3: Make `notify()` report whether it sent.** Replace the whole `notify` function with:

```python
def notify(title, message, group, action, log=None):
    """Post a macOS notification. Returns True only when terminal-notifier exited 0."""
    if not notifier():
        if log:
            log.write("No notification: terminal-notifier isn't installed (brew install terminal-notifier).")
        return False
    # -execute, not -action: a waiting notifier quits the moment the click arrives, before macOS
    # brings it forward, and macOS then shows "terminal-notifier is not open anymore". With -execute
    # it exits once the notification is posted, and macOS relaunches it on click to run the action.
    try:
        r = subprocess.run([notifier(), "-title", title, "-message", message, "-group", group,
                            "-sound", "default", "-execute", shlex.join(action)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        failure = f"exit {r.returncode}: {r.stderr.strip()}" if r.returncode != 0 else None
    except (OSError, subprocess.TimeoutExpired) as e:
        failure = str(e)
    if failure and log:
        log.write(f"terminal-notifier failed ({failure})")
    return failure is None
```

- [ ] **Step 4: Add `run_outcome()` and `deliver()`** directly after `notify()`:

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
        with open(expected, errors="replace") as f:
            m = re.search(job["countRegex"], f.read())
        n = re.search(r"\d+", m.group(0)) if m else None
        if n:
            message = f'{n.group(0)} {job.get("countNoun", "items")}.'
    target = job.get("open") or expected
    if target:
        return {"title": f"{label} ready", "message": message, "action": open_action(target),
                "hint": "Click to open it."}
    return {"title": f"{label} done", "message": message, "action": show_log, "hint": "Click to see the log."}


def deliver(job, outcome, payload, tx):
    """Tell the user how the run went. Returns how it went out, for the run record."""
    group = "scheduled-" + job["slug"]
    sent = notify(outcome["title"], f'{outcome["message"]} {outcome["hint"]}', group, outcome["action"], tx)
    return "terminal-notifier" if sent else "none"
```

- [ ] **Step 5: Use them in `cmd_exec()`.** Replace everything from `finished_at = now_iso()` through `return code` at the end of `cmd_exec` with this code. The record is now written after delivery, so it can carry `notified`.

```python
    finished_at = now_iso()
    duration_ms = int((time.monotonic() - t0) * 1000)
    entry = {"runId": run_id, "slug": slug, "source": source, "status": status, "exitCode": code,
             "error": error, "startedAt": started_at, "finishedAt": finished_at,
             "durationMs": duration_ms, "sessionId": tx.session_id,
             "costUsd": (tx.result or {}).get("total_cost_usd")}
    tx.write(f"##### Finished {finished_at} · {status} · exit {code} · {duration_ms // 1000}s"
             + (f" · {error}" if error else "") + " #####")
    resume = runner.resume_hint(job["workdir"], tx.session_id) if tx.session_id else None
    if resume:
        tx.write(resume)

    outcome = run_outcome(job, status, error, expected)
    try:
        entry["notified"] = deliver(job, outcome, None, tx)
    except Exception as e:  # never lose the run record to a delivery bug
        tx.write(f"notification failed ({type(e).__name__}: {e})")
        entry["notified"] = "none"
    record(slug, entry)
    return code
```

- [ ] **Step 6: Run the whole suite**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all tests pass, including every existing notification-text test.

- [ ] **Step 7: Commit**

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
git add skills/scheduling-agent-jobs/scripts/jobs.py skills/scheduling-agent-jobs/tests/test_jobs.py
git commit -m "Separate a run's outcome from its delivery and record how it notified"
```

---

### Task 2: Validate the `notify` spec field

**Files:**
- Modify: `skills/scheduling-agent-jobs/scripts/jobs.py`, in `OPTIONAL` (around line 330) and `validate()`.
- Test: `skills/scheduling-agent-jobs/tests/test_jobs.py`

**Interfaces:**
- Produces: a stored job's `job["notify"]["command"]` is a non-empty `list[str]` whose first item is an absolute path to an existing file, with every leading `~` expanded.

- [ ] **Step 1: Let `Exec.add()` return a refused add.** Replace the two lines `r = self.jobs("add", p)` and `self.assertEqual(r.returncode, 0, r.stderr)` in `add()`, and its signature, so it reads:

```python
    def add(self, check=True, **over):
```

and, at the end of `add()`:

```python
        r = self.jobs("add", p)
        if check:
            self.assertEqual(r.returncode, 0, r.stderr)
        return r
```

- [ ] **Step 2: Write the failing tests.** Add these to `class Exec`:

```python
    def test_notify_must_be_a_command_list(self):
        hook = os.path.join(self.tmp, "hook")
        for bad in ({"command": []}, {"command": hook}, {"cmd": [hook]},
                    {"command": [hook], "extra": 1}, {"command": [hook, 3]}, [hook]):
            r = self.add(check=False, notify=bad)
            self.assertEqual(r.returncode, 1, bad)
            self.assertIn('"notify"', r.stderr, bad)

    def test_notify_command_needs_an_absolute_existing_file(self):
        for first in ("hook", os.path.join(self.tmp, "no-such-hook")):
            r = self.add(check=False, notify={"command": [first]})
            self.assertEqual(r.returncode, 1, first)
            self.assertIn("absolute path", r.stderr)

    def test_notify_arguments_expand_home(self):
        self.env["HOME"] = self.tmp
        self.add(notify={"command": ["~/hook", "--env-file", "~/.env"]})
        with open(os.path.join(self.tmp, "home", "jobs", "triage.json")) as f:
            command = json.load(f)["notify"]["command"]
        self.assertEqual(command, [os.path.join(self.tmp, "hook"), "--env-file", os.path.join(self.tmp, ".env")])
```

These tests need a `hook` file in the temp folder, and Task 3's tests reuse it. Define the fake now, under `FAKE_NOTIFIER` at the top of the file:

```python
FAKE_HOOK = """#!/bin/bash
sleep "${FAKE_HOOK_SLEEP:-0}"
cat >"$FAKE_HOOK_OUT"
echo "hook says hi"
exit "${FAKE_HOOK_EXIT:-0}"
"""
```

and in `setUp`:

```python
        for name, body in (("claude", FAKE_CLAUDE), ("notifier", FAKE_NOTIFIER), ("hook", FAKE_HOOK)):
```

plus, in the `self.env = dict(...)` call, add `FAKE_HOOK_OUT=os.path.join(self.tmp, "hook-payload.json"),`, and after it:

```python
        self.hook = os.path.join(self.tmp, "hook")
        self.payload_path = os.path.join(self.tmp, "hook-payload.json")
```

- [ ] **Step 3: Run them and confirm they fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py Exec.test_notify_must_be_a_command_list Exec.test_notify_command_needs_an_absolute_existing_file Exec.test_notify_arguments_expand_home`
Expected: failures with `unknown field "notify"`.

- [ ] **Step 4: Implement.** In `OPTIONAL`, add `"notify": dict` (keep the dict on two lines as it is now):

```python
OPTIONAL = {"label": str, "expectedFile": str, "open": (str, list), "countRegex": str, "countNoun": str,
            "model": str, "paused": bool, "claudePath": str, "createdAt": str, "notify": dict}
```

The type check in `validate()` rejects a list `notify` with `"notify" must be a dict`. In `validate()`, directly after the `if isinstance(spec.get("open"), list) ...` check, add:

```python
    if "notify" in spec:
        cmd = spec["notify"].get("command")
        if (set(spec["notify"]) != {"command"} or not isinstance(cmd, list) or not cmd
                or not all(isinstance(a, str) for a in cmd)):
            die('"notify" must be {"command": [...]}, a non-empty list of strings')
        spec["notify"] = {"command": [expand(a) for a in cmd]}
        first = spec["notify"]["command"][0]
        if not os.path.isabs(first) or not os.path.isfile(first):
            die(f'"notify" command must start with an absolute path to a file, not {first}')
```

- [ ] **Step 5: Run the whole suite**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
git add skills/scheduling-agent-jobs/scripts/jobs.py skills/scheduling-agent-jobs/tests/test_jobs.py
git commit -m "Validate the notify spec field"
```

---

### Task 3: Run the hook, with a payload and a fallback

**Files:**
- Modify: `skills/scheduling-agent-jobs/scripts/jobs.py`, covering the constants near the top, `deliver()`, and the delivery block in `cmd_exec()`.
- Test: `skills/scheduling-agent-jobs/tests/test_jobs.py`

**Interfaces:**
- Consumes: `run_outcome()` and `deliver()` from Task 1, and the validated `job["notify"]["command"]` from Task 2.
- Produces: payload version 1, a JSON object with these keys, which Task 4 reads: `version`, `slug`, `name`, `label`, `runId`, `source`, `status`, `title`, `message`, `error`, `exitCode`, `startedAt`, `finishedAt`, `durationMs`, `expectedFile`, `logPath`, `logTail`, `sessionId`, and `resumeHint`. `deliver()` can now return `"hook"` or `"hook-failed"`.

- [ ] **Step 1: Write the failing tests.** Add these to `class Exec`:

```python
    def payload(self):
        with open(self.payload_path) as f:
            return json.load(f)

    def test_notify_hook_replaces_notifier(self):
        self.add(notify={"command": [self.hook]})
        r = self.jobs("exec", "triage")
        self.assertEqual(r.returncode, 0, r.stderr)
        p = self.payload()
        run = self.last_run()
        self.assertEqual((p["version"], p["status"], p["title"], p["message"]),
                         (1, "success", "Triage ready", "7 emails triaged."))
        self.assertEqual((p["runId"], p["slug"], p["expectedFile"]), (run["runId"], "triage", self.out))
        self.assertIn("→ Bash: gh auth status", p["logTail"])
        self.assertIn("claude --resume sess-1", p["resumeHint"])
        self.assertFalse(os.path.exists(self.notified))
        self.assertEqual(run["notified"], "hook")
        self.assertIn("hook says hi", self.log())

    def test_notify_hook_gets_failures_and_timeouts(self):
        self.add(notify={"command": [self.hook]})
        self.jobs("exec", "triage", mode="fail")
        p = self.payload()
        self.assertEqual((p["status"], p["title"], p["error"]), ("failed", "Triage failed", "exit code 3"))
        self.add(notify={"command": [self.hook]}, timeoutSeconds=1)
        self.jobs("exec", "triage", mode="hang")
        self.assertEqual(self.payload()["status"], "timeout")

    def test_log_tail_covers_only_this_run(self):
        self.add(notify={"command": [self.hook]})
        self.jobs("exec", "triage", mode="fail")
        first = self.payload()["runId"]
        self.jobs("exec", "triage", mode="fail")
        tail = self.payload()["logTail"]
        self.assertNotIn(first, tail)
        self.assertEqual(tail.count("##### Started"), 1)

    def test_failing_hook_falls_back_to_notifier(self):
        self.add(notify={"command": [self.hook]})
        self.env["FAKE_HOOK_EXIT"] = "1"
        self.jobs("exec", "triage")
        self.assertIn("7 emails triaged. Click to open it. The notify hook failed; see the log.",
                      self.notification())
        self.assertIn("notify hook failed (exit 1)", self.log())
        self.assertEqual(self.last_run()["notified"], "hook-failed")

    def test_hanging_hook_times_out_and_falls_back(self):
        self.add(notify={"command": [self.hook]})
        self.env.update(FAKE_HOOK_SLEEP="10", SCHEDULED_JOBS_HOOK_TIMEOUT="1")
        start = time.monotonic()
        self.jobs("exec", "triage")
        self.assertLess(time.monotonic() - start, 8)
        self.assertIn("The notify hook failed", self.notification())
        self.assertIn("notify hook failed (", self.log())

    def test_hook_bin_override(self):
        self.add(notify={"command": ["/usr/bin/false"]})
        self.env["NOTIFY_HOOK_BIN"] = self.hook
        self.jobs("exec", "triage")
        self.assertEqual(self.payload()["status"], "success")
        self.assertEqual(self.last_run()["notified"], "hook")

    def test_missing_hook_at_run_time_falls_back(self):
        self.add(notify={"command": [self.hook]})
        self.env["NOTIFY_HOOK_BIN"] = os.path.join(self.tmp, "gone")
        self.jobs("exec", "triage")
        self.assertIn("The notify hook failed", self.notification())
        self.assertEqual(self.last_run()["notified"], "hook-failed")

    def test_skipped_run_does_not_call_hook(self):
        self.add(notify={"command": [self.hook]}, paused=True)
        self.jobs("exec", "triage")
        self.assertFalse(os.path.exists(self.payload_path))
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py Exec.test_notify_hook_replaces_notifier`
Expected: FAIL. The terminal-notifier fake is called and there is no payload file.

- [ ] **Step 3: Add the constants** under `KILL_GRACE_SECONDS = 10`:

```python
HOOK_TIMEOUT_SECONDS = int(os.environ.get("SCHEDULED_JOBS_HOOK_TIMEOUT", "60"))  # tests shorten it
LOG_TAIL_LINES = 50
```

- [ ] **Step 4: Add `run_tail()` and `run_hook()`** above `deliver()`, and replace `deliver()`:

```python
def run_tail(log_path, run_id, limit=LOG_TAIL_LINES):
    """Up to the last `limit` lines of this run's part of the log."""
    marker = f"· run {run_id} #####"
    try:
        with open(log_path, errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    start = max((i for i, line in enumerate(lines) if line.endswith(marker)), default=0)
    return "\n".join(lines[start:][-limit:])


def run_hook(job, payload, tx):
    """Run the job's notify command with the payload on stdin. Returns True when it exited 0."""
    override = os.environ.get("NOTIFY_HOOK_BIN")  # tests and fake runs
    command = [override] if override else job["notify"]["command"]
    try:
        r = subprocess.run(command, input=json.dumps(payload), cwd=job["workdir"], env=child_env(),
                           stdout=tx.log, stderr=tx.log, text=True, timeout=HOOK_TIMEOUT_SECONDS)
        failure = f"exit {r.returncode}" if r.returncode != 0 else None
    except (OSError, subprocess.TimeoutExpired) as e:
        failure = str(e)
    if failure:
        tx.write(f"notify hook failed ({failure})")
    return failure is None


def deliver(job, outcome, payload, tx):
    """Tell the user how the run went: through the job's notify hook, or terminal-notifier.
    Returns how it went out, for the run record."""
    group = "scheduled-" + job["slug"]
    extra = ""
    if job.get("notify"):
        if run_hook(job, payload, tx):
            return "hook"
        extra = " The notify hook failed; see the log."
    sent = notify(outcome["title"], f'{outcome["message"]} {outcome["hint"]}{extra}', group,
                  outcome["action"], tx)
    if job.get("notify"):
        return "hook-failed"
    return "terminal-notifier" if sent else "none"
```

- [ ] **Step 5: Build the payload in `cmd_exec()`.** Replace the line `entry["notified"] = deliver(job, outcome, None, tx)` with:

```python
        payload = None
        if job.get("notify"):
            payload = {"version": 1, "slug": slug, "name": job["name"],
                       "label": job.get("label", job["name"]), "runId": run_id, "source": source,
                       "status": status, "title": outcome["title"], "message": outcome["message"],
                       "error": error, "exitCode": code, "startedAt": started_at,
                       "finishedAt": finished_at, "durationMs": duration_ms, "expectedFile": expected,
                       "logPath": path("log", slug), "logTail": run_tail(path("log", slug), run_id),
                       "sessionId": tx.session_id, "resumeHint": resume}
        entry["notified"] = deliver(job, outcome, payload, tx)
```

Keep it inside the existing `try:` block from Task 1.

- [ ] **Step 6: Run the whole suite**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
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
- Produces: a CLI with `--from ADDR`, `--to ADDR` (repeatable), `--token-var NAME` (required), `--env-file PATH`, and `--message-stream NAME` (default `outbound`). It exits 0 when sent and 1 on any failure, with a `notify_postmark: …` line on stderr. The `POSTMARK_API_URL` env var overrides the endpoint.

- [ ] **Step 1: Write the failing tests** in `skills/scheduling-agent-jobs/tests/test_notify_postmark.py`:

```python
#!/usr/bin/python3
"""Tests for notify_postmark.py. A local HTTP server stands in for Postmark, so nothing is
sent. Run: /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py"""
import http.server
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SENDER = os.path.join(HERE, "..", "scripts", "notify_postmark.py")
TOKEN = "tok-1234-secret"


class FakePostmark(http.server.BaseHTTPRequestHandler):
    reply = (200, {"ErrorCode": 0, "Message": "OK", "MessageID": "msg-1"})
    received = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakePostmark.received.append((dict(self.headers), body))
        code, reply = FakePostmark.reply
        data = json.dumps(reply).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
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
        FakePostmark.reply = (200, {"ErrorCode": 0, "Message": "OK", "MessageID": "msg-1"})
        self.env_file = os.path.join(self.tmp, ".env")
        with open(self.env_file, "w") as f:
            f.write(f'# keys\n\nOTHER=1\nexport POSTMARK_TOKEN="{TOKEN}"\n')
        self.out = os.path.join(self.tmp, "report.md")
        with open(self.out, "w") as f:
            f.write("# Report\n\n3 new posts\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_sender(self, payload, *extra, env_file=True):
        args = ["/usr/bin/python3", SENDER, "--from", "jobs@example.com", "--to", "me@example.com",
                "--token-var", "POSTMARK_TOKEN", *extra]
        if env_file:
            args += ["--env-file", self.env_file]
        env = dict(os.environ, POSTMARK_API_URL=self.url)
        r = subprocess.run(args, input=json.dumps(payload), env=env, capture_output=True, text=True, timeout=30)
        self.assertNotIn(TOKEN, r.stdout + r.stderr)
        return r

    def success(self, **over):
        p = {"version": 1, "slug": "report", "status": "success", "title": "Report ready",
             "message": "3 new posts.", "expectedFile": self.out, "error": None,
             "logPath": "/tmp/report.log", "logTail": "line", "resumeHint": None}
        p.update(over)
        return p

    def test_success_sends_the_output_file(self):
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 0, r.stderr)
        headers, body = FakePostmark.received[0]
        self.assertEqual(headers["X-Postmark-Server-Token"], TOKEN)
        self.assertEqual((body["From"], body["To"], body["Subject"]),
                         ("jobs@example.com", "me@example.com", "Report ready: 3 new posts."))
        self.assertEqual(body["TextBody"], "# Report\n\n3 new posts\n")
        self.assertEqual((body["MessageStream"], body["Tag"]), ("outbound", "report"))
        self.assertIn("msg-1", r.stdout)

    def test_success_without_output_file_sends_the_message(self):
        self.run_sender(self.success(expectedFile=None))
        self.assertEqual(FakePostmark.received[0][1]["TextBody"], "3 new posts.")

    def test_failure_sends_error_log_and_tail(self):
        self.run_sender(self.success(status="failed", title="Report failed", message="Exit code 3.",
                                     error="exit code 3", logTail="boom\nlast line",
                                     resumeHint="cd /w && claude --resume s-1"))
        text = FakePostmark.received[0][1]["TextBody"]
        for part in ("exit code 3", "Log: /tmp/report.log", "claude --resume s-1", "boom\nlast line"):
            self.assertIn(part, text)

    def test_several_recipients(self):
        self.run_sender(self.success(), "--to", "other@example.com")
        self.assertEqual(FakePostmark.received[0][1]["To"], "me@example.com, other@example.com")

    def test_token_from_environment_without_env_file(self):
        args = ["/usr/bin/python3", SENDER, "--from", "a@example.com", "--to", "b@example.com",
                "--token-var", "POSTMARK_TOKEN"]
        env = dict(os.environ, POSTMARK_API_URL=self.url, POSTMARK_TOKEN=TOKEN)
        r = subprocess.run(args, input=json.dumps(self.success()), env=env, capture_output=True, text=True)
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

    def test_postmark_error_exits_1(self):
        FakePostmark.reply = (422, {"ErrorCode": 400, "Message": "Sender signature not found"})
        r = self.run_sender(self.success())
        self.assertEqual(r.returncode, 1)
        self.assertIn("Postmark error 400: Sender signature not found", r.stderr)

    def test_bad_payload(self):
        args = ["/usr/bin/python3", SENDER, "--from", "a@example.com", "--to", "b@example.com",
                "--token-var", "POSTMARK_TOKEN", "--env-file", self.env_file]
        r = subprocess.run(args, input="not json", capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("payload", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=1)
```

- [ ] **Step 2: Run them and confirm they fail**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py`
Expected: every test fails, because `notify_postmark.py` doesn't exist.

- [ ] **Step 3: Write `skills/scheduling-agent-jobs/scripts/notify_postmark.py`:**

```python
#!/usr/bin/python3
"""Email a scheduled job's result through Postmark.

jobs.py runs this as a job's notify hook and passes the finished run on stdin as JSON.
On success it sends the job's output file; on any other result, the error and the end
of the log. Standard library only, for the macOS system Python.

  "notify": {"command": ["/usr/bin/python3",
      "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
      "--from", "reports@example.com", "--to", "me@example.com",
      "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]}

The token is a Postmark server token. It is read from --env-file, or from the
environment when no file is given, and is never printed.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("POSTMARK_API_URL", "https://api.postmarkapp.com/email")  # tests override it
LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def fail(message):
    print(f"notify_postmark: {message}", file=sys.stderr)
    sys.exit(1)


def read_env_file(p):
    values = {}
    with open(os.path.expanduser(p)) as f:
        for raw in f:
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            m = LINE.match(raw)
            if not m:
                continue
            key, value = m.groups()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def find_token(env_file, var):
    if env_file:
        try:
            value = read_env_file(env_file).get(var)
        except OSError as e:
            fail(f"can't read {env_file}: {e.strerror}")
        where = env_file
    else:
        value, where = os.environ.get(var), "the environment"
    if not value:
        fail(f"{var} isn't set in {where}")
    return value


def text_body(run):
    if run.get("status") == "success":
        out = run.get("expectedFile")
        if not out:
            return run.get("message") or "Finished."
        try:
            with open(out, errors="replace") as f:
                return f.read()
        except OSError as e:
            return f'{run.get("message") or "Finished."}\n\nCouldn\'t read {out}: {e.strerror}'
    parts = [run.get("error") or run.get("message") or run.get("status") or "failed", "",
             f'Log: {run.get("logPath")}']
    if run.get("resumeHint"):
        parts.append(run["resumeHint"])
    if run.get("logTail"):
        parts += ["", "Last lines of the log:", "", run["logTail"]]
    return "\n".join(parts)


def send(token, message):
    req = urllib.request.Request(API_URL, data=json.dumps(message).encode(), method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/json",
        "X-Postmark-Server-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            reply = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            reply = json.load(e)
        except ValueError:
            fail(f"Postmark returned HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        fail(f"couldn't reach Postmark: {getattr(e, 'reason', e)}")
    if reply.get("ErrorCode") != 0:
        fail(f'Postmark error {reply.get("ErrorCode")}: {reply.get("Message")}')
    return reply.get("MessageID")


def main():
    p = argparse.ArgumentParser(description="Email a scheduled job's result through Postmark.")
    p.add_argument("--from", dest="sender", required=True)
    p.add_argument("--to", action="append", required=True)
    p.add_argument("--token-var", required=True, help="name of the variable that holds the server token")
    p.add_argument("--env-file", help="KEY=value file to read the token from (default: the environment)")
    p.add_argument("--message-stream", default="outbound")
    args = p.parse_args()
    try:
        run = json.load(sys.stdin)
    except ValueError:
        fail("the payload on stdin isn't JSON")
    if not isinstance(run, dict):
        fail("the payload on stdin isn't a JSON object")
    token = find_token(args.env_file, args.token_var)
    message = {"From": args.sender, "To": ", ".join(args.to),
               "Subject": f'{run.get("title", "Scheduled job")}: {run.get("message", "")}'.strip(": "),
               "TextBody": text_body(run), "MessageStream": args.message_stream}
    if run.get("slug"):
        message["Tag"] = run["slug"]
    message_id = send(token, message)
    print(f"notify_postmark: sent {message_id} to {', '.join(args.to)}")


if __name__ == "__main__":
    main()
```

Then: `chmod 755 skills/scheduling-agent-jobs/scripts/notify_postmark.py`

- [ ] **Step 4: Run the tests**

Run: `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py && /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B tests/test_install.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
find . -name __pycache__ -prune -exec rm -rf {} +
git add skills/scheduling-agent-jobs/scripts/notify_postmark.py skills/scheduling-agent-jobs/tests/test_notify_postmark.py
git commit -m "Add a Postmark sender for the notify hook"
```

---

### Task 5: Docs

**Files:**
- Modify: `AGENTS.md`, the "Before every commit" bullet. `CLAUDE.md` is a symlink to it.
- Modify: `README.md`, with a new section after "How it works and what to watch".
- Modify: `skills/scheduling-agent-jobs/SKILL.md`, covering the field table and step 4.

- [ ] **Step 1: AGENTS.md.** Change the first "Before every commit" bullet to:

```markdown
- Run `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py`, `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py`, and `/usr/bin/python3 -B tests/test_install.py`. Delete any `__pycache__` they leave.
```

- [ ] **Step 2: README.md.** Insert this section before `## Update`:

````markdown
## Send results somewhere else

By default every finished run tries to post a macOS notification. To send the result somewhere else, give the job a `notify` command. `jobs.py` runs it after each finished run instead of the notification, and passes the run on stdin as JSON: its status (`success`, `failed`, `timeout`, or `stopped`), title, message, error, output file, log path, the last lines of this run's log, and a `claude --resume` line when there is one. The first item must be an absolute path, and a leading `~` in any item is expanded.

The repo includes a Postmark sender. On success it emails the job's output file, and on any other result, the error and the end of the log:

```json
"notify": {
  "command": ["/usr/bin/python3", "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
              "--from", "reports@example.com", "--to", "me@example.com",
              "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]
}
```

- Use a Postmark **server** token, and send from an address or domain verified in that Postmark account.
- Keep the token in a file the job can read, outside `~/Documents`, `~/Desktop`, `~/Downloads`, and iCloud Drive, and out of git. The spec names the file and the variable, never the token.
- If the command exits nonzero, takes longer than 60 seconds, or can't start, the log says so and the run falls back to the macOS notification, with "The notify hook failed; see the log." added. Each run's record in `list --json` has `notified`: `hook`, `hook-failed`, `terminal-notifier`, or `none`.
- Skipped runs send nothing, as before.
````

- [ ] **Step 3: SKILL.md.** Add this row to the field table after the `open` row:

```markdown
| `notify` | Optional. `{"command": [...]}`: a command `jobs.py` runs after each finished run instead of the macOS notification, with the run as JSON on stdin. The first item is an absolute path. The repo's `scripts/notify_postmark.py` emails the output, or the error and log tail, through Postmark (see the README). If the command fails, the run falls back to the notification. Never put a secret in the spec: the Postmark sender takes `--env-file` and `--token-var`. |
```

In step 4, after the fake-run code block, add:

````markdown
   For a job with `notify`, the fake run must not call the real command. Point `NOTIFY_HOOK_BIN` at a fake that saves its input, and check the payload instead of `$T/n`:

   ```bash
   printf '#!/bin/bash\ncat >%s/payload.json\n' $T >$T/hook; chmod +x $T/hook
   NOTIFY_HOOK_BIN=$T/hook CLAUDE_BIN=$T/claude NOTIFIER_BIN=$T/notifier /usr/bin/python3 "$J" exec morning-report --manual; echo exit=$?; cat $T/payload.json
   ```

   Expect `"status": "failed"` with `not updated` in `error` when `expectedFile` is set. The step 5 real run sends a real message.
````

- [ ] **Step 4: Check for personal references**

Run: `git diff main --stat && git diff main | grep -niE 'rianvdm|/Users/|elezea|cloudflare|product-ai' || echo clean`
Expected: `clean`.

- [ ] **Step 5: Run all tests and commit**

```bash
/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py && /usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py && /usr/bin/python3 -B tests/test_install.py
find . -name __pycache__ -prune -exec rm -rf {} +
git add AGENTS.md README.md skills/scheduling-agent-jobs/SKILL.md
git commit -m "Document the notify hook and the Postmark sender"
```

---

## After the plan: bringing the x-mentions job live (product-ai, not this repo)

These steps run after the branch is reviewed, and they need Rian's go-ahead before step 3.

1. In `product-ai/scripts/scheduled-jobs/x-mentions.json`, replace the `open` key with this `notify` block, and change the command's "the scheduled job opens it from its notification instead" line in `.opencode/command/x-mentions.md` to say it emails the report.

   ```json
   "notify": {"command": ["/usr/bin/python3", "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
     "--from", "rian@elezea.com", "--to", "rian@cloudflare.com",
     "--env-file", "~/git/product-ai/.env", "--token-var", "PRODUCT_AI_POSTMARK_TOKEN"]}
   ```

2. Run `jobs.py add scripts/scheduled-jobs/x-mentions.json` (paused), then do the skill's fake run with `NOTIFY_HOOK_BIN`.
3. Send one real email by piping a sample payload into `notify_postmark.py`. This confirms that `rian@elezea.com` is a verified sender on the product-ai Postmark server.
4. Offer the real run, which costs about $0.005 per matching post and advances `newest_id`. On a yes, run `resume x-mentions`, then `run x-mentions`, and check the email arrives.
