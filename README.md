<img src=".github/assets/logo.svg" alt="" width="96" height="96">

# herdr-scheduled-jobs

Schedule unattended Claude Code runs on your Mac. Ask Claude Code to "run /my-command every weekday at 5am", and launchd starts the run on time, even with every app closed. When a run finishes, the scheduler tries to send a macOS notification with the result. Click it to open the output on success or the log on failure. To send a job's result by email or through another command, see [Send results somewhere else](#send-results-somewhere-else). An optional Schedules tab in the [Herdr](https://herdr.dev) terminal app shows each job's next run and last result, with Run now, Pause, Log, and Open output buttons.

## Set it up with Claude Code

Start Claude Code in your clone of this repo (or anywhere, if you haven't cloned it yet) and paste this prompt. Claude Code runs the commands and stops at each step that needs you.

```text
Set up herdr-scheduled-jobs on this Mac.

1. Run `git --version` and `claude --version`. If macOS offers to install the Command Line Tools, stop and wait for me to finish that install.
2. Ask me whether I want the optional Herdr tab, which folder my first scheduled job will run in (the clone, if I don't have a job in mind yet), which tools and accounts that job needs, and how Claude Code gets model access on this Mac: my own Claude login or an organization's proxy.
3. Find my clone of herdr-scheduled-jobs. If the current folder isn't one, ask me whether I have one and where it is.
   - If I have one, resolve its real path. If that's under ~/Documents, ~/Desktop, ~/Downloads, or iCloud Drive, ask me where to make a fresh clone, and leave the old one in place. Otherwise pull only if it has no local changes, its upstream is the rianvdm/herdr-scheduled-jobs remote, and `git pull --ff-only` succeeds.
   - If I don't, ask me which folder to clone it into, and suggest ~/git/herdr-scheduled-jobs. Never use ~/Documents, ~/Desktop, ~/Downloads, or an iCloud Drive folder, because macOS doesn't let scheduled jobs read files there. Clone with `git clone https://github.com/rianvdm/herdr-scheduled-jobs.git <folder>`. If that fails, show me the error and wait for me. Don't move on without me.
4. Read README.md in full before you run anything else. If I chose an organization's proxy in step 2, ask me for its setup steps and use them in Prerequisite 3.
5. Do the steps under "Prerequisites" one at a time. Check what's already in place before you change anything. Run each command yourself and show me its output. A missing tool or an expired sign-in is something to ask me about, not a failure. For model access, require only the sign-in for the route I chose. Run the terminal-notifier test notification in the background, not in a command that waits for it, and tell me to answer any macOS prompt or the notification. Check that it exited 0 once it finishes.
6. Stop and tell me exactly what to do at each step that needs me, such as a browser sign-in, `gh auth login`, `glab auth login`, a Claude Code login, `/mcp`, or a System Settings change. Wait for me to say it's done, then check that it worked.
7. Before any brew command, check that `brew` exists. If it doesn't, stop, point me to https://brew.sh, wait, and then check that `brew --version` works in your shell. Ask me before any `brew install`.
8. Before you run install.sh, tell me what it will do, as "Install" describes, including whether it will update my existing jobs to use this clone. If I want the tab, tell me the Herdr mod reads the scheduler's state in ~/.config/scheduled-jobs and can run each job's open command. If I want the tab and `herdr-app` isn't found, tell me to start Herdr or put herdr-app on my PATH, and wait. Then wait for my go-ahead before you run install.sh.
9. Run ./install.sh from the clone, with --no-mod if I don't want the tab. If it exits 2, show me why the installer stopped, ask me about --replace-skill and --replace-mod separately, and rerun only with the flags I approve individually. If it exits 5, tell me which job was running (the `skipped` line), wait for that run to finish, run the `relink` command the installer printed, and only then go on. For any other nonzero exit, show me the output and stop. If I want the tab, the output must include "herdr: herdr-app check passed".
10. Before the smoke test, resolve the real path of the folder from step 2. If it's under ~/Documents, ~/Desktop, ~/Downloads, or iCloud Drive, tell me scheduled jobs can't read files there and ask for another folder. Tell me the test runs Claude Code unattended in that folder with every permission prompt skipped, and that the folder's own Claude Code settings and hooks load there. Offer the clone as the default folder, and wait for my go-ahead.
11. Run the smoke test as "Check unattended runs" describes, with --workdir set to that folder, and give the command a 10-minute timeout. It passes only with exit 0 and a line starting "Result: success". Then review the table with me, covering each row my job needs. If I have no job planned yet, tell me that only the scheduler and model access have been checked.
12. If I want the tab, run `herdr-app do tab.new --name Schedules --mod schedules`, and ask me to confirm the tab shows job cards or a message starting "No jobs yet".

If a command fails, stop and show me the exact error instead of working around it. Don't create a lasting schedule.
```

## Prerequisites

These are for macOS, which runs the jobs through launchd. Run each command in Terminal.

1. **git and Apple's command line tools.** Check with `git --version`. If macOS offers to install the command line tools, accept and wait for the install to finish. The same tools provide `/usr/bin/python3`, which the scheduler runs on.
2. **Homebrew.** Check with `brew --version`. If it's missing, install it from [brew.sh](https://brew.sh).
3. **Claude Code.** Install it with [its setup guide](https://docs.claude.com/en/docs/claude-code/setup), and check that `claude --version` works. Choose how Claude Code will access models: your own Claude login or your organization's proxy. Sign in only for the option you use. Scheduled jobs don't load a shell profile, so Claude Code must use its own login or `~/.claude/settings.json`. A key you export in `~/.zshrc` isn't available to a scheduled job.
4. **terminal-notifier**, which shows the notifications:

   ```bash
   brew install terminal-notifier
   terminal-notifier -title "Scheduled jobs" -message "Test notification"
   ```

   The test notification adds terminal-notifier to System Settings → Notifications. Allow it if macOS asks. The first test can take a few minutes to return, for example while macOS waits for you to allow notifications or click the test. Then open that pane, choose terminal-notifier, and set the style to **Alerts** (**Persistent** on macOS 27), so notifications stay until you click them.
5. **Your jobs' tools.** A job can only use tools that are installed and signed in. For example, a job that reads GitHub needs `gh`: run `brew install gh` and `gh auth login`, then check with `gh auth status`. The same goes for `glab` and GitLab.
6. **Herdr** (optional), for the Schedules tab. Install it from [herdr.dev](https://herdr.dev) and open it before you run `install.sh`. Then check that `herdr-app status` prints the window's state. If `herdr-app` isn't found, it isn't on your PATH yet, and the installer can't check the mod.

## Install

Keep the clone outside `~/Documents`, `~/Desktop`, `~/Downloads`, and iCloud Drive. macOS privacy protection stops launchd jobs from reading files in those folders, so every job would fail with no notification. `install.sh` refuses to run from them.

From your clone:

```bash
./install.sh
```

It checks the prerequisites, then:

1. Links `skills/scheduling-agent-jobs` to `~/.claude/skills/scheduling-agent-jobs`, where Claude Code finds it.
2. If Herdr is installed, copies the mod's files (`main.js` and `mod.json`) into `~/.config/herdr-app/mods/schedules/`. It runs `herdr-app check` on them only when it finds `herdr-app` on the PATH.
3. Updates any existing jobs to use this clone's `jobs.py`.

It never installs packages, changes Claude Code's settings or MCP servers, runs the smoke test, or schedules a job. It's safe to run again. The installer stops without changing anything if it finds something it didn't link at the skill path, a mod it didn't install in the mod folder, or changes you've made to an installed mod file. Re-run it with `--replace-skill` or `--replace-mod` to replace that item. The installer first moves the old item to `~/.config/herdr-scheduled-jobs/backups/<date-time>/`. Use `--no-mod` to skip the Herdr mod and its check, even if Herdr is installed. With `--no-mod`, the installer ignores `--replace-mod`.

| Exit code | Meaning |
|---|---|
| 0 | Done. |
| 1 | A prerequisite is missing, the clone is in a folder jobs can't read, or a step failed partway. Read the message. |
| 2 | Stopped to protect existing files. Nothing changed. |
| 3 | Installed, but `jobs.py relink` exited nonzero. The output usually includes one or more `jobs:` lines saying `failed`. Read the full output, since an error can also occur before any job is processed. Fix the cause, then run the `relink` command it prints. When `herdr-app check` also failed, the exit code is still 3. |
| 4 | Installed, but `herdr-app check` failed on the mod. It can't happen with `--no-mod`. |
| 5 | Installed, but `jobs.py relink` skipped a job because it was running, so that job still points at the old `jobs.py`. Once the run finishes, run the `relink` command it prints. If a relink also failed, the installer exits 3. |

Each existing job gets a `jobs:` line. `relinked` means it now runs this clone's `jobs.py`. `skipped <slug>: running now, re-run relink after it finishes` means a run was in progress, so the installer left that job alone. Once the run finishes, run:

```bash
/usr/bin/python3 skills/scheduling-agent-jobs/scripts/jobs.py relink
```

## Check unattended runs

```bash
/usr/bin/python3 skills/scheduling-agent-jobs/scripts/jobs.py smoke-test
```

The smoke test creates a temporary job, runs it under launchd, prints the result, and removes the job. It usually takes under a minute and times out after 8 minutes. Its report (`~/.config/scheduled-jobs/smoke-test.md`), run history, and log stay.

The test passes only if it exits 0 and prints a line starting `Result: success`. That confirms Claude Code could access its models unattended. The table shows sign-in status for `gh` and `glab` if installed, along with the `claude mcp list` status for each configured MCP server. A server can appear connected even if a service behind it fails. The first real run checks the services the job uses. Read each row your job needs; the test doesn't use the table to decide whether it passed.

By default, the test runs from your current folder. To test a job's folder with its own Claude Code settings and hooks, pass `--workdir` (for example, `--workdir ~/notes`). Like the clone, that folder must be outside `~/Documents`, `~/Desktop`, `~/Downloads`, and iCloud Drive. If the test is interrupted and the next try says a smoke-test job already exists, remove it and try again:

```bash
/usr/bin/python3 skills/scheduling-agent-jobs/scripts/jobs.py remove smoke-test
```

## Open the Schedules tab

If you use Herdr:

```bash
herdr-app do tab.new --name Schedules --mod schedules
```

The tab shows a card per job, or a message starting "No jobs yet". It runs `jobs.py` to list jobs and to run, pause, and resume them. Its Log button opens a terminal that follows the log, and its Open output button opens the job's output. `RECIPE.md` describes the mod in full.

## Schedule a job

Start Claude Code in the folder the job should run in, and ask, for example, "run /my-command every weekday at 5am". The skill reads the command, writes a prompt for an unattended run, installs the job paused, and tests it with a fake run. It then offers one real run. Saying yes turns the schedule on and runs the job right away. If that run fails, the skill pauses the job again.

To see or change jobs later, ask Claude Code, use the Schedules tab, or run `jobs.py` yourself. For a job called `morning-report`:

```bash
J=~/.claude/skills/scheduling-agent-jobs/scripts/jobs.py
/usr/bin/python3 "$J" list
/usr/bin/python3 "$J" pause morning-report
/usr/bin/python3 "$J" resume morning-report
/usr/bin/python3 "$J" run morning-report
/usr/bin/python3 "$J" remove morning-report
```

## How it works and what to watch

Each scheduled job is a launchd agent in `~/Library/LaunchAgents/local.scheduled-jobs.<slug>.plist` that runs `jobs.py exec <slug>`. A paused job has no plist. Job specs, run history, and logs are in `~/.config/scheduled-jobs/`.

- **When jobs run:** while you're logged in, even with Herdr and every terminal closed and the screen locked. If the Mac is asleep at run time, the job runs when it wakes. If the Mac is shut down or you're logged out, the run is skipped, and no notification appears.
- **Permissions:** jobs run Claude Code with `--permission-mode bypassPermissions`, so a run never stops to ask for approval and can use any tool, including ones that delete files or send messages. Keep each job's prompt narrow, and list any actions it may take that can't be undone. The skill writes prompts this way.
- **Success:** a run succeeds only when Claude Code exits cleanly and reports no error. For a job with an output file, the runner also checks that the file's modification time moved forward during the run. For jobs with no output file, write the prompt so Claude ends with a line starting `PARTIAL:` if a required step fails. Otherwise, a failed step can look like a successful run. A `PARTIAL:` line always marks the run as failed.
- **Notifications:** every run that finishes tries to send one, and it stays on screen only when terminal-notifier is set to Alerts (Persistent on macOS 27). Clicking it opens the output on success and the log on failure. A run that starts while the last one is still going is skipped, with no notification. For a job with `notify`, the runner sends the result through that command and tries a macOS notification if it fails.
- **Looking into a run:** if the runner captures Claude Code's session ID, the run's log includes a `claude --resume <session>` line.
- **Sign-ins expire:** an unattended run can't sign in again. If a tool's login expires between runs, the next run of a job that needs that tool fails. Check how long each tool stays signed in.
- **The job's PATH** is `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`, and no shell profile runs. Jobs can find Homebrew tools on this PATH. In job prompts, use absolute paths for tools in `~/.local/bin` or tools your shell profile adds.

## Send results somewhere else

By default every finished run tries to post a macOS notification. Set `notify` to choose how a job sends its results instead. After each finished run, `jobs.py` calls that command in the job's folder and passes the run details as JSON on stdin. The JSON includes:

- the status (`success`, `failed`, `timeout`, or `stopped`), title, message, and error
- the output file and log path
- the last lines of this run's log
- the agent's final message and, when available, a `claude --resume` line

Use an absolute path for the command's first item. `jobs.py` expands a leading `~` in any item. Skipped runs send nothing.

The repo includes a Postmark sender. On success, the sender emails the job's output file or, if the job has no output file, the agent's final message. For Markdown (`.md`) output, the email also includes a rendered HTML version, which mail clients display. Reports over 100,000 characters go out as plain text only. For any other result, the sender emails the error and the end of the log. Set it up with a `notify` block like this:

```json
"notify": {
  "command": ["/usr/bin/python3", "~/.claude/skills/scheduling-agent-jobs/scripts/notify_postmark.py",
              "--from", "reports@example.com", "--to", "me@example.com",
              "--env-file", "~/notes/.env", "--token-var", "POSTMARK_SERVER_TOKEN"]
}
```

- Use a Postmark **server** token and send from an address or domain verified in the same account.
- Keep the token in a file the job can read, outside `~/Documents`, `~/Desktop`, `~/Downloads`, and iCloud Drive, and out of git. The spec names the file and the variable, never the token.
- The failure email includes the end of the log, which can show commands the run used and local paths.
- If the command exits nonzero, runs longer than 60 seconds, or can't start, the runner logs `notify hook failed (…)`. It falls back to the macOS notification and adds "The notify hook failed; see the log." The runner logs `notify hook: done` for a delivered result. Keep terminal-notifier installed for that fallback.
- Older copies of `jobs.py` reject the `notify` field. Remove `notify` from the job before switching to an older copy of `jobs.py`.

## Update

```bash
git pull --ff-only && ./install.sh
```

The skill updates as soon as you pull, since it's a link to your clone. `install.sh` updates the Herdr mod. If you've edited any of the installed mod files, it stops and lists the edited files.

To move the clone, clone the repo in the new location and run `./install.sh --replace-skill` there. Jobs keep using the old `jobs.py` until the installer relinks them. Check the `jobs:` lines for `relinked`. For any job marked `skipped`, wait for its run to finish and relink it. Delete the old folder only after every job is relinked or the output says `No jobs to relink.`

## Uninstall

```bash
./uninstall.sh
```

`./uninstall.sh` removes the skill link if it points at this clone. If you haven't edited the mod files the installer put there, the script removes those files, the mod's marker, and the files `herdr-app check` generated. It then removes the mod folder if it's empty. It leaves other files alone. By default, it also leaves your jobs in place and prints the command to remove each one. To remove the jobs as well, run `./uninstall.sh --remove-jobs`.

Remove your jobs before you delete the clone. A job whose `jobs.py` is gone fails without a notification, because the notification comes from `jobs.py`. Run history and logs stay in `~/.config/scheduled-jobs/`, and installer backups stay in `~/.config/herdr-scheduled-jobs/`. Delete those folders if you don't want them.

## License

MIT. See [LICENSE](LICENSE). This project isn't affiliated with Anthropic or Herdr.
