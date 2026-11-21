---
name: scheduling-agent-jobs
description: Use when the user asks to run a slash command or prompt on a schedule ("run /morning-report every morning at 5am", "every Friday at 4pm"), to be notified when a scheduled job finishes or fails, to see what's scheduled, to change, pause, test, or remove a scheduled job, or to re-point jobs at this copy after moving the repo.
---

# Scheduling Agent Jobs

`scripts/jobs.py` schedules unattended Claude Code runs (`claude -p`) through launchd on macOS. The scheduler prevents overlapping runs of the same job, enforces a timeout, and records run history. Each run's stream is written to a readable log, and each completed run tries to send a persistent notification through terminal-notifier; clicking it runs the job's open action. If the notification can't be sent, or terminal-notifier isn't installed, the log says so. When a job has a `notify` command, `jobs.py` uses it to send the result and falls back to the notification if the command fails. The bundled Postmark sender can email the result. The runner doesn't see whether the command a click runs succeeds. A run skipped because the previous one is still going, or because the job is paused, is recorded without a notification. The optional **Schedules** Herdr mod shows every job and has Run now, Pause/Resume, Log, and Open output buttons. It calls `jobs.py` to list, run, pause, and resume jobs, and Herdr handles Log and Open output directly.

This skill lives in the herdr-scheduled-jobs repo, and the repo's `install.sh` links it into `~/.claude/skills/`. Find the repo with `cd -P "<base directory>/../.." && pwd`. Setup is in the repo's `README.md`. If the user hasn't run the smoke test on this Mac, or isn't sure, run it first. The mod's source is `mods/schedules/` in the repo. After editing it, re-run `install.sh`, which copies it into `~/.config/herdr-app/mods/schedules/` and, when herdr-app is on PATH, runs `herdr-app check`.

A job is done when it is installed, has passed a fake run, has had one real run or the user has declined one, and the user has the log path and either the next run time or word that the job is paused.

Claude Code reports this skill's folder when it loads it ("Base directory for this skill: …"). Put that path in place of `<base directory>` here and below:

```bash
J="<base directory>/scripts/jobs.py"
/usr/bin/python3 "$J" list                               # add --json for the full record
/usr/bin/python3 "$J" add "/tmp/morning-report.json"     # install, or replace the job with the same slug
/usr/bin/python3 "$J" pause morning-report
/usr/bin/python3 "$J" resume morning-report
/usr/bin/python3 "$J" run morning-report
/usr/bin/python3 "$J" remove morning-report
/usr/bin/python3 "$J" relink                             # re-point every job at this copy of jobs.py; paused jobs stay paused
/usr/bin/python3 "$J" smoke-test                         # a read-only run under launchd that reports gh, glab, and claude mcp list, then cleans up
```

The examples use a job called `morning-report`; use the job's own slug.

State lives in `~/.config/scheduled-jobs/`: `jobs/<slug>.json` (the spec), `runs/<slug>.jsonl`, `logs/<slug>.log`, and `jobs-path` (where `jobs.py` is, for the Herdr mod). Plists are `~/Library/LaunchAgents/local.scheduled-jobs.<slug>.plist`. Change a job by editing a copy of its spec and running `add` again. For an enabled job, `add` writes the plist and loads it. For a paused one, it removes the plist. Each plist records the real path of the `jobs.py` that ran `add`, so after the repo moves, run `relink` (or `install.sh`) from the new copy.

## 1. Read what the job will run

Read the command file the job runs in full, including any configuration it reads. In Claude Code, a slash command is `.claude/commands/<name>.md` in the job's working folder or `~/.claude/commands/<name>.md`, and a skill is the `SKILL.md` in its folder under `.claude/skills/` or `~/.claude/skills/`. If the user supplies a prompt instead of a slash command, use it as the job's instructions. Note:

- **Working folder:** resolve the real path of the job's `workdir`. If it's under `~/Documents`, `~/Desktop`, `~/Downloads`, or iCloud Drive, tell the user scheduled jobs can't read files there, and ask for another location.
- **Output file:** the path it writes. `jobs.py` can't compute a path per run, such as a dated or week-numbered folder. For such a command, offer the user a stable `expectedFile` (for example, have the prompt also write a fixed `latest.md`), or stop and ask.
- **Steps that wait on the user:** questions, confirmations, opening an app or editor, and committing.
- **One-way actions:** marking email read, posting, and sending. Note what must succeed before each one.
- **Sources:** every tool it calls, such as an MCP server, gh, glab, or AppleScript. Each can fail unattended: a sign-in can expire before the next run, and an app's first AppleScript call under launchd waits on a macOS Automation prompt that only the step 5 real run can surface.
- **Sign-in lifetimes:** an unattended run can't sign in again. A tool whose login expires between runs fails once it lapses, so reach that source through a tool with a longer-lived login if there is one.
- **Dates:** if the command targets "today" or "tomorrow", check the schedule against that. For example, a weekday 6pm run of "tomorrow" briefs Saturday and never Monday.

