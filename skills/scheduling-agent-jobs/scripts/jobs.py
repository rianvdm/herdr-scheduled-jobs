#!/usr/bin/python3
"""Scheduled agent jobs: launchd starts them, Claude Code runs them.

  jobs.py list [--json]       every job: schedule, next run, last result
  jobs.py add <spec.json>     install a job, or replace one with the same slug
  jobs.py pause <slug>        stop scheduling it (the job file stays)
  jobs.py resume <slug>
  jobs.py run <slug>          start it now, under launchd when it's loaded
  jobs.py remove <slug>       uninstall it (its run history and log stay)
  jobs.py relink              re-point every job's plist at this copy of the script
  jobs.py smoke-test          check that unattended runs work on this Mac
  jobs.py exec <slug>         what launchd calls: run the job once

State lives in ~/.config/scheduled-jobs/: jobs/<slug>.json (the spec),
runs/<slug>.jsonl (one line per run), logs/<slug>.log, locks/, and
jobs-path (where this script is, for the Herdr panel). Each job's
plist is ~/Library/LaunchAgents/local.scheduled-jobs.<slug>.plist.

Written for the macOS system Python (3.9), standard library only.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

HOME = os.path.expanduser("~")
ROOT = os.environ.get("SCHEDULED_JOBS_HOME", os.path.join(HOME, ".config/scheduled-jobs"))
LAUNCH_AGENTS = os.environ.get("SCHEDULED_JOBS_LAUNCH_AGENTS", os.path.join(HOME, "Library/LaunchAgents"))
NO_LAUNCHD = os.environ.get("SCHEDULED_JOBS_NO_LAUNCHD") == "1"  # tests
PYTHON = "/usr/bin/python3"
SELF = os.path.realpath(__file__)
LABEL_PREFIX = "local.scheduled-jobs."
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
KILL_GRACE_SECONDS = 10
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def die(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


# ---------- paths and files ----------

def path(kind, slug):
    return {
        "job": os.path.join(ROOT, "jobs", f"{slug}.json"),
        "runs": os.path.join(ROOT, "runs", f"{slug}.jsonl"),
        "log": os.path.join(ROOT, "logs", f"{slug}.log"),
        "lock": os.path.join(ROOT, "locks", f"{slug}.lock"),
        "manual": os.path.join(ROOT, "locks", f"{slug}.manual"),
        "plist": os.path.join(LAUNCH_AGENTS, f"{LABEL_PREFIX}{slug}.plist"),
    }[kind]


def ensure_dirs():
    for d in ("jobs", "runs", "logs", "locks"):
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    # The Herdr panel reads this to find the script, wherever the repo is cloned.
    pointer = os.path.join(ROOT, "jobs-path")
    try:
        with open(pointer) as f:
            if f.read().strip() == SELF:
                return
    except OSError:
        pass
    with open(pointer, "w") as f:
        f.write(SELF + "\n")


def find_claude():
    """Claude Code's CLI. launchd's PATH is minimal, so look where installers put it."""
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    for p in (os.path.join(HOME, ".local/bin/claude"), os.path.join(HOME, ".claude/local/claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if os.access(p, os.X_OK):
            return p
    return shutil.which("claude")


def write_json(p, data):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def load_job(slug):
    p = path("job", slug)
    if not os.path.exists(p):
        die(f'no job "{slug}" (looked for {p})')
    with open(p) as f:
        return json.load(f)


def all_jobs():
    d = os.path.join(ROOT, "jobs")
    if not os.path.isdir(d):
        return []
    jobs = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            with open(os.path.join(d, name)) as f:
                jobs.append(json.load(f))
    return jobs


def runs(slug):
    p = path("runs", slug)
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def mtime(p):
    try:
        return os.stat(p).st_mtime
    except OSError:
        return 0.0


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------- cron ----------

class Field:
    def __init__(self, values, full, star):
        self.values = values
        self.restricted = values != full  # narrower than the whole range
        self.star = star  # written as * or */n, which matters for cron's day-or-weekday rule


CRON_FIELDS = [("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 7)]


def parse_field(text, lo, hi, name):
    values = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise ValueError(f"bad step in {name} field: {text}")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            if not (a_s.isdigit() and b_s.isdigit()):
                raise ValueError(f"bad range in {name} field: {text} (numbers only, no names)")
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = int(part)
            b = hi if step > 1 else a
        else:
            raise ValueError(f"bad {name} field: {text} (numbers only, no names)")
        if a < lo or b > hi or a > b:
            raise ValueError(f"{name} field out of range {lo}-{hi}: {text}")
        values.update(range(a, b + 1, step))
    if name == "dow" and 7 in values:
        values.discard(7)
        values.add(0)
        full = list(range(0, 7))
    else:
        full = list(range(lo, hi + 1)) if name != "dow" else list(range(0, 7))
    return Field(sorted(values), full, text.startswith("*"))


def parse_cron(expr):
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f'cron needs 5 fields (minute hour day month weekday), got "{expr}"')
    return {name: parse_field(p, lo, hi, name) for p, (name, lo, hi) in zip(parts, CRON_FIELDS)}


def day_matches(c, d):
    if d.month not in c["month"].values:
        return False
    dom_ok = d.day in c["dom"].values
    dow_ok = (d.isoweekday() % 7) in c["dow"].values
    if not c["dom"].star and not c["dow"].star:
        return dom_ok or dow_ok  # cron's rule: both written out means either one
    return dom_ok and dow_ok


def next_run(expr, after=None):
    """The first scheduled minute strictly after `after` (local, naive)."""
    c = parse_cron(expr)
    after = after or dt.datetime.now()
    start = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for offset in range(0, 366 * 5):
        d = start.date() + dt.timedelta(days=offset)
        if not day_matches(c, d):
            continue
        for h in c["hour"].values:
            for m in c["minute"].values:
                t = dt.datetime(d.year, d.month, d.day, h, m)
                if t >= start:
                    return t
    return None


def launchd_calendars(expr):
    c = parse_cron(expr)
    base = [{}]
    for key, name in (("Minute", "minute"), ("Hour", "hour"), ("Month", "month")):
        if c[name].restricted:
            base = [dict(b, **{key: v}) for b in base for v in c[name].values]
    out = []
    dom, dow = c["dom"], c["dow"]
    if dom.restricted and dow.restricted and (dom.star or dow.star):
        raise ValueError("launchd can't require both a day of the month and a weekday; use one or the other")
    # launchd starts a job when any one dict matches, so cron's day-OR-weekday
    # rule becomes two separate sets of dicts.
    if dom.restricted:
        out += [dict(b, Day=v) for b in base for v in dom.values]
    if dow.restricted:
        out += [dict(b, Weekday=v) for b in base for v in dow.values]
    if not dom.restricted and not dow.restricted:
        out = base
    if len(out) > 500:
        raise ValueError(f"schedule expands to {len(out)} launchd entries; simplify it")
    return out


def describe(expr):
    """A short plain-words version of common schedules; the raw cron otherwise."""
    try:
        c = parse_cron(expr)
    except ValueError:
        return expr
    if c["month"].restricted or c["dom"].restricted or c["minute"].values == list(range(60)):
        return expr
    if len(c["hour"].values) > 4 or not c["hour"].restricted or len(c["minute"].values) > 2:
        return expr
    times = ", ".join(
        dt.time(h, m).strftime("%-I:%M%p").lower().replace(":00", "")
        for h in c["hour"].values for m in c["minute"].values)
    days = "daily"
    if c["dow"].restricted:
        names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        v = c["dow"].values
        days = "weekdays" if v == [1, 2, 3, 4, 5] else ", ".join(names[i] for i in v)
    return f"{days} at {times}"


# ---------- launchd ----------

def domain():
    return f"gui/{os.getuid()}"


def launchctl(*args):
    if NO_LAUNCHD:
        return subprocess.CompletedProcess(args, 113, "", "launchd disabled for tests")
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, timeout=15)


def is_loaded(slug):
    return launchctl("print", f"{domain()}/{LABEL_PREFIX}{slug}").returncode == 0


def write_plist(job):
    slug = job["slug"]
    cals = launchd_calendars(job["schedule"])
    plist = {
        "Label": LABEL_PREFIX + slug,
        "ProgramArguments": [PYTHON, SELF, "exec", slug],
        "WorkingDirectory": job["workdir"],
        "EnvironmentVariables": {"PATH": PATH},
        "StartCalendarInterval": cals[0] if len(cals) == 1 else cals,
        "StandardOutPath": path("log", slug),
        "StandardErrorPath": path("log", slug),
        "RunAtLoad": False,
    }
    os.makedirs(LAUNCH_AGENTS, exist_ok=True)
    with open(path("plist", slug), "wb") as f:
        plistlib.dump(plist, f)


def remove_plist(slug):
    try:
        os.remove(path("plist", slug))
    except FileNotFoundError:
        pass


def bootstrap(slug):
    r = launchctl("bootstrap", domain(), path("plist", slug))
    if r.returncode != 0 and not NO_LAUNCHD:
        die(f"launchctl bootstrap failed ({r.returncode}): {r.stderr.strip()}")


def bootout(slug):
    if is_loaded(slug):
        r = launchctl("bootout", f"{domain()}/{LABEL_PREFIX}{slug}")
        if r.returncode != 0:
            die(f"launchctl bootout failed ({r.returncode}): {r.stderr.strip()}")


# ---------- job specs ----------

REQUIRED = {"name": str, "slug": str, "schedule": str, "workdir": str, "prompt": str, "timeoutSeconds": int}
OPTIONAL = {"label": str, "expectedFile": str, "open": (str, list), "countRegex": str, "countNoun": str,
            "model": str, "paused": bool, "claudePath": str, "createdAt": str}


def type_name(t):
    return " or ".join(x.__name__ for x in t) if isinstance(t, tuple) else t.__name__


def expand(value):
    return os.path.expanduser(value) if isinstance(value, str) and value.startswith("~") else value


PROTECTED = ("Documents", "Desktop", "Downloads", "Library/Mobile Documents")


def protected_folder(p):
    """The macOS-protected folder that holds p, or None. Launchd jobs can't read these."""
    for name in PROTECTED:
        folder = os.path.join(HOME, name)
        for base in {folder, os.path.realpath(folder)}:
            for candidate in {p, os.path.realpath(p)}:
                if (candidate + "/").startswith(base + "/"):
                    return folder
    return None


