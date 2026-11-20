#!/bin/bash
# Undoes install.sh:
#   - removes ~/.claude/skills/scheduling-agent-jobs, but only if it's a link to
#     this clone's skill,
#   - removes ~/.config/herdr-app/mods/schedules, but only if install.sh put it
#     there and nobody has changed it since, and
#   - lists the scheduled jobs that remain, with the command that removes each.
#     With --remove-jobs, it runs those removals itself.
#
# Jobs' run history and logs stay in ~/.config/scheduled-jobs either way.
#
# Exit codes:
#   0  done (anything left in place is listed in the output)
#   1  a prerequisite is missing, or a step failed unexpectedly
#   3  done, but some jobs failed to remove
#
# Overrides (mostly for the tests): SCHEDULED_JOBS_* pass through to jobs.py.

unset CDPATH

usage() {
  cat <<'EOF'
Usage: ./uninstall.sh [--remove-jobs]
  --remove-jobs  also remove every scheduled job (their run history and logs stay)
EOF
}

remove_jobs=0
for arg in "$@"; do
  case "$arg" in
    --remove-jobs) remove_jobs=1 ;;
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
MARKER=".installed-by-herdr-scheduled-jobs"
GENERATED="gpui-kit.d.ts jsconfig.json" # herdr-app check writes these into a mod folder
SKILL_DST="$HOME/.claude/skills/scheduling-agent-jobs"
MOD_DST="$HOME/.config/herdr-app/mods/schedules"
STATE="${SCHEDULED_JOBS_HOME:-$HOME/.config/scheduled-jobs}"
PYTHON=/usr/bin/python3

if [ $remove_jobs = 1 ] && ! "$PYTHON" -B -c "" >/dev/null 2>&1; then
  echo "python3: $PYTHON doesn't run, so --remove-jobs can't work. Install Apple's command line tools: xcode-select --install"
  echo "Nothing was changed."
  exit 1
fi

# The file names the marker in folder $1 lists. Fails if the marker isn't in the
# format install.sh writes. (Kept in step with install.sh.)
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

# Prints each listed file in mod folder $1 whose content changed since install.sh
# copied it there.
mod_edits() {
  local dir=$1 line want have
  marker_files "$dir" >/dev/null || { echo "$MARKER (not in the format install.sh writes)"; return; }
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in "commit "*) continue ;; esac
    want=${line%%  *}
    file=${line#*  }
    [ -e "$dir/$file" ] || [ -L "$dir/$file" ] || continue
    have=$(shasum -a 256 "$dir/$file" 2>/dev/null)
    [ "${have%% *}" = "$want" ] || echo "$file"
  done <"$dir/$MARKER"
}

prefixed() {
  printf '%s\n' "$1" | while IFS= read -r line; do echo "$2$line"; done
}

# ---------- skill ----------

if [ -L "$SKILL_DST" ]; then
  if [ "$(cd "$SKILL_DST" 2>/dev/null && pwd -P)" = "$SKILL_SRC" ]; then
    rm "$SKILL_DST" || { echo "error: couldn't remove $SKILL_DST" >&2; exit 1; }
    echo "skill: removed the ~/.claude/skills/scheduling-agent-jobs link"
  else
    echo "skill: left ~/.claude/skills/scheduling-agent-jobs alone; it links to $(readlink "$SKILL_DST"), not this clone"
  fi
elif [ -e "$SKILL_DST" ]; then
  echo "skill: left ~/.claude/skills/scheduling-agent-jobs alone; it isn't a link to this clone"
else
  echo "skill: not installed"
fi

# ---------- mod ----------

if [ ! -e "$MOD_DST" ] && [ ! -L "$MOD_DST" ]; then
  echo "mod: not installed"
elif [ -L "$MOD_DST" ] || [ ! -d "$MOD_DST" ] || [ ! -f "$MOD_DST/$MARKER" ]; then
  echo "mod: left ~/.config/herdr-app/mods/schedules alone; install.sh didn't put it there"
else
  edits=$(mod_edits "$MOD_DST")
  if [ -n "$edits" ]; then
    echo "mod: left ~/.config/herdr-app/mods/schedules alone; these files were changed since it was installed:"
    prefixed "$edits" "  "
  else
    { marker_files "$MOD_DST"; printf '%s\n' $GENERATED "$MARKER"; } | while IFS= read -r f; do
      rm -f "$MOD_DST/$f" || { echo "error: couldn't remove $MOD_DST/$f" >&2; exit 1; }
    done || exit 1
    if rmdir "$MOD_DST" 2>/dev/null; then
      echo "mod: removed ~/.config/herdr-app/mods/schedules"
    else
      echo "mod: removed the Schedules mod's files, but left ~/.config/herdr-app/mods/schedules because it holds other files:"
      prefixed "$(ls -A "$MOD_DST")" "  "
    fi
  fi
fi

# ---------- jobs ----------

status=0
slugs=""
for spec in "$STATE"/jobs/*.json; do
  [ -f "$spec" ] || continue
  slug=$(basename "$spec" .json)
  slugs="$slugs $slug"
done

if [ -z "$slugs" ]; then
  echo "jobs: none scheduled"
elif [ $remove_jobs = 1 ]; then
  for slug in $slugs; do
    if out=$("$PYTHON" "$JOBS_PY" remove "$slug" 2>&1); then
      prefixed "$out" "jobs: "
    else
      prefixed "$out" "jobs: "
      echo "jobs: failed to remove $slug"
      status=3
    fi
  done
else
  echo "jobs: these jobs are still scheduled and run from $JOBS_PY. To remove each one:"
  for slug in $slugs; do
    echo "  $PYTHON $(printf '%q' "$JOBS_PY") remove $slug"
  done
  echo "jobs: or re-run with --remove-jobs to remove them all. Their run history and logs stay in $STATE."
fi

exit $status
