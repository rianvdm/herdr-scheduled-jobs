# Notify hook design

*2026-11-21*

## Problem

The scheduler tries to report each finished run through terminal-notifier, and nothing else. Someone who wants a scheduled report in their inbox, or who needs to know a 5am job failed while they were away from the Mac, has no way to get the result anywhere else. The first case is a twice-daily report job whose owner wants the report emailed on success and the error emailed on failure, in place of the notification.

## Outcome

A job spec can name a command that `jobs.py` runs after every finished run (success, failure, timeout, or stop) instead of the macOS notification. The repo ships one such command, a Postmark sender, so a job can email its output on success and the error otherwise. Anyone can use it with their own Postmark server token, or write a command for another service. If the command fails, the run falls back to the notification. A spec without the new field keeps today's notification, run record, and text, plus one fix: a count that can't be read gives "Finished." instead of an error.

## Spec field

```json
"notify": {
  "command": ["/usr/bin/python3", "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
              "--from", "reports@example.com", "--to", "me@example.com",
              "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]
}
```

- `notify` is optional. Its only key is `command`, a non-empty list of strings.
- `validate()` expands a leading `~` in each item, the way it expands `workdir`, and keeps the path as written, without resolving symlinks. That way the skill link still works after the clone moves. The first item must be an absolute path.
- `validate()` doesn't check that the file exists, so `relink` never fails because a hook is missing. A missing command fails at run time and falls back to the notification.
- The spec and the command's arguments never hold a secret. The Postmark sender takes the name of a variable and the file that holds it.

## Runner changes (`jobs.py`)

1. **Separate the outcome from its delivery.** `run_outcome()` builds the title, a message without the click hint, the click action, and the hint, from the same rules as today. terminal-notifier text stays word for word the same, because the hint is added at delivery time. If the count can't be read from the output file, the message falls back to "Finished.", where today it raises.
2. **Keep the run record where it is.** The record is still appended before delivery, unchanged, so the smoke test and anything waiting on the record behave as today. How the result went out goes in the log instead: `notify hook: done` or `notify hook failed (<reason>)`.
3. **Run the hook** when the job has `notify`:
   - Start the command directly, with no shell, in `workdir` with `child_env()` and in a new process group.
   - Write the payload to stdin as one UTF-8 JSON object, then close stdin. The hook's stdout and stderr go to the job's log.
   - The deadline is 60 seconds. Tests shorten it with `SCHEDULED_JOBS_HOOK_TIMEOUT`, which is read when the hook starts, and a value that isn't a positive integer is ignored. At the deadline, kill the process group and reap the hook.
   - Failure reasons in the log are fixed strings: `exit <n>`, `timed out after <n>s`, `could not start: <ExceptionType>`, or `payload: <ExceptionType>`. They never include the command line, since a custom hook's arguments could hold something private.
   - `NOTIFY_HOOK_BIN`, when set (even to an empty string, which then fails to start and falls back), replaces the whole command with that one executable, the way `NOTIFIER_BIN` replaces terminal-notifier. The skill's fake run uses it so that a fake run never sends a real message. The test fixture clears any inherited value.
4. **Payload, version 1.** Optional values that are absent are JSON null. The fields are:
   - The job and run: `version`, `slug`, `name`, `label`, `runId`, `source`, and `status` (`success`, `failed`, `timeout`, or `stopped`).
   - The outcome: `title`, `message`, `error`, and `exitCode`.
   - Timing: `startedAt`, `finishedAt`, and `durationMs`.
   - Paths: `expectedFile` and `logPath`. A relative `expectedFile` is resolved against `workdir` once, before the run, and that same absolute path is used for the freshness check, the count, and the payload.
   - From this run's log: `logTail`, which starts at the line that begins `##### Started` and ends `· run <runId> #####`, and leaves out other invocations' `##### Skipped` lines. It keeps at most the last 50 lines and 16,384 characters, and is empty without that line.
   - From the agent: `sessionId`, `resumeHint` (the log's `cd … && claude --resume …` line), and `resultText` (the agent's final message). `resultText`, `error`, and `message` are each capped at 16,384 characters.
5. **Fall back when the hook fails.** A nonzero exit, a timeout, or a start failure (including `OSError` and the `ValueError` an embedded NUL raises) gets its log line, then the usual terminal-notifier notification with " The notify hook failed; see the log." added after the click hint. The run's status and exit code don't change.
6. **Skipped runs** (an overlap or a paused job) still send nothing.

## Postmark sender (`skills/scheduling-agent-jobs/scripts/notify_postmark.py`)