def validate(spec):
    for key, typ in REQUIRED.items():
        if not isinstance(spec.get(key), typ):
            die(f'"{key}" is required and must be a {typ.__name__}')
    for key, value in spec.items():
        if key in OPTIONAL and not isinstance(value, OPTIONAL[key]):
            die(f'"{key}" must be a {type_name(OPTIONAL[key])}')
        if key not in REQUIRED and key not in OPTIONAL and key != "updatedAt":
            die(f'unknown field "{key}"')
    for key in ("workdir", "expectedFile", "open"):
        spec[key] = expand(spec.get(key)) if key in spec else None
        if spec[key] is None:
            del spec[key]
    if isinstance(spec.get("open"), list) and not all(isinstance(a, str) for a in spec["open"]):
        die('"open" must be a path, a URL, or a command as a list of strings')
    if not SLUG_RE.match(spec["slug"]):
        die("slug must be lowercase letters, digits and dashes")
    if not os.path.isdir(spec["workdir"]):
        die(f'workdir does not exist: {spec["workdir"]}')
    for what, p in (("jobs.py is", SELF), ("workdir is", spec["workdir"])):
        folder = protected_folder(p)
        if folder:
            die(f"{what} in {folder}, which macOS doesn't let scheduled jobs read; "
                "move it somewhere else (for example ~/git)")
    if spec["timeoutSeconds"] <= 0:
        die("timeoutSeconds must be positive")
    try:
        launchd_calendars(spec["schedule"])
    except ValueError as e:
        die(str(e))
    if "countRegex" in spec:
        re.compile(spec["countRegex"])