## 2. Write the prompt

The prompt has these parts, in order:

1. `Run the /<name> slash command: read <path to the command file> in full and follow it exactly`, plus any arguments. For a prompt of the user's own, use their prompt here.
2. `This is an unattended scheduled run, so nobody can answer questions or approve anything.`
3. A replacement for each step that waits on the user, such as "leave missing links unlinked and list them in your final report". Always tell the job not to open any app or editor: `Do not open any app or editor.`
4. If the command reaches a source through a tool whose login won't last until the run, name the tool to use instead for this run.
5. `If <each source the command needs> fails or reports an auth error before the output file is written, stop immediately: take no one-way action and do not overwrite the output file. Print the exact error and finish. After the file is written, follow the command's own recovery steps and correct the file to match what actually happened. If a step after the write still fails, end your final message with a line starting PARTIAL: that says what failed.` The runner records a run whose final message has a `PARTIAL:` line as failed. For a source the output can do without, say what to write in its place and continue instead.

## 3. Write the spec and install it

```json
{
  "name": "Morning report",
  "slug": "morning-report",
  "label": "Morning report",
  "schedule": "0 5 * * 1-5",
  "workdir": "~/notes",
  "timeoutSeconds": 1800,
  "prompt": "Run the /morning-report slash command with no arguments: read .claude/commands/morning-report.md in full and follow it exactly. This is an unattended scheduled run, so nobody can answer questions or approve anything. Do not open any app or editor. If a source the report needs fails or reports an auth error before morning-report.md is written, stop immediately: take no one-way action and do not overwrite morning-report.md. Print the exact error and finish. After morning-report.md is written, follow the command's own recovery steps, and if a step after the write still fails, end your final message with a line starting PARTIAL: that says what failed.",
  "expectedFile": "~/notes/morning-report.md",
  "countRegex": "[0-9]+ items? reviewed",
  "countNoun": "items reviewed",
  "paused": true
}
```

| Field | Meaning |
|---|---|
| `schedule` | 5-field cron in the Mac's local time, numbers only. `0 5 * * 1-5` is 5am on weekdays. |
| `timeoutSeconds` | About 5–10× a normal run. A job that takes 4–5 minutes can use 1800. |
| `label` | Notification title prefix. Defaults to `name`. |
| `expectedFile` | A run succeeds only when Claude exits 0, reports no error, **and** this file's modification time moved forward during the run. An agent that stops on an auth error still exits 0. Omit it only when the job writes no file, and then the prompt must end with a `PARTIAL:` line whenever a step it needs fails, or the failure looks like a success. `~` paths are expanded. |
| `open` | What clicking the notification does on success: a command as a list of strings with an absolute path, a URL, or a file path (opened with `open -t`). Without it, the click shows `expectedFile`, and with neither set, it opens the log. Failures always open the log. |
| `notify` | Optional. Set `{"command": [...]}` to use a custom delivery command. After each finished run, `jobs.py` runs the command instead of the macOS notification and passes the run details as JSON on stdin. If the command fails, `jobs.py` falls back to the notification. Use an absolute path for the first item; a leading `~` expands. `scripts/notify_postmark.py` emails the output on success, or the error and log tail otherwise. Markdown output up to 100,000 characters also goes out as HTML. See the README for Postmark setup. Pass `--env-file` and `--token-var` to the sender, and keep secrets out of the spec. |
| `countRegex` / `countNoun` | Optional. The first number matched in `expectedFile` goes in the success message. |
| `model` | Optional `--model` value. Without it, Claude Code's default applies. |
| `claudePath` | Filled in by `add` from where Claude Code is installed. Set it only to pin a different binary. |
| `paused` | `true` installs it without scheduling it: the job file is saved, but no plist is written, so nothing loads at login. Install new jobs paused, so nothing runs before step 5. |

Save the spec outside the repo (for example in `/tmp`). Before `add`, check `list --json` for a job with the same slug. If one exists, `add` would replace it, so show the user the existing job and ask first. Then run `add`. The job is installed but not scheduled until step 5.

### Opening the output in an app

Some apps' command-line tools open a file without bringing the app forward. For those, set `open` to run the tool through `/bin/sh -c`, followed by `/usr/bin/open -a <App>`:

```json
"open": ["/bin/sh", "-c", "<the app's CLI, by absolute path> <arguments>; /usr/bin/open -a <App>"]
```

Example only, for Obsidian (use whichever app the output belongs in):

