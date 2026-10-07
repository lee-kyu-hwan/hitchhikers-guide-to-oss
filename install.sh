#!/usr/bin/env bash
# install.sh — link this checkout's skills and executables into place (hitchhikers-guide-to-oss#1).
#
# Everything is a symlink into this checkout, so the installed version is whatever this
# checkout has checked out (git checkout vX.Y.Z, then run this again). Nothing is copied.
#
#   ./install.sh --project DIR   link the 6 skills into DIR/.agents/skills and DIR/.claude/skills
#   ./install.sh --user          link the 6 skills into $HOME/.agents/skills
#   ./install.sh --check ...     report every link as ok / missing / conflict (exit 1 unless all ok)
#   ./install.sh --uninstall ... remove the links that point into this checkout (others are kept)
#   ./install.sh --force ...     replace a symlink to somewhere else or a regular file in the way
#
# The 12 executables bin/oss-research-* are always linked into ${BIN_DIR:-$HOME/.local/bin}.
# HOME and BIN_DIR are plain environment variables, so a test can point them at a temporary tree.
#
# The workflow skill (researching-open-source-contributions) is linked item by item (SKILL.md,
# scripts, references, tests) inside a real directory, not as one directory link: its analysis/ is
# the isolated analysis bundle (host harness, stage prompts, strict checks) and must not be exposed
# as skill content to Codex or Claude Code. This is the same shape the dotfiles install used.
#
# Runs on macOS (bash 3.2) and Linux. Nothing is created until every link has been inspected: a
# conflict stops the whole install before the first link, so a partial install never happens.
set -eu

WORKFLOW_SKILL=researching-open-source-contributions
WORKFLOW_ITEMS="SKILL.md scripts references tests"

usage() {
  sed -n '2,/^set -eu/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
}

die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 2
}

ROOT=$(cd "$(dirname "$0")" && pwd -P)
[ -d "$ROOT/skills" ] && [ -d "$ROOT/bin" ] || die "not a checkout root (skills/ and bin/ expected): $ROOT"

MODE=install
FORCE=0
PROJECT=
USER_SCOPE=0
while [ $# -gt 0 ]; do
  case $1 in
    --project)
      [ $# -ge 2 ] || die "--project needs a directory"
      PROJECT=$2
      shift 2
      ;;
    --user) USER_SCOPE=1; shift ;;
    --check) MODE=check; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
[ -n "$PROJECT" ] || [ "$USER_SCOPE" = 1 ] || die "choose a scope: --project DIR and/or --user (see --help)"
if [ -n "$PROJECT" ]; then
  [ -d "$PROJECT" ] || die "project directory does not exist: $PROJECT"
  PROJECT=$(cd "$PROJECT" && pwd)
fi
BIN_DIR=${BIN_DIR:-$HOME/.local/bin}

# ---------------------------------------------------------------------------
# Plan: parallel arrays of link path and expected target, plus the real directories that hold
# the workflow skill's item links. Order: skills per scope, then executables.
# ---------------------------------------------------------------------------
LINKS=()
TARGETS=()
CONTAINERS=()
SCOPE_ROOTS=()

add_link() {
  LINKS+=("$1")
  TARGETS+=("$2")
}

