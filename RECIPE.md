# Schedules

A Herdr tab that lists the Claude Code jobs `jobs.py` runs on a schedule. Each job gets a card with its schedule, its next run, how its last run went, dots for its recent runs, and buttons to run it now, pause or resume it, follow its log, and open its output. The tab updates by itself when a run starts or finishes, or when a job changes from the command line.

## What you see

The tab is one pane, top to bottom, on the theme's `canvas` color (`background` if the theme has none), with 14px padding, 10px between rows, and a vertical scrollbar when the cards overflow.

1. **Header row.** A 14px clock icon in `muted`, then "Scheduled jobs · 3" (the job count, left out until the first load) in 13px semibold `foreground`, filling the row, then a Refresh button on the right.
2. **Cards**, one per job in the order `jobs.py list` gives, 10px apart, full width. Each card has 12px padding, 6px between lines, 8px corners, a `surface` background, and a 1px `border`, which turns `blocked` when the card's state is blocked. Its lines are:
   1. An 8px round state dot (or a 12px spinner while running), the job's name in 14px semibold `foreground` (truncated), and the state word in 12px on the right.
   2. The schedule and next run in 12px `muted`: "weekdays at 5am · Next 5:00am (in 3h 12m)". A paused job shows "Paused" in place of the next run, and a job with no next run shows "No next run".
   3. The last run in 12px, `muted` when it succeeded and `blocked` otherwise: "Succeeded 2h ago · 4m 31s · $0.42", "Failed 5m ago · 12s", or "Never run". The duration and cost appear only when the run recorded them. On the right, one 8px dot per recent run (up to ten, skipped runs left out): `done` for a success and `blocked` for anything else, 3px apart.
   4. Only when the last run has an error: the error text in 11px `dim`, truncated to one line.
   5. A wrapping row of buttons, 6px apart, 4px above: **Run now** (primary), **Pause** or **Resume**, **Log**, and **Open output** (only when the job has an output file or an open action).

Buttons are 12px text with 10px by 4px padding, 6px corners, and a 1px border. Plain buttons use `surface` with a `border` edge and `foreground` text. The primary button uses `solid` (or `accent`) with an `accent` edge and `solid_foreground` (or `accent_foreground`) text. Hovering a plain button turns it `hover`, and hovering the primary one turns it `accent`. A disabled button has `dim` text and no hover.

Instead of cards, the body can show one 12px line: "Loading…" in `dim` before the first load, "No jobs yet. Ask Claude Code to schedule one; see the scheduling-agent-jobs skill." in `dim`, or "Couldn't read jobs: <error>" in `blocked`.

## How it behaves

Each card has one state, shown by its dot and state word in that state's theme color:

| State | When | Color |
|---|---|---|
| Running | a run holds the job's lock | `working`, with the spinner turning once every 900ms in place of the dot |
| Paused | the job is paused | `idle` |
| Not loaded | the job should be scheduled but launchd doesn't have it | `blocked` |
| Last run failed | the last run that wasn't skipped didn't succeed | `blocked` |
| Scheduled | otherwise | `done` |

- **Run now** changes to "Starting…", runs `jobs.py run <slug>`, and shows an info toast for 4 seconds: "<label> started", "Click Log on its card to follow it." It's disabled while the job is running or another button on the card is busy.
- **Pause** and **Resume** change to "Pausing…" and "Resuming…" and run `jobs.py pause <slug>` or `jobs.py resume <slug>`. Every button that runs `jobs.py` is disabled on that card until it finishes. A failure shows an error toast, "Couldn't <verb> <label>", with the command's error output.
- **Log** opens a terminal split to the right, named "<label> log", running `tail -n 400 -f` on the job's log.
- **Open output** does what a click on the job's success notification does: it runs the job's `open` command list as is, opens a URL with `/usr/bin/open`, or opens a file with `/usr/bin/open -t`. With no `open`, it opens the output file. If Herdr can't start the command, an error toast says so. A command that starts and then exits nonzero shows nothing.
- **Refresh** reloads the list.
- The tab reloads whenever anything under `~/.config/scheduled-jobs/` changes, and once a minute, which also keeps the relative times current.

## Built from

- `mod.json`: `{ "place": "none", "title": "Schedules" }`. The person opens it as a tab with `herdr-app do tab.new --name Schedules --mod schedules`. It takes no props.
- `main.js`: one `View` class. Imports `View`, `div`, and `svg` from `gpui-kit`, `v_flex` and `h_flex` from `gpui-base`, and `exec`, `home`, `run`, `theme`, `notify`, `watchFolder`, `checking`, and `readTextFile` from `herdr`.
- Data: it reads the path of `jobs.py` from `~/.config/scheduled-jobs/jobs-path`, then runs `/usr/bin/python3 <jobs.py> list --json` (20-second timeout) and uses the `jobs` array. Each entry has `slug`, `name`, `label`, `scheduleText`, `paused`, `loaded`, `running`, `nextRunAt`, `lastRun` (`status`, `startedAt`, `finishedAt`, `durationMs`, `costUsd`, and `error`), `recentRuns`, `logPath`, `expectedFile`, and `open`.
- Actions: `/usr/bin/python3 <jobs.py> run|pause|resume <slug>` (30-second timeout), the job's open command, and the `terminal.open` command (`argv`, `place: "right"`, and `name`) for the log.
- Updates: a `cx.spawn` loop on ``watchFolder(`${home()}/.config/scheduled-jobs`)``, plus `cx.timer.every(60000, …)`.
- Art: the shipped `workspace-chrome` mod's `clock.svg` and `spinner.svg`. Nothing of its own.

## Watch out

- The mod never touches launchd or the job files. The job list and the Run now, Pause, and Resume actions go through `jobs.py`, so the tab agrees with the command line and with the skill. Log and Open output run directly in Herdr (`terminal.open` and `exec`).
- It runs programs (`/usr/bin/python3` and each job's open command) and reads `~/.config/scheduled-jobs/`, outside its own folder. Tell the person before installing it.
- During `herdr-app check`, `init` returns before loading anything (`if (checking()) return;`), so the check doesn't run `jobs.py`.
- `jobs-path` exists only once `jobs.py relink`, `add`, `run`, `smoke-test`, or a job run has written it. `relink` writes it even with no jobs, and `install.sh` runs `relink`. `list` doesn't write it. Until it exists, the tab shows "Couldn't read jobs: jobs.py hasn't run on this Mac yet…".
- If `watchFolder` throws, the loop ends and the one-minute timer carries on alone.
- Times are local, in a 12-hour clock ("5:00am"). The relative times round to minutes, hours, and days ("in 3h 12m", "2h ago").
- Theme tokens the theme may lack fall back: `canvas` to `background`, `solid` to `accent`, `solid_foreground` to `accent_foreground`, and a missing state color to `dim` or `muted`.
- The spinner and clock come from `workspace-chrome`, so a setup without that shipped mod shows no icons there.

## Done when

- `herdr-app check mods/schedules` passes.
- With no jobs, the tab shows a message that starts with "No jobs yet".
- Installing a paused job with `jobs.py add` makes its card appear within a few seconds, without Refresh, showing "Paused" with an `idle` dot and a Resume button.
- Run now shows the "started" toast, and the card switches to Running with a spinner until the run finishes. Then the last-run line and a new dot show the result.
- Log opens a split on the right that follows the job's log.
- Pause on a scheduled job leaves `jobs.py list` showing it paused, and the card says so.
