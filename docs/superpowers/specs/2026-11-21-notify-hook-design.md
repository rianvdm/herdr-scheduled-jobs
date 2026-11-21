# Notify hook design

*2026-11-21*

## Problem

Every finished run tells its owner the result through terminal-notifier, and that's the only channel. Someone who wants a scheduled report in their inbox, or who isn't at the Mac when a 5am job fails, has no way to get the result anywhere else. The first case is a twice-daily report job whose owner wants the report emailed to them, with failures emailed too.

## Outcome

A job spec can name a command that `jobs.py` runs after each finished run instead of sending the macOS notification. The repo ships one such command, a Postmark sender, so a job can email its output on success and the error on failure. Anyone can use it with their own Postmark server token, or write their own command for another service. A spec that doesn't set the new field behaves exactly as it does today.

## Spec field

```json
"notify": {
  "command": ["/usr/bin/python3", "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
              "--from", "reports@example.com", "--to", "me@example.com",
              "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]
}
```

- `notify` is optional and must be an object with a `command` key, a non-empty list of strings. No other keys are allowed.
- `validate()` expands a leading `~` in each argument, the same way it expands `workdir`, and requires the first argument to be an absolute path to an existing file, because jobs run with a minimal PATH.
- The spec never holds a secret. The Postmark sender takes the name of a variable and the file it lives in.

## Runner changes (`jobs.py`)

1. **Separate the outcome from its delivery.** `cmd_exec` builds one outcome after recording the run: `title`, `message` (without the "Click to …" hint), and the click `action`. Delivery then goes to either the hook or terminal-notifier. terminal-notifier output stays word for word what it is today: the hint is added at delivery time.
2. **Run the hook** when the job has `notify`:
   - `subprocess.run(command, input=<payload JSON>, cwd=workdir, env=child_env(), timeout=60)`, with stdout and stderr appended to the job's log. Tests shorten the timeout with `SCHEDULED_JOBS_HOOK_TIMEOUT`.
   - Payload (version 1): `version`, `slug`, `name`, `label`, `runId`, `source`, `status` (`success`, `failed`, `timeout`, or `stopped`), `title`, `message`, `error`, `exitCode`, `startedAt`, `finishedAt`, `durationMs`, `expectedFile`, `logPath`, `logTail` (up to the last 50 lines of this run's part of the log), `sessionId`, and `resumeHint` (the log's `cd … && claude --resume …` line, or null).
   - Skipped runs (overlap or paused) still send nothing.
   - `NOTIFY_HOOK_BIN`, when set, replaces the whole command with that one executable, the way `NOTIFIER_BIN` replaces terminal-notifier. Tests and the skill's fake run use it, so a fake run never sends a real email.
3. **Fall back when the hook fails.** A nonzero exit, a timeout, or a command that can't start gets a log line (`notify hook failed (…)`), then the usual terminal-notifier notification, with " The notify hook failed; see the log." added to its message. So a broken mail setup still shows up on the Mac.
4. **Run record.** Add `notified`: `"hook"`, `"hook-failed"`, `"terminal-notifier"`, or `"none"`. That lets `list --json`, and anyone debugging, see whether the email went out.

## Postmark sender (`skills/scheduling-agent-jobs/scripts/notify_postmark.py`)

- Stdlib only, and runs on `/usr/bin/python3`. It reads the payload from stdin.
- Arguments: `--from`, `--to` (repeatable), `--token-var` (required), `--env-file` (optional; without it the variable comes from the environment), and `--message-stream` (default `outbound`).
- The env-file parser handles `KEY=value`, `export KEY=value`, single or double quotes, blank lines, and `#` comments. It never prints the token.
- Subject: `<title>: <message>`, for example "X mentions ready: 3 new X posts."
- Body (`TextBody`):
  - On success with an `expectedFile`, the file's contents. With no file, the message.
  - On every other status, the error, the log path, `resumeHint` when it's set, and `logTail`.
- Sends `POST https://api.postmarkapp.com/email`, with the URL overridable through `POSTMARK_API_URL` for tests. It exits 0 on HTTP 200 with `ErrorCode` 0. Otherwise it prints Postmark's `ErrorCode` and `Message` to stderr and exits 1, which triggers the fallback.

## Tests

- `test_jobs.py` (fake hook scripts, with `SCHEDULED_JOBS_NO_LAUNCHD=1` and a temporary home as today):
  - A hook gets a valid payload for success, failure, and timeout.
  - A hook replaces terminal-notifier, so the fake notifier isn't called.
  - A failing or hanging hook falls back to terminal-notifier with the added message.
  - Skipped runs don't call the hook.
  - Validation rejects bad `notify` shapes and a relative or missing first argument.
  - `~` arguments expand.
  - The `notified` value is recorded.
- New `skills/scheduling-agent-jobs/tests/test_notify_postmark.py`: a local `http.server` stands in for Postmark.
  - Success body is the output file, and the failure body has the error and tail.
  - Env-file parsing, a missing variable, and a missing file each exit 1 with a clear message.
  - Postmark error responses exit 1.
  - The token never appears in stdout or stderr.
- AGENTS.md's "Before every commit" list gains the new test command.

## Docs

- README: a short "Send results somewhere else" section after "How it works and what to watch". It covers the `notify` field, the Postmark example, the fallback, and that the token lives in a file the job can read, outside the protected folders.
- SKILL.md: a `notify` row in the field table, and a step 4 change: for a job with `notify`, the fake run sets `NOTIFY_HOOK_BIN` to a fake that saves its stdin, and checks the payload instead of the notifier's arguments.
- RECIPE.md and the mod need no change: the mod doesn't send notifications, and `list --json` gains only the new run field.

## Out of scope

- Several channels per job, HTML email, and per-status recipients.
- Any provider besides Postmark in the repo.
- Changes to the Schedules mod.
