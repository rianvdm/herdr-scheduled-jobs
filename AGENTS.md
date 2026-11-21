# herdr-scheduled-jobs

This repo holds a macOS launchd scheduler for unattended Claude Code runs (`jobs.py`), the Claude Code skill that drives it (`skills/scheduling-agent-jobs/`), and a Herdr terminal-app mod (`mods/schedules/`) that shows a "Schedules" tab with one card per job. The mod reads and acts only through `jobs.py`, and never touches launchd directly.

## Before every commit

- Run `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_jobs.py`, `/usr/bin/python3 -B skills/scheduling-agent-jobs/tests/test_notify_postmark.py`, and `/usr/bin/python3 -B tests/test_install.py`. Delete any `__pycache__` they leave.
- Grep for personal references (home paths, usernames, and private project names) and remove them.

## Rules

- Keep `RECIPE.md` in step with `mods/schedules/`.
- Organization-specific content (internal proxies, portals, token lifetimes, internal package taps, and internal URLs) goes only inside internal blocks (an `<!-- internal:start -->` line through an `<!-- internal:end -->` line). None goes in public code, the installer, or the mod.
- `jobs.py` and its tests use the stdlib only and run on `/usr/bin/python3`. The installer scripts are bash using only macOS built-ins.
- Tests that touch jobs set `SCHEDULED_JOBS_NO_LAUNCHD=1` plus a temporary `HOME` or `SCHEDULED_JOBS_HOME`. No test may load anything into the real launchd domain.
- Use the Oxford comma in everything you write. Commits carry no attribution trailers.

## After editing the mod

Run `herdr-app check mods/schedules`.
