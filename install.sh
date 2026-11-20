#!/bin/bash
# Installs this clone for Claude Code and Herdr:
#   1. checks the prerequisites,
#   2. links skills/scheduling-agent-jobs into ~/.claude/skills/,
#   3. copies the Schedules mod into Herdr (when Herdr is installed), and
#   4. re-points any existing scheduled jobs at this clone's jobs.py.
#
# Safe to re-run, for example after `git pull`. It never replaces a skill or mod
# it didn't install unless you pass --replace-skill or --replace-mod, and then it
# moves the old one to ~/.config/herdr-scheduled-jobs/backups/ first. It never installs packages, edits Claude Code's
# settings, runs the smoke test, or schedules a job.
#
# Exit codes:
#   0  done
#   1  a prerequisite is missing, the clone is in a folder launchd jobs can't read, or a step failed
#   2  stopped to protect existing files (nothing was changed)
#   3  installed, but `jobs.py relink` exited nonzero (usually a job that failed to relink)
#   4  installed, but `herdr-app check` failed on the mod (never with --no-mod, which
#      skips the mod and ignores --replace-mod)
#   5  installed, but `jobs.py relink` skipped a job that was running, so it still
#      points at the old jobs.py (3 wins when relink also failed)
#
# Overrides (mostly for the tests): CLAUDE_BIN, NOTIFIER_BIN, and HERDR_APP_BIN are
# paths to use instead of searching; SCHEDULED_JOBS_* pass through to jobs.py.

unset CDPATH
set -f # no globbing: every list here is plain file names

usage() {
  cat <<'EOF'
Usage: ./install.sh [--replace-skill] [--replace-mod] [--no-mod]
  --replace-skill  move whatever is at ~/.claude/skills/scheduling-agent-jobs into
                   ~/.config/herdr-scheduled-jobs/backups/<date-time>/, then link
                   this clone's skill there
  --replace-mod    move ~/.config/herdr-app/mods/schedules into the same backup
                   folder, then install this clone's Schedules mod there
  --no-mod         don't install or check the Herdr mod, even if Herdr is installed
EOF
}

replace_skill=0
replace_mod=0
no_mod=0
for arg in "$@"; do
  case "$arg" in
    --replace-skill) replace_skill=1 ;;
    --replace-mod) replace_mod=1 ;;
    --no-mod) no_mod=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

if [ -z "${HOME:-}" ] || [ ! -d "$HOME" ]; then
  echo "error: HOME is not set to a folder" >&2
  exit 1
fi

REPO=$(cd "$(dirname "$0")" && pwd -P) || exit 1
SKILL_SRC="$REPO/skills/scheduling-agent-jobs"
JOBS_PY="$SKILL_SRC/scripts/jobs.py"
MOD_SRC="$REPO/mods/schedules"
MOD_FILES="main.js mod.json" # only these are installed; herdr-app writes others into the folder itself
MARKER=".installed-by-herdr-scheduled-jobs"
SKILL_DST="$HOME/.claude/skills/scheduling-agent-jobs"
MOD_DST="$HOME/.config/herdr-app/mods/schedules"
# Backups go outside the live folders: one under ~/.claude/skills/ would load as a
# second copy of the skill, and one under Herdr's mods/ would show up as another mod.
BACKUPS="$HOME/.config/herdr-scheduled-jobs/backups/$(date +%Y%m%d-%H%M%S)"
PYTHON=/usr/bin/python3

is_exe() { [ -f "$1" ] && [ -x "$1" ]; }

die() {
  echo "error: $*" >&2
  exit 1
}

# Same search order as find_claude() in jobs.py. A CLAUDE_BIN that isn't there
# counts as missing, so the search stops at an explicit override.
find_claude() {
  if [ -n "${CLAUDE_BIN:-}" ]; then
    is_exe "$CLAUDE_BIN" && echo "$CLAUDE_BIN"
    return 0
  fi
  local p
  for p in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /opt/homebrew/bin/claude /usr/local/bin/claude; do
    if is_exe "$p"; then echo "$p"; return 0; fi
  done
  command -v claude
}