```json
"open": ["/bin/sh", "-c", "/Applications/Obsidian.app/Contents/MacOS/obsidian open vault=notes path=morning-report.md newtab; /usr/bin/open -a Obsidian"]
```

The click runs with a minimal PATH, so use absolute paths.

## 4. Test with a fake run

1. Run `/usr/bin/python3 -B "<base directory>/tests/test_jobs.py"` and require every test to pass. The suite covers cron parsing, plists, success, an untouched file, nonzero exits, agent errors, timeouts, overlapping runs, relinking, and the smoke test.
2. Run the job against fakes. Fakes stand in for Claude Code, terminal-notifier, and the `notify` command, so no notification or message goes out:

   ```bash
   T=$(mktemp -d); printf '#!/bin/bash\nprintf "%%s\\n" "$*" >>%s/n; echo @CLOSED\n' $T >$T/notifier
   printf '#!/bin/bash\nexit 0\n' >$T/claude; printf '#!/bin/bash\ncat >%s/payload.json\n' $T >$T/hook
   chmod +x $T/notifier $T/claude $T/hook
   NOTIFY_HOOK_BIN=$T/hook CLAUDE_BIN=$T/claude NOTIFIER_BIN=$T/notifier /usr/bin/python3 "$J" exec morning-report --manual; echo exit=$?; sleep 1; cat $T/n $T/payload.json 2>/dev/null
   ```

   With `expectedFile` set, expect `exit=1` because the fake Claude command writes no output file. Otherwise, expect `exit=0`.
   - For a job without `notify`, expect the notifier line `<label> failed … not updated` if `expectedFile` is set. Otherwise, expect `<label> done` (`<label> ready` if the job sets `open`).
   - For a job with `notify`, there's no notifier line; check `payload.json` instead. Expect `"status": "failed"` with `not updated` in `error` when `expectedFile` is set, or `"status": "success"` otherwise. The log ends with `notify hook: done` in both cases.

   `--manual` runs the job even though it's paused. This adds one entry to the job's run history.

## 5. Real run

Offer the user a real run and explain that saying yes also turns the schedule on. Name any one-way effects, such as marking mail as read or sending a message to the `notify` command's recipients. On a yes, run `resume <slug>` and require it to succeed. Then run `run <slug>`: once the job is loaded, `run` goes through launchd, so it gets the same environment as a scheduled run. Wait until `list --json` shows a new `lastRun` with `source` `manual` before you judge the run. Then check that record and the log. For a job without `notify`, check that the notification appeared. For a job with `notify`, wait until `list --json` shows `running` false, find `notify hook: done` after that run's `##### Started … · run <runId> #####` line, and check that the message arrived. `notify hook failed` means the hook didn't confirm delivery and the runner tried a notification instead. If the run fails, `pause <slug>` again and report the error. If the user declines the real run, ask whether to resume the schedule anyway, and leave it paused unless the user says yes.

## 6. Report

Tell the user the schedule, the log path, and, if Herdr is installed, that the job appears in the Schedules tab. Give the next run from `list` if the schedule is on. If the user left it off, say the job is paused. A stale output file or a "failed" notification means the run failed. The log has the error, and a `claude --resume <session>` line when the runner caught a session ID.

## Facts that shape every job

| Fact | Consequence |
|---|---|
| launchd runs jobs | They run with Herdr and every agent app closed and the screen locked. The user must be logged in. A Mac asleep at run time runs the job on wake; a Mac that is shut down skips it, with no notification. |
| Job PATH is `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` | Homebrew tools such as gh, glab, and terminal-notifier resolve. Claude is called by the absolute path `add` found. Anything else in `~/.local/bin` or a shell profile does not resolve, so use absolute paths. |
| No shell profile runs | Claude Code's model access has to come from its own login or `~/.claude/settings.json`, not from a variable exported in a shell profile. Whatever serves the models must be up at run time, and an outage shows up as a failed run. |
| Credential lifetimes differ by source | Each tool's login lapses on its own schedule (step 1). A failed run's log is the first place to look. |
| Runs use `--permission-mode bypassPermissions` | Runs never stall on approval, and they can run any tool. Keep prompts narrow and spell out the one-way actions they may take. |
| One run at a time per job | A run that starts while the previous one is still going is recorded as `skipped`, with no notification. |
| The notification uses `-execute`, not `-action` | terminal-notifier exits once the notification is posted, and macOS relaunches it on click to run the action through `/bin/sh -c` with a minimal PATH. A waiting `-action` notifier quits as the click arrives, so macOS shows "terminal-notifier is not open anymore". Notifications persist only if terminal-notifier is set to **Alerts** in System Settings > Notifications. |

To remove a job, `remove <slug>`. Its run history and log stay.