add_scope() {
  SCOPE_ROOTS+=("$1")
  for skill_dir in "$ROOT"/skills/*/; do
    skill=$(basename "$skill_dir")
    if [ "$skill" = "$WORKFLOW_SKILL" ]; then
      CONTAINERS+=("$1/$skill")
      for item in $WORKFLOW_ITEMS; do
        add_link "$1/$skill/$item" "$ROOT/skills/$skill/$item"
      done
    else
      add_link "$1/$skill" "$ROOT/skills/$skill"
    fi
  done
}

if [ -n "$PROJECT" ]; then
  add_scope "$PROJECT/.agents/skills"
  add_scope "$PROJECT/.claude/skills"
fi
if [ "$USER_SCOPE" = 1 ]; then
  add_scope "$HOME/.agents/skills"
fi
for exe in "$ROOT"/bin/oss-research-*; do
  add_link "$BIN_DIR/$(basename "$exe")" "$exe"
done

# ok | missing | foreign-link | file | dir
state_of() {
  if [ -L "$1" ]; then
    if [ "$(readlink "$1")" = "$2" ]; then echo ok; else echo foreign-link; fi
  elif [ -d "$1" ]; then echo dir
  elif [ -e "$1" ]; then echo file
  else echo missing
  fi
}

# A container must be a real directory: a symlink there would expose the whole skill (analysis/).
container_state() {
  if [ -L "$1" ]; then echo foreign-link
  elif [ -d "$1" ]; then echo ok
  elif [ -e "$1" ]; then echo file
  else echo missing
  fi
}

describe() {
  case $1 in
    foreign-link) printf 'symlink to %s' "$(readlink "$2")" ;;
    file) printf 'a regular file' ;;
    dir) printf 'a directory' ;;
  esac
}

report() {
  printf '%-9s %s\n' "$1" "$2"
}

count=${#LINKS[@]}
n_ok=0 n_missing=0 n_conflict=0 n_hard=0

# ---------------------------------------------------------------------------
# Phase 1: inspect everything.
# ---------------------------------------------------------------------------
for container in "${CONTAINERS[@]}"; do
  s=$(container_state "$container")
  case $s in
    ok|missing) ;;
    foreign-link|file)
      n_conflict=$((n_conflict + 1))
      if [ "$MODE" = uninstall ]; then
        report skip "$container (not a directory made by install.sh)"
      else
        report conflict "$container ($(describe "$s" "$container"); a real directory is needed here)"
      fi
      ;;
  esac
done
i=0
while [ "$i" -lt "$count" ]; do
  link=${LINKS[$i]}
  target=${TARGETS[$i]}
  s=$(state_of "$link" "$target")
  case $MODE in
    check)
      case $s in
        ok) n_ok=$((n_ok + 1)); report ok "$link" ;;
        missing) n_missing=$((n_missing + 1)); report missing "$link" ;;
        *) n_conflict=$((n_conflict + 1)); report conflict "$link ($(describe "$s" "$link"))" ;;
      esac
      ;;
    install)
      case $s in
        ok|missing) ;;
        dir)
          n_conflict=$((n_conflict + 1)); n_hard=$((n_hard + 1))
          report conflict "$link (a directory; --force never removes a directory)"
          ;;
        *)
          n_conflict=$((n_conflict + 1))
          report conflict "$link ($(describe "$s" "$link"))"
          ;;
      esac
      ;;
    uninstall) ;;
  esac
  i=$((i + 1))
done

case $MODE in
  check)
    printf 'check: %d ok, %d missing, %d conflict (of %d)\n' "$n_ok" "$n_missing" "$n_conflict" "$count"
    if [ "$n_missing" = 0 ] && [ "$n_conflict" = 0 ]; then exit 0; else exit 1; fi
    ;;
  install)
    if [ "$n_hard" -gt 0 ] || { [ "$n_conflict" -gt 0 ] && [ "$FORCE" = 0 ]; }; then
      printf 'install: stopped before linking anything: %d conflict' "$n_conflict" >&2
      if [ "$n_hard" -gt 0 ]; then printf ' (%d cannot be replaced by --force)\n' "$n_hard" >&2
      else printf ' (rerun with --force to replace symlinks and regular files)\n' >&2; fi
      exit 1
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# Phase 2: apply.
# ---------------------------------------------------------------------------
n_linked=0 n_present=0 n_replaced=0 n_removed=0 n_skipped=0
if [ "$MODE" = install ]; then
  for container in "${CONTAINERS[@]}"; do
    case $(container_state "$container") in
      foreign-link|file) rm -f "$container"; report replace "$container (now a directory)" ;;
    esac
    mkdir -p "$container"
  done
  i=0
  while [ "$i" -lt "$count" ]; do
    link=${LINKS[$i]}
    target=${TARGETS[$i]}
    case $(state_of "$link" "$target") in
      ok) n_present=$((n_present + 1)); report ok "$link" ;;
      missing)
        mkdir -p "$(dirname "$link")"
        ln -s "$target" "$link"
        n_linked=$((n_linked + 1)); report link "$link -> $target"
        ;;
      foreign-link|file)
        rm -f "$link"
        ln -s "$target" "$link"
        n_replaced=$((n_replaced + 1)); report replace "$link -> $target"
        ;;
    esac
    i=$((i + 1))
  done
  printf 'install: %d linked, %d already present, %d replaced (of %d)\n' "$n_linked" "$n_present" "$n_replaced" "$count"
  exit 0
fi

# uninstall
i=0
while [ "$i" -lt "$count" ]; do
  link=${LINKS[$i]}
  target=${TARGETS[$i]}
  case $(state_of "$link" "$target") in
    ok) rm -f "$link"; n_removed=$((n_removed + 1)); report unlink "$link" ;;
    missing) ;;
    *) n_skipped=$((n_skipped + 1)); report skip "$link (not a link into this checkout)" ;;
  esac
  i=$((i + 1))
done
# Directories this script may have made, removed only when empty (rmdir never removes content).
for container in "${CONTAINERS[@]}"; do
  [ -d "$container" ] && [ ! -L "$container" ] && rmdir "$container" 2>/dev/null || true
done
for base in ${SCOPE_ROOTS[@]+"${SCOPE_ROOTS[@]}"}; do
  rmdir "$base" 2>/dev/null && rmdir "$(dirname "$base")" 2>/dev/null || true
done
rmdir "$BIN_DIR" 2>/dev/null || true
printf 'uninstall: %d unlinked, %d skipped (of %d)\n' "$n_removed" "$n_skipped" "$count"
exit 0