find_notifier() {
  if [ -n "${NOTIFIER_BIN:-}" ]; then
    is_exe "$NOTIFIER_BIN" && echo "$NOTIFIER_BIN"
    return 0
  fi
  (PATH="/opt/homebrew/bin:/usr/local/bin:$PATH" && command -v terminal-notifier)
}

# Only the caller's PATH: Herdr installs herdr-app wherever the user chose.
find_herdr() {
  if [ -n "${HERDR_APP_BIN:-}" ]; then
    is_exe "$HERDR_APP_BIN" && echo "$HERDR_APP_BIN"
    return 0
  fi
  command -v herdr-app
}

# Moves $1 into this run's backup folder and prints where it went. Both backups in
# one run share the folder; a clash with an earlier run in the same second gets -2.
back_up() {
  local name n=2
  name="$BACKUPS/$(basename "$1")"
  mkdir -p "$BACKUPS" || return 1
  while [ -e "$name" ] || [ -L "$name" ]; do
    name="$BACKUPS/$(basename "$1")-$n"
    n=$((n + 1))
  done
  mv "$1" "$name" || return 1
  echo "$name"
}

# The file names the marker in folder $1 lists, one per line. Fails if the marker
# isn't in the format this script writes.
marker_files() {
  local line first=1
  while IFS= read -r line || [ -n "$line" ]; do
    if [ $first = 1 ]; then
      first=0
      case "$line" in "commit "*) continue ;; *) return 1 ;; esac
    fi
    [ -z "$line" ] && continue
    case "$line" in *"  "*) ;; *) return 1 ;; esac
    case "${line#*  }" in "" | . | .. | */*) return 1 ;; esac
    echo "${line#*  }"
  done <"$1/$MARKER"
  [ $first = 0 ]
}

# Prints each file in mod folder $1 that it isn't safe to overwrite: a file the
# marker lists whose content changed since it was installed, or a file this clone
# would install that the marker doesn't list. A listed file that's gone is fine.
mod_edits() {
  local dir=$1 line file want have listed
  listed=$(marker_files "$dir") || { echo "$MARKER (not in the format this script writes)"; return; }
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in "commit "*) continue ;; esac
    want=${line%%  *}
    file=${line#*  }
    [ -e "$dir/$file" ] || [ -L "$dir/$file" ] || continue
    have=$(shasum -a 256 "$dir/$file" 2>/dev/null)
    [ "${have%% *}" = "$want" ] || echo "$file"
  done <"$dir/$MARKER"
  for file in $MOD_FILES; do
    if ! printf '%s\n' "$listed" | grep -qxF "$file" && { [ -e "$dir/$file" ] || [ -L "$dir/$file" ]; }; then
      echo "$file"
    fi
  done
}

source_commit() {
  local sha=""
  if [ -e "$REPO/.git" ] && command -v git >/dev/null 2>&1; then
    sha=$(git -C "$REPO" rev-parse HEAD 2>/dev/null)
  fi
  echo "${sha:-unknown}"
}

# Copies the mod's files into $MOD_DST (leaving anything else there alone), drops
# files an earlier install put there that this clone no longer has, and rewrites
# the marker.
install_mod() {
  local f old=""
  if [ -f "$MOD_DST/$MARKER" ]; then
    old=$(marker_files "$MOD_DST") || old=""
  fi
  mkdir -p "$MOD_DST" || return 1
  for f in $MOD_FILES; do
    cmp -s "$MOD_SRC/$f" "$MOD_DST/$f" && continue
    # Herdr reloads on every save but ignores hidden files, so copy, then rename.
    cp "$MOD_SRC/$f" "$MOD_DST/.$f.tmp" && mv -f "$MOD_DST/.$f.tmp" "$MOD_DST/$f" || return 1
  done
  for f in $old; do
    case " $MOD_FILES " in *" $f "*) ;; *) rm -f "$MOD_DST/$f" ;; esac
  done
  # shellcheck disable=SC2086 # MOD_FILES is a list of plain file names
  { echo "commit $(source_commit)" && (cd "$MOD_DST" && shasum -a 256 $MOD_FILES); } >"$MOD_DST/$MARKER.tmp" &&
    mv -f "$MOD_DST/$MARKER.tmp" "$MOD_DST/$MARKER"
}

# Prints each line of $1 with prefix $2.
prefixed() {
  printf '%s\n' "$1" | while IFS= read -r line; do echo "$2$line"; done
}

# ---------- 1. checks (nothing changes until they all pass) ----------

# macOS privacy protection stops launchd jobs reading these folders, so every job
# would fail with "Operation not permitted" and no notification.
for dir in "$HOME/Documents" "$HOME/Desktop" "$HOME/Downloads" "$HOME/Library/Mobile Documents"; do
  real=$(cd "$dir" 2>/dev/null && pwd -P) || real=$dir
  case "$REPO/" in
    "$real"/* | "$dir"/*)
      echo "install: this clone is in $dir, which macOS doesn't let scheduled jobs read; move it (for example to ~/git/herdr-scheduled-jobs) and run install.sh again" >&2
      exit 1
      ;;
  esac
done

missing=0
if [ "$(uname)" != Darwin ]; then
  echo "macOS: this only runs on macOS, because launchd runs the jobs"
  missing=1
fi
if ! "$PYTHON" -B -c "" >/dev/null 2>&1; then
  echo "python3: $PYTHON doesn't run. Install Apple's command line tools: xcode-select --install"
  missing=1
fi
claude=$(find_claude)
if [ -z "$claude" ]; then
  echo "claude: Claude Code isn't installed. Install Claude Code: https://docs.claude.com/en/docs/claude-code/setup"
  missing=1
fi
notifier=$(find_notifier)
if [ -z "$notifier" ]; then
  echo "terminal-notifier: not installed, so jobs can't notify you. Install it: brew install terminal-notifier"
  missing=1
fi
if [ $missing = 1 ]; then
  echo "Nothing was changed. Fix the above, then run this again."
  exit 1
fi

# Work out what each step would do, and stop before changing anything if a step
# would overwrite something this script didn't put there.
stop=0

skill_action=link
skill_found=""
if [ -L "$SKILL_DST" ]; then
  if [ "$(cd "$SKILL_DST" 2>/dev/null && pwd -P)" = "$SKILL_SRC" ]; then
    skill_action=linked
  else
    skill_found=$(readlink "$SKILL_DST")
  fi
elif [ -d "$SKILL_DST" ]; then
  skill_found="a folder"
elif [ -e "$SKILL_DST" ]; then
  skill_found="a file"
fi
if [ -n "$skill_found" ]; then
  if [ $replace_skill = 1 ]; then
    skill_action=replace
  else
    echo "skill: ~/.claude/skills/scheduling-agent-jobs already exists ($skill_found); re-run with --replace-skill to replace it"
    stop=1
  fi
fi

herdr=""
[ $no_mod = 1 ] || herdr=$(find_herdr)
mod_action=skip
if [ $no_mod = 0 ] && { [ -n "$herdr" ] || [ -d "$HOME/.config/herdr-app" ]; }; then
  mod_found=""
  if [ ! -e "$MOD_DST" ] && [ ! -L "$MOD_DST" ]; then
    mod_action=copy
  elif [ -L "$MOD_DST" ]; then
    mod_found="it's a link to $(readlink "$MOD_DST"), not a copy this script installed"
  elif [ ! -d "$MOD_DST" ]; then
    mod_found="it isn't a folder"
  elif [ ! -f "$MOD_DST/$MARKER" ]; then
    mod_found="this script didn't install it (it has no $MARKER file)"
  else
    edits=$(mod_edits "$MOD_DST")
    if [ -z "$edits" ]; then
      mod_action=update
    else
      mod_found="these files were changed since it was installed:
$(prefixed "$edits" "  ")"
    fi
  fi
  if [ -n "$mod_found" ]; then
    if [ $replace_mod = 1 ]; then
      mod_action=replace
    else
      echo "mod: ~/.config/herdr-app/mods/schedules already exists, and $mod_found"
      echo "mod: re-run with --replace-mod to back it up and replace it"
      stop=1
    fi
  fi
fi

if [ $stop = 1 ]; then
  echo "Stopped to protect the files above. Nothing was changed."
  exit 2
fi

# ---------- 2. skill ----------

mkdir -p "$HOME/.claude/skills" || die "couldn't create ~/.claude/skills"
case $skill_action in
  linked)
    echo "skill: already linked"
    ;;
  link)
    ln -s "$SKILL_SRC" "$SKILL_DST" || die "couldn't link $SKILL_DST"
    echo "skill: linked ~/.claude/skills/scheduling-agent-jobs -> $SKILL_SRC"
    ;;
  replace)
    bak=$(back_up "$SKILL_DST") || die "couldn't move $SKILL_DST into $BACKUPS"
    ln -s "$SKILL_SRC" "$SKILL_DST" || die "couldn't link $SKILL_DST (the old one is at $bak)"
    echo "skill: backed up the old one to $bak"
    echo "skill: linked ~/.claude/skills/scheduling-agent-jobs -> $SKILL_SRC"
    ;;
esac

# ---------- 3. Herdr mod ----------

check_failed=0
if [ $no_mod = 1 ]; then
  echo "herdr: skipped (--no-mod)"
elif [ $mod_action = skip ]; then
  echo "herdr: not installed, skipping the Schedules tab"
else
  case $mod_action in
    copy)
      install_mod || die "couldn't install the mod into $MOD_DST"
      echo "mod: installed ~/.config/herdr-app/mods/schedules"
      ;;
    update)
      install_mod || die "couldn't update the mod in $MOD_DST"
      echo "mod: updated ~/.config/herdr-app/mods/schedules"
      ;;
    replace)
      bak=$(back_up "$MOD_DST") || die "couldn't move $MOD_DST into $BACKUPS"
      install_mod || die "couldn't install the mod into $MOD_DST (the old one is at $bak)"
      echo "mod: backed up the old one to $bak"
      echo "mod: installed ~/.config/herdr-app/mods/schedules"
      ;;
  esac
  if [ -n "$herdr" ]; then
    if out=$("$herdr" check "$MOD_DST" 2>&1); then
      echo "herdr: herdr-app check passed"
    else
      echo "herdr: herdr-app check failed on $MOD_DST:"
      prefixed "$out" "  "
      check_failed=1
    fi
  else
    echo "herdr: herdr-app not found on PATH, so herdr-app check was skipped; run it yourself"
  fi
fi

# ---------- 4. jobs ----------

out=$("$PYTHON" "$JOBS_PY" relink 2>&1)
relink_status=$?
prefixed "$out" "jobs: "
relink_skipped=0
case "
$out" in *"
skipped "*) relink_skipped=1 ;; esac

# ---------- 5. summary ----------

echo
echo "Next steps:"
echo "  - Check that unattended runs work on this Mac:"
echo "      $PYTHON $(printf '%q' "$JOBS_PY") smoke-test"
if [ $mod_action != skip ]; then
  echo "  - Open the Schedules tab in Herdr:"
  echo "      herdr-app do tab.new --name Schedules --mod schedules"
fi

status=0
if [ $check_failed = 1 ]; then
  echo
  echo "Installed, but herdr-app check failed on the mod (its output is above)."
  status=4
fi
if [ $relink_status != 0 ]; then
  echo
  echo "Installed, but some jobs failed to relink (the \"failed\" lines above). Fix them, then run:"
  echo "  $PYTHON $(printf '%q' "$JOBS_PY") relink"
  status=3
elif [ $relink_skipped = 1 ]; then
  echo
  echo "jobs: some jobs were running and weren't re-pointed; run $PYTHON $(printf '%q' "$JOBS_PY") relink after they finish"
  status=5
fi
exit $status