- Stdlib only, and runs on `/usr/bin/python3` 3.9. It reads the payload from stdin and exits 1 on malformed JSON, a non-object, or a `version` that isn't the integer 1 (`true` is rejected).
- Arguments: `--from`, `--to` (one or more), `--token-var` (all three required), `--env-file`, and `--message-stream` (default `outbound`). Recipients are joined into Postmark's `To` string.
- With `--env-file`, the token comes only from that file. Without it, the token comes from the environment.
- The file parser accepts these forms:
  - `KEY=value` and `export KEY=value`.
  - Single- or double-quoted values, keeping `#` inside the quotes.
  - A ` #` comment after an unquoted value or after the closing quote.
  - Blank lines and full-line comments.
- The last assignment of a key wins, and the parser never evaluates shell syntax. A missing, unreadable, or empty token, or one with spaces or control characters, gives a fixed message before any request is built.
- Subject: `<title>: <message>` on one line, capped at 200 characters, for example "Report ready: 3 new items."
- Body (`TextBody`):
  - **Success with an `expectedFile`:** the file's contents, up to 1,000,000 characters, marked if truncated. If the file can't be read, the sender exits 1 so the runner falls back. A success email without the report would hide the problem.
  - **Success without a file:** `resultText`, then `message`.
  - **Any other status:** the error, the log path, `resumeHint` when set, and `logTail`.
- Sending:
  - One `POST` to `https://api.postmarkapp.com/email`, with normal TLS verification, redirects rejected, a 20-second timeout, and at most 64 KiB of response read. `POSTMARK_API_URL` overrides the URL for tests, and only a `http://127.0.0.1` or `http://localhost` URL is accepted, so an inherited value can't send the token elsewhere.
  - Success means HTTP 200 with a JSON object whose integer `ErrorCode` is 0. That prints `notify_postmark: sent`.
  - Any request or response error (including a truncated reply) gives one `notify_postmark: …` line naming only the exception type. A Postmark error reply prints the HTTP status, `ErrorCode`, and Postmark's `Message`, with every occurrence of the token replaced by `[token]`. Both exit 1.

## Tests

- `test_jobs.py`, using fakes, a temporary state folder, and `SCHEDULED_JOBS_NO_LAUNCHD=1`:
  - **Unit tests:** `run_outcome()` texts and the count fallback, and `run_tail()` covering this run only, the skip lines it leaves out, the line cap, and the missing marker.
  - **The hook in place of the notifier:** it replaces terminal-notifier, its payload is right for success, failure, and timeout, `resultText` is set, and the log says `notify hook: done`.
  - **Fallback:** it covers a nonzero exit, a timeout that kills the hook, a hook deleted after `add`, and a NUL in an argument, each giving the exact fallback text.
  - **The override:** `NOTIFY_HOOK_BIN` replaces the configured command.
  - **Skips:** neither skip path calls the hook.
  - **Validation:** bad shapes and a relative first item are rejected, a missing absolute path is accepted, and `~` expands.
- `skills/scheduling-agent-jobs/tests/test_notify_postmark.py`, using a loopback `http.server` as Postmark and a dummy token:
  - The success body with and without a file, and the failure body.
  - Several recipients.
  - Env-file parsing (bare, quoted, `export`, and inline comments), the token from the environment, and a missing variable or file.
  - An unreadable output file sends nothing.
  - A Postmark error, a redirect, a non-200 response with `ErrorCode` 0, a non-object reply, and an error message that echoes the token.
  - Every invocation checks that the token appears in neither stdout nor stderr.
- AGENTS.md's "Before every commit" list gains the sender's test command.

## Docs

- **README:**
  - Update the introduction and the Notifications bullet: terminal-notifier is the default, and the fallback for a failed hook.
  - Add a "Send results somewhere else" section covering the `notify` field and the Postmark example:
    - The sender address must be verified in the user's Postmark account.
    - The token file must be readable by jobs, outside the protected folders, and out of git.
    - The failure email includes the end of the log.
    - terminal-notifier is still required for the fallback.
    - Remove `notify` from a job before going back to an older `jobs.py`, which rejects the unknown field.
- **SKILL.md:**
  - The introduction and a `notify` row in the field table.
  - Step 4's fake run becomes a single invocation that fakes Claude Code, terminal-notifier, and the hook together.
  - In step 5, the real-run offer names the message the hook sends and its recipients. After the run, wait for `running` to be false, read that run's part of the log for its `notify hook` line, and check that the message arrived. `notify hook failed` means the hook didn't confirm delivery.
  - README: the "Looking into a run" bullet says the log includes the `claude --resume` line, since hook output can follow it.
- **RECIPE.md and the mod:** no change. The mod reads the existing `list --json` fields and shows its own toasts for its buttons, and the run record doesn't change.

## Out of scope

- Several channels per job, HTML email, and per-status recipients.
- Any provider besides Postmark in the repo.
- Recording delivery in the run record, and any change to the Schedules mod.
