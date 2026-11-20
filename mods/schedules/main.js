// Schedules: the launchd jobs that jobs.py manages (the scheduling-agent-jobs skill).
// One card per job: schedule, next run, last result, the last ten runs as dots, and
// Run now / Pause / Resume / Log / Open output. All reads and actions go through jobs.py,
// so this panel never touches launchd or the job files itself. jobs.py records where it
// lives in ~/.config/scheduled-jobs/jobs-path, so this works wherever the repo is cloned.
// Installed by install.sh; see README

import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import { exec, home, run, theme, notify, watchFolder, checking, readTextFile } from "herdr";

const STATE = () => `${home()}/.config/scheduled-jobs`;
const PYTHON = "/usr/bin/python3";

function ago(iso, now) {
  if (!iso) return "";
  const s = Math.round((now - Date.parse(iso)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function until(iso, now) {
  if (!iso) return "";
  const m = Math.max(0, Math.round((Date.parse(iso) - now) / 60000));
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `in ${h}h ${m % 60}m`;
  return `in ${Math.floor(h / 24)}d ${h % 24}h`;
}

function clock(iso) {
  if (!iso) return "";
  const d = new Date(Date.parse(iso));
  const h = d.getHours(), m = d.getMinutes();
  const hh = ((h + 11) % 12) + 1;
  return `${hh}:${String(m).padStart(2, "0")}${h < 12 ? "am" : "pm"}`;
}

function duration(ms) {
  if (ms == null) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

export default class Schedules extends View {
  init(_props, cx) {
    this.jobs = null;
    this.error = null;
    this.busy = {};
    this.now = Date.now();
    if (checking()) return;
    this.refresh(cx);
    // Job files, run history, and locks all live under STATE, so one watch covers
    // a run starting, finishing, or a job changing from the command line.
    cx.spawn(async (cx) => {
      for (;;) {
        try {
          await watchFolder(STATE());
        } catch (_e) {
          return; // the minute timer below still refreshes
        }
        await this.load();
        cx.notify();
      }
    });
    cx.timer.every(60000, (cx) => this.refresh(cx));
  }

  async jobsPath() {
    const p = (await readTextFile(`${STATE()}/jobs-path`))?.trim();
    if (!p) throw new Error("jobs.py hasn't run on this Mac yet. Run ./install.sh from the herdr-scheduled-jobs clone (or jobs.py relink) once.");
    return p;
  }

  async load() {
    try {
      const r = await exec(PYTHON, [await this.jobsPath(), "list", "--json"], { timeout: 20000 });
      if (r.code !== 0) throw new Error(r.stderr.trim() || `jobs.py exited ${r.code}`);
      this.jobs = JSON.parse(r.stdout).jobs;
      this.error = null;
    } catch (e) {
      this.error = String(e.message ?? e);
    }
    this.now = Date.now();
  }

  refresh(cx) {
    cx.spawn(async (cx) => {
      await this.load();
      cx.notify();
    });
  }

  act(cx, job, verb) {
    this.busy[job.slug] = verb;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const r = await exec(PYTHON, [await this.jobsPath(), verb, job.slug], { timeout: 30000 });
        if (r.code !== 0) throw new Error(r.stderr.trim() || `exit ${r.code}`);
        if (verb === "run") notify({ title: `${job.label} started`, body: "Click Log on its card to follow it.", level: "info", timeout: 4000 });
      } catch (e) {
        notify({ title: `Couldn't ${verb} ${job.label}`, body: String(e.message ?? e), level: "error" });
      }
      delete this.busy[job.slug];
      await this.load();
      cx.notify();
    });
  }

  showLog(job) {
    run("terminal.open", { argv: ["tail", "-n", "400", "-f", job.logPath], place: "right", name: `${job.label} log` });
  }

  // The same action a click on the notification runs (jobs.py open_action).
  openOutput(job) {
    const target = job.open || job.expectedFile;
    if (!target) return;
    const [cmd, ...args] = Array.isArray(target) ? target
      : target.includes("://") ? ["/usr/bin/open", target] : ["/usr/bin/open", "-t", target];
    exec(cmd, args).catch((e) => notify({ title: "Couldn't open the output", body: String(e.message ?? e), level: "error" }));
  }

  state(job) {
    if (job.running) return { text: "Running", color: "working" };
    if (job.paused) return { text: "Paused", color: "idle" };
    if (!job.loaded) return { text: "Not loaded", color: "blocked" };
    const s = job.lastRun?.status;
    if (s && s !== "success") return { text: "Last run failed", color: "blocked" };
    return { text: "Scheduled", color: "done" };
  }

  dot(t, status) {
    const color = status === "success" ? t.done : status === "skipped" ? t.dim : t.blocked;
    return div().size(8).flex_none().rounded(4).bg(color ?? t.dim);
  }

  button(t, id, label, onClick, { primary = false, disabled = false } = {}) {
    return div()
      .id(id)
      .px(10)
      .py(4)
      .rounded(6)
      .text_size(12)
      .border(1)
      .border_color(primary ? t.accent : t.border)
      .bg(primary ? t.solid ?? t.accent : t.surface)
      .text_color(disabled ? t.dim : primary ? t.solid_foreground ?? t.accent_foreground : t.foreground)
      .when(!disabled, (el) => el.cursor_pointer().hover((s) => s.bg(primary ? t.accent : t.hover)).on_click((_e, cx) => onClick(cx)))
      .child(label);
  }

  card(t, job) {
    const st = this.state(job);
    const busy = this.busy[job.slug];
    const last = job.lastRun;
    const lastText = last
      ? `${last.status === "success" ? "Succeeded" : last.status[0].toUpperCase() + last.status.slice(1)} ${ago(last.finishedAt || last.startedAt, this.now)}`
        + (last.durationMs != null ? ` · ${duration(last.durationMs)}` : "")
        + (typeof last.costUsd === "number" ? ` · $${last.costUsd.toFixed(2)}` : "")
      : "Never run";
    const next = job.paused ? "Paused" : job.nextRunAt ? `Next ${clock(job.nextRunAt)} (${until(job.nextRunAt, this.now)})` : "No next run";
    const badge = job.running
      ? svg("mods/workspace-chrome/spinner.svg").size(12).flex_none().text_color(t.working).spin(900)
      : div().size(8).flex_none().rounded(4).bg(t[st.color] ?? t.dim);
    const history = (job.recentRuns || []).filter((r) => r.status !== "skipped");

    return v_flex()
      .id(`job-${job.slug}`)
      .w_full()
      .p(12)
      .gap(6)
      .rounded(8)
      .bg(t.surface)
      .border(1)
      .border_color(st.color === "blocked" ? t.blocked ?? t.border : t.border)
      .child(h_flex().gap(8).items_center()
        .child(badge)
        .child(div().flex_1().truncate().text_size(14).font_semibold().text_color(t.foreground).child(job.name))
        .child(div().text_size(12).text_color(t[st.color] ?? t.muted).child(st.text)))
      .child(div().text_size(12).text_color(t.muted).child(`${job.scheduleText} · ${next}`))
      .child(h_flex().gap(8).items_center()
        .child(div().flex_1().truncate().text_size(12).text_color(last && last.status !== "success" ? t.blocked ?? t.muted : t.muted).child(lastText))
        .child(h_flex().gap(3).items_center().children(history.map((r) => this.dot(t, r.status)))))
      .when(Boolean(last?.error), (el) => el.child(div().text_size(11).text_color(t.dim).truncate().child(last.error)))
      .child(h_flex().gap(6).pt(4).flex_wrap()
        .child(this.button(t, `run-${job.slug}`, busy === "run" ? "Starting…" : "Run now", (cx) => this.act(cx, job, "run"),
          { primary: true, disabled: Boolean(busy) || job.running }))
        .child(job.paused
          ? this.button(t, `resume-${job.slug}`, busy === "resume" ? "Resuming…" : "Resume", (cx) => this.act(cx, job, "resume"), { disabled: Boolean(busy) })
          : this.button(t, `pause-${job.slug}`, busy === "pause" ? "Pausing…" : "Pause", (cx) => this.act(cx, job, "pause"), { disabled: Boolean(busy) }))
        .child(this.button(t, `log-${job.slug}`, "Log", () => this.showLog(job)))
        .when(Boolean(job.open || job.expectedFile), (el) =>
          el.child(this.button(t, `open-${job.slug}`, "Open output", () => this.openOutput(job)))));
  }

  render(_cx) {
    const t = theme();
    const jobs = this.jobs || [];
    const header = h_flex().gap(8).items_center().pb(4)
      .child(svg("mods/workspace-chrome/clock.svg").size(14).flex_none().text_color(t.muted))
      .child(div().flex_1().text_size(13).font_semibold().text_color(t.foreground).child(`Scheduled jobs${this.jobs ? ` · ${jobs.length}` : ""}`))
      .child(this.button(t, "refresh", "Refresh", (cx) => this.refresh(cx)));
    let body;
    if (this.error) {
      body = div().text_size(12).text_color(t.blocked ?? t.foreground).child(`Couldn't read jobs: ${this.error}`);
    } else if (!this.jobs) {
      body = div().text_size(12).text_color(t.dim).child("Loading…");
    } else if (jobs.length === 0) {
      body = div().text_size(12).text_color(t.dim).child("No jobs yet. Ask Claude Code to schedule one; see the scheduling-agent-jobs skill.");
    } else {
      body = v_flex().gap(10).w_full().children(jobs.map((j) => this.card(t, j)));
    }
    return v_flex()
      .id("root")
      .size_full()
      .bg(t.canvas ?? t.background)
      .p(14)
      .gap(10)
      .overflow_y_scrollbar()
      .child(header)
      .child(body);
  }
}