def summary(job):
    slug = job["slug"]
    history = runs(slug)
    last = next((r for r in reversed(history) if r.get("status") != "skipped"), None)
    paused = bool(job.get("paused"))
    nxt = None if paused else next_run(job["schedule"])
    return {
        "slug": slug,
        "name": job["name"],
        "label": job.get("label", job["name"]),
        "schedule": job["schedule"],
        "scheduleText": describe(job["schedule"]),
        "paused": paused,
        "loaded": is_loaded(slug),
        "running": is_running(slug),
        "nextRunAt": nxt.astimezone().isoformat(timespec="seconds") if nxt else None,
        "lastRun": last,
        "recentRuns": history[-10:],
        "logPath": path("log", slug),
        "expectedFile": job.get("expectedFile"),
        "open": job.get("open"),
        "workdir": job["workdir"],
        "jobPath": path("job", slug),
    }


def is_running(slug):
    p = path("lock", slug)
    if not os.path.exists(p):
        return False
    with open(p, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


# ---------- commands ----------

def cmd_list(args):
    rows = [summary(j) for j in all_jobs()]
    if args.json:
        print(json.dumps({"root": ROOT, "jobs": rows}, indent=2))
        return
    if not rows:
        print(f"No jobs in {ROOT}/jobs.")
        return
    for r in rows:
        state = "running" if r["running"] else "paused" if r["paused"] else "scheduled" if r["loaded"] else "NOT LOADED"
        last = r["lastRun"]
        last_text = f'{last["status"]} {last["startedAt"]}' if last else "never run"
        print(f'{r["slug"]}  [{state}]  {r["scheduleText"]} ({r["schedule"]})')
        print(f'  next: {r["nextRunAt"] or "-"}   last: {last_text}')
        print(f'  log: {r["logPath"]}')


def cmd_add(args):
    with open(args.spec) as f:
        spec = json.load(f)
    r = install(spec)
    print(f'{"Paused" if spec["paused"] else "Scheduled"} {spec["slug"]}: {r["scheduleText"]}; next run {r["nextRunAt"] or "-"}')
    print(f'log: {r["logPath"]}')


def relink_one(path_to_job):
    """Relink one job file. Returns (line, ok); never raises or exits."""
    stem = os.path.basename(path_to_job)[:-len(".json")]
    slug = stem
    err = io.StringIO()
    try:
        with open(path_to_job) as f:
            job = json.load(f)
        slug = job["slug"]
        # A run could start between this check and install()'s bootout; the window is tiny.
        if is_running(slug):  # install() boots the job out, which would kill the run
            return f"skipped {slug}: running now, re-run relink after it finishes", True
        old = "no plist"
        try:
            with open(path("plist", slug), "rb") as f:
                old = plistlib.load(f)["ProgramArguments"][1]
        except (OSError, plistlib.InvalidFileException, KeyError, IndexError, ValueError):
            pass
        with contextlib.redirect_stderr(err):  # die() reports on stderr, then exits
            install(job)
        state = "paused" if job.get("paused") else "scheduled"
        return f"relinked {slug} ({state}): {old} -> {SELF}", True
    except (SystemExit, Exception) as e:
        msg = err.getvalue().strip().replace("error: ", "", 1) or str(e) or type(e).__name__
        return f"failed {slug}: {msg}", False


def cmd_relink(args):
    ensure_dirs()
    d = os.path.join(ROOT, "jobs")
    files = sorted(os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json"))
    if not files:
        print("No jobs to relink.")
        return 0
    failed = 0
    for p in files:
        line, ok = relink_one(p)
        print(line)
        failed += not ok
    return 1 if failed else 0


def install(spec):
    validate(spec)
    ensure_dirs()
    slug = spec["slug"]
    old = path("job", slug)
    if os.path.exists(old):
        with open(old) as f:
            spec.setdefault("createdAt", json.load(f).get("createdAt"))
    spec.setdefault("createdAt", now_iso())
    spec["updatedAt"] = now_iso()
    spec.setdefault("paused", False)
    if not spec.get("claudePath"):
        found = find_claude()
        if not found:
            die("can't find the claude CLI; install Claude Code or set claudePath in the spec")
        spec["claudePath"] = found
    write_json(old, spec)
    bootout(slug)
    if spec["paused"]:
        remove_plist(slug)  # launchd loads every plist here at login, paused or not
    else:
        write_plist(spec)
        bootstrap(slug)
    return summary(spec)


def set_paused(slug, paused):
    job = load_job(slug)
    job["paused"] = paused
    job["updatedAt"] = now_iso()
    write_json(path("job", slug), job)
    if paused:
        bootout(slug)
        remove_plist(slug)
        print(f"Paused {slug}.")
    else:
        write_plist(job)
        if not is_loaded(slug):
            bootstrap(slug)
        print(f'Resumed {slug}; next run {summary(job)["nextRunAt"]}.')


def cmd_run(args):
    start_run(args.slug)
    print(f'Started {args.slug}. Log: {path("log", args.slug)}')


def start_run(slug):
    job = load_job(slug)
    ensure_dirs()
    if is_running(slug):
        die(f"{slug} is already running")
    open(path("manual", slug), "w").close()
    if is_loaded(slug):
        r = launchctl("kickstart", f"{domain()}/{LABEL_PREFIX}{slug}")
        if r.returncode != 0:
            die(f"launchctl kickstart failed ({r.returncode}): {r.stderr.strip()}")
    else:
        log = open(path("log", slug), "a")
        subprocess.Popen([PYTHON, SELF, "exec", slug], cwd=job["workdir"], stdin=subprocess.DEVNULL,
                         stdout=log, stderr=log, start_new_session=True)


# The smoke test has to finish inside an agent's 10-minute Bash call, so the wait is 8 minutes.
# It outlasts the job's timeout plus the kill grace, so a run that times out still reports.
SMOKE_TIMEOUT_SECONDS = 420
SMOKE_WAIT_SECONDS = 480

# {claude} is the CLI's absolute path: jobs run with a minimal PATH that may not include it.
SMOKE_PROMPT = """This is an unattended smoke test of scheduled Claude Code runs, so nobody can answer questions or approve anything. Do only these read-only steps:
1. For each of gh and glab that is installed, run its auth check (`gh auth status`, `glab auth status`) and note the result. Skip any that isn't installed.
2. Run `{claude} mcp list` and note each MCP server it lists, by name, and whether it is connected. If it lists none, say so.
3. Overwrite {out} with a short Markdown table of the results.
Do not change any other file, post anything, or open any app."""


def smoke_prompt(out, claude):
    return SMOKE_PROMPT.format(out=out, claude=shlex.quote(claude))


def cmd_smoke_test(args):
    """Installs a throwaway job, runs it under launchd, reports, and removes it."""
    out = os.path.join(ROOT, "smoke-test.md")
    workdir = os.path.abspath(args.workdir or os.getcwd())
    slug = "smoke-test"
    if os.path.exists(path("job", slug)):
        die("a smoke-test job already exists; remove it first with: jobs.py remove smoke-test")
    claude = find_claude()
    if not claude:
        die("can't find the claude CLI; install Claude Code first")
    install({"name": "Smoke test", "slug": slug, "label": "Scheduler smoke test", "schedule": "0 3 1 1 *",
             "workdir": workdir, "timeoutSeconds": SMOKE_TIMEOUT_SECONDS, "expectedFile": out, "open": out,
             "claudePath": claude, "prompt": smoke_prompt(out, claude)})
    before = len(runs(slug))
    try:
        start_run(slug)
        print(f"Running a read-only smoke test under launchd from {workdir} (usually under a minute)…")
        print(f'Follow along: tail -f {path("log", slug)}')
        deadline = time.monotonic() + SMOKE_WAIT_SECONDS
        while len(runs(slug)) == before and time.monotonic() < deadline:
            time.sleep(2)
        new = runs(slug)[before:]
        if not new:
            print(f"No result after {SMOKE_WAIT_SECONDS // 60} minutes. Check the log above.")
            return 1
        r = new[-1]
        print(f'Result: {r["status"]}' + (f' ({r["error"]})' if r.get("error") else "")
              + (f' in {r["durationMs"] // 1000}s' if r.get("durationMs") else ""))
        if r["status"] == "success":
            with open(out) as f:
                print("\n" + f.read().strip())
        else:
            print(f'The log has the details: {path("log", slug)}')
        return 0 if r["status"] == "success" else 1
    finally:
        bootout(slug)
        for kind in ("plist", "job"):
            try:
                os.remove(path(kind, slug))
            except FileNotFoundError:
                pass


def cmd_remove(args):
    load_job(args.slug)
    bootout(args.slug)
    remove_plist(args.slug)
    try:
        os.remove(path("job", args.slug))
    except FileNotFoundError:
        pass
    print(f'Removed {args.slug}. Its runs and log stay in {ROOT}.')


# ---------- running a job ----------

def short(value, n=160):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def tool_summary(block):
    inp = block.get("input") or {}
    for key in ("command", "file_path", "pattern", "query", "url", "description"):
        if isinstance(inp.get(key), str):
            return f'→ {block.get("name")}: {short(inp[key])}'
    return f'→ {block.get("name")} {short(inp)}' if inp else f'→ {block.get("name")}'


class Transcript:
    """Turns Claude Code's stream-json into a readable log and keeps the facts we need."""

    def __init__(self, log):
        self.log = log
        self.session_id = None
        self.result = None
        self.last_text = None

    def write(self, text):
        self.log.write(text.rstrip("\n") + "\n")
        self.log.flush()

    def feed(self, line):
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip():
                self.write(line)
            return
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            self.session_id = ev.get("session_id")
            self.write(f'session {self.session_id} · model {ev.get("model")}')
            for s in ev.get("mcp_servers") or []:
                if s.get("status") != "connected":
                    self.write(f'MCP server {s.get("name")}: {s.get("status")}')
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.last_text = block["text"].strip()
                    self.write(block["text"])
                elif block.get("type") == "tool_use":
                    self.write(tool_summary(block))
        elif kind == "user":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_result" and block.get("is_error"):
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
                    self.write(f"  ✗ {short(body or '', 400)}")
        elif kind == "result":
            self.result = ev
            self.session_id = ev.get("session_id") or self.session_id
            cost = ev.get("total_cost_usd")
            self.write(f'----- result: {ev.get("subtype")} · {ev.get("num_turns")} turns'
                       + (f" · ${cost:.2f}" if isinstance(cost, (int, float)) else "") + " -----")
            if isinstance(ev.get("result"), str) and ev["result"].strip() != self.last_text:
                self.write(ev["result"])


def open_action(target):
    """What clicking Open runs: a command list as is, a URL or a file with `open`."""
    if isinstance(target, list):
        return target
    if "://" in target:
        return ["/usr/bin/open", target]
    return ["/usr/bin/open", "-t", target]


def notifier():
    return os.environ.get("NOTIFIER_BIN") or shutil.which("terminal-notifier", path=PATH)


def notify(title, message, group, action, log=None):
    if not notifier():
        if log:
            log.write("No notification: terminal-notifier isn't installed (brew install terminal-notifier).")
        return
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


def record(slug, entry):
    with open(path("runs", slug), "a") as f:
        f.write(json.dumps(entry) + "\n")


def child_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    env["PATH"] = PATH
    return env


class ClaudeRunner:
    """Everything Claude-specific about an unattended run, so another agent can be added beside it."""

    def find_binary(self, job):
        return os.environ.get("CLAUDE_BIN") or job.get("claudePath") or find_claude()

    def argv(self, job, binary):
        argv = [binary, "-p", job["prompt"], "--permission-mode", "bypassPermissions",
                "--output-format", "stream-json", "--verbose", "--name", f'Scheduled: {job["name"]}']
        if job.get("model"):
            argv += ["--model", job["model"]]
        return argv

    def transcript(self, log):
        return Transcript(log)

    def resume_hint(self, workdir, session_id):
        return f'To look into this run: cd {shlex.quote(workdir)} && claude --resume {session_id}'


RUNNERS = {"claude": ClaudeRunner()}


def cmd_exec(args):
    job = load_job(args.slug)
    slug = job["slug"]
    ensure_dirs()
    log = open(path("log", slug), "a")
    runner = RUNNERS["claude"]
    tx = runner.transcript(log)
    lock = open(path("lock", slug), "a")
    run_id = uuid.uuid4().hex[:12]
    started_at, t0 = now_iso(), time.monotonic()
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        tx.write(f"##### Skipped {started_at}: the previous run is still going #####")
        record(slug, {"runId": run_id, "slug": slug, "status": "skipped", "startedAt": started_at,
                      "finishedAt": started_at, "error": "already running"})
        return 0
    source = "manual" if getattr(args, "manual", False) else "scheduled"
    if os.path.exists(path("manual", slug)):
        source = "manual"
        os.remove(path("manual", slug))
    if job.get("paused") and source == "scheduled":
        tx.write(f"##### Skipped {started_at}: the job is paused #####")
        record(slug, {"runId": run_id, "slug": slug, "status": "skipped", "startedAt": started_at,
                      "finishedAt": started_at, "error": "paused"})
        return 0

    expected = job.get("expectedFile")
    before = mtime(expected) if expected else 0.0
    binary = runner.find_binary(job) or "claude"
    argv = runner.argv(job, binary)
    tx.write(f"\n##### Started {started_at} · {source} · run {run_id} #####")

    spawn_error = None
    try:
        proc = subprocess.Popen(argv, cwd=job["workdir"], env=child_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=log, text=True, start_new_session=True)
    except OSError as e:
        tx.write(f"could not start {binary}: {e}")
        spawn_error = f"couldn't start the agent ({os.path.basename(binary)})"
        proc = None

    ended = {"why": None}

    def stop(why):
        if ended["why"] or proc is None or proc.poll() is not None:
            return
        ended["why"] = why
        tx.write(f"##### {why}: sending SIGTERM #####")
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        def hard_kill():
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        killer = threading.Timer(KILL_GRACE_SECONDS, hard_kill)
        killer.daemon = True
        killer.start()

    timer = threading.Timer(job["timeoutSeconds"], stop, args=(f'Timed out after {job["timeoutSeconds"]}s',))
    timer.daemon = True
    if proc is not None:
        signal.signal(signal.SIGTERM, lambda *_: stop("Stopped"))
        timer.start()
        for line in proc.stdout:
            tx.feed(line)
        code = proc.wait()
        timer.cancel()
    else:
        code = 127

    updated = (not expected) or mtime(expected) > before
    agent_error = bool(tx.result and tx.result.get("is_error"))
    # A prompt can ask the agent to end with "PARTIAL: <what failed>" when a step after the
    # output file was written failed, so the run counts as failed even though the file is fresh.
    final_text = (tx.result or {}).get("result") or ""
    partial = next((l.strip()[len("PARTIAL:"):].strip() for l in final_text.splitlines()
                    if l.strip().startswith("PARTIAL:")), None)
    if ended["why"]:
        status = "timeout" if ended["why"].startswith("Timed out") else "stopped"
        code = 124
        error = ended["why"]
    elif code != 0 or not updated or agent_error or partial is not None:
        status = "failed"
        reasons = ([spawn_error] if proc is None else [f"exit code {code}"]) if code != 0 else []
        if agent_error:
            reasons.append(f'agent error: {tx.result.get("subtype")}')
        if not updated:
            reasons.append(f"{os.path.basename(expected)} not updated")
        if partial is not None:
            reasons.append(f"partly done: {partial or 'see the log'}")
        error = ", ".join(reasons)
        code = code or 1
    else:
        status, error = "success", None

    finished_at = now_iso()
    duration_ms = int((time.monotonic() - t0) * 1000)
    record(slug, {"runId": run_id, "slug": slug, "source": source, "status": status, "exitCode": code,
                  "error": error, "startedAt": started_at, "finishedAt": finished_at,
                  "durationMs": duration_ms, "sessionId": tx.session_id,
                  "costUsd": (tx.result or {}).get("total_cost_usd")})
    tx.write(f"##### Finished {finished_at} · {status} · exit {code} · {duration_ms // 1000}s"
             + (f" · {error}" if error else "") + " #####")
    if tx.session_id:
        tx.write(runner.resume_hint(job["workdir"], tx.session_id))

    outcome = run_outcome(job, status, error, expected)
    deliver(job, outcome, None, tx)
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("add"); s.add_argument("spec"); s.set_defaults(fn=cmd_add)
    s = sub.add_parser("pause"); s.add_argument("slug"); s.set_defaults(fn=lambda a: set_paused(a.slug, True))
    s = sub.add_parser("resume"); s.add_argument("slug"); s.set_defaults(fn=lambda a: set_paused(a.slug, False))
    s = sub.add_parser("run"); s.add_argument("slug"); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("remove"); s.add_argument("slug"); s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("relink"); s.set_defaults(fn=cmd_relink)
    s = sub.add_parser("smoke-test"); s.add_argument("--workdir"); s.set_defaults(fn=cmd_smoke_test)
    s = sub.add_parser("exec"); s.add_argument("slug"); s.add_argument("--manual", action="store_true", help="run even if paused (tests)"); s.set_defaults(fn=cmd_exec)
    args = p.parse_args()
    sys.exit(args.fn(args) or 0)


if __name__ == "__main__":
    main()
