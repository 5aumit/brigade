#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
STACK_REPO=$(dirname -- "$SCRIPT_DIR")
AGENTS_DIR="${HOME}/.agents"
CLAUDE_DIR="${HOME}/.claude"
ANTIGRAVITY_DIR="${HOME}/.gemini/config"
T3_DIR="${HOME}/.t3/userdata"
BIN_DIR="${HOME}/.local/bin"
DRY_RUN=0

usage() {
  echo "Usage: $0 [--dry-run] [--agents-dir PATH] [--claude-dir PATH] [--antigravity-dir PATH] [--t3-dir PATH] [--bin-dir PATH]"
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --agents-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      AGENTS_DIR=$2
      shift 2
      ;;
    --claude-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      CLAUDE_DIR=$2
      shift 2
      ;;
    --antigravity-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      ANTIGRAVITY_DIR=$2
      shift 2
      ;;
    --t3-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      T3_DIR=$2
      shift 2
      ;;
    --bin-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      BIN_DIR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

BACKUP_DIR="$AGENTS_DIR/brigade-backups/v1"
CLAUDE_BACKUP_DIR="$CLAUDE_DIR/brigade-backups/v1"
ANTIGRAVITY_BACKUP_DIR="$ANTIGRAVITY_DIR/brigade-backups/v1"
LEGACY_BACKUP_DIR="$AGENTS_DIR/5stack-backups/v1"
SKILLS_DIR="$AGENTS_DIR/skills"
CLAUDE_SKILLS_DIR="$CLAUDE_DIR/skills"
ANTIGRAVITY_SKILLS_DIR="$ANTIGRAVITY_DIR/skills"
# T3 Code runs Antigravity with a private config folder per provider instance.
# T3 links skills there itself, but not global rules.
T3_ANTIGRAVITY_CONFIGS=()
for config in "$T3_DIR"/providers/antigravity/*/config; do
  [[ -d "$config" ]] && T3_ANTIGRAVITY_CONFIGS+=("$config")
done
ROOT_LINK="$AGENTS_DIR/brigade"
BIN_LINK="$BIN_DIR/brigade"
LEGACY_ROOT_LINK="$AGENTS_DIR/5stack"
LEGACY_BIN_LINK="$BIN_DIR/5stack"
mapfile -t OWNED_SKILLS < "$STACK_REPO/skills/owned.txt"
LEGACY_SKILLS=(5stack-setup feedback reflect give-5stack-feedback reflect-5stack review-5stack-feedback)

say_action() {
  if ((DRY_RUN)); then
    echo "DRY RUN: $*"
  else
    echo "$*"
  fi
}

remove_owned_link() {
  local source_path=$1 target_path=$2 backup_path=$3 resolved_source resolved_target
  resolved_source=$(readlink -f -- "$source_path")

  if [[ -L "$target_path" ]]; then
    resolved_target=$(readlink -f -- "$target_path" 2>/dev/null || true)
    if [[ "$resolved_target" == "$resolved_source" ]]; then
      say_action "Remove Brigade link $target_path"
      ((!DRY_RUN)) && rm -- "$target_path"
    else
      echo "Leave non-Brigade link unchanged: $target_path"
      return
    fi
  elif [[ -e "$target_path" ]]; then
    echo "Leave non-link path unchanged: $target_path"
    return
  fi

  if [[ -e "$backup_path" || -L "$backup_path" ]]; then
    say_action "Restore $backup_path -> $target_path"
    if ((!DRY_RUN)); then
      mkdir -p -- "$(dirname -- "$target_path")"
      mv -- "$backup_path" "$target_path"
    fi
  fi
}

remove_legacy_link() {
  local source_path=$1 target_path=$2 backup_path=$3 expected_target actual_target
  expected_target=$(readlink -m -- "$source_path")

  if [[ -L "$target_path" ]]; then
    actual_target=$(readlink -m -- "$target_path")
    if [[ "$actual_target" != "$expected_target" ]]; then
      echo "Leave non-Brigade legacy link unchanged: $target_path"
      return
    fi
    say_action "Remove legacy 5stack link $target_path"
    ((!DRY_RUN)) && rm -- "$target_path"
  elif [[ -e "$target_path" ]]; then
    echo "Leave non-link legacy path unchanged: $target_path"
    return
  fi

  if [[ -e "$backup_path" || -L "$backup_path" ]]; then
    say_action "Restore $backup_path -> $target_path"
    if ((!DRY_RUN)); then
      mkdir -p -- "$(dirname -- "$target_path")"
      mv -- "$backup_path" "$target_path"
    fi
  fi
}

for skill in "${OWNED_SKILLS[@]}"; do
  remove_owned_link \
    "$STACK_REPO/skills/$skill" \
    "$SKILLS_DIR/$skill" \
    "$BACKUP_DIR/skills/$skill"
done

for skill in "${OWNED_SKILLS[@]}"; do
  remove_owned_link \
    "$STACK_REPO/skills/$skill" \
    "$CLAUDE_SKILLS_DIR/$skill" \
    "$CLAUDE_BACKUP_DIR/skills/$skill"
  remove_owned_link \
    "$STACK_REPO/skills/$skill" \
    "$ANTIGRAVITY_SKILLS_DIR/$skill" \
    "$ANTIGRAVITY_BACKUP_DIR/skills/$skill"
done

for skill in "${LEGACY_SKILLS[@]}"; do
  remove_legacy_link \
    "$STACK_REPO/skills/$skill" \
    "$SKILLS_DIR/$skill" \
    "$LEGACY_BACKUP_DIR/skills/$skill"
done

remove_owned_link "$STACK_REPO/AGENTS.md" "$AGENTS_DIR/AGENTS.md" "$BACKUP_DIR/AGENTS.md"
remove_owned_link "$STACK_REPO/AGENTS.md" "$CLAUDE_DIR/CLAUDE.md" "$CLAUDE_BACKUP_DIR/CLAUDE.md"
remove_owned_link "$STACK_REPO/AGENTS.md" "$ANTIGRAVITY_DIR/AGENTS.md" "$ANTIGRAVITY_BACKUP_DIR/AGENTS.md"
for config in "${T3_ANTIGRAVITY_CONFIGS[@]}"; do
  remove_owned_link "$STACK_REPO/AGENTS.md" "$config/AGENTS.md" "$BACKUP_DIR/t3-antigravity/$(basename -- "$(dirname -- "$config")")/AGENTS.md"
done
remove_owned_link "$STACK_REPO" "$ROOT_LINK" "$BACKUP_DIR/root"
remove_owned_link "$STACK_REPO/bin/brigade" "$BIN_LINK" "$BACKUP_DIR/bin/brigade"
remove_owned_link "$STACK_REPO/bin/5stack" "$LEGACY_BIN_LINK" "$BACKUP_DIR/bin/5stack"
remove_legacy_link "$STACK_REPO" "$LEGACY_ROOT_LINK" "$LEGACY_BACKUP_DIR/root"

if [[ ! -e "$LEGACY_BIN_LINK" && ! -L "$LEGACY_BIN_LINK" && ( -e "$LEGACY_BACKUP_DIR/bin/5stack" || -L "$LEGACY_BACKUP_DIR/bin/5stack" ) ]]; then
  say_action "Restore $LEGACY_BACKUP_DIR/bin/5stack -> $LEGACY_BIN_LINK"
  if ((!DRY_RUN)); then
    mkdir -p -- "$BIN_DIR"
    mv -- "$LEGACY_BACKUP_DIR/bin/5stack" "$LEGACY_BIN_LINK"
  fi
fi

if [[ ! -e "$AGENTS_DIR/AGENTS.md" && ! -L "$AGENTS_DIR/AGENTS.md" && ( -e "$LEGACY_BACKUP_DIR/AGENTS.md" || -L "$LEGACY_BACKUP_DIR/AGENTS.md" ) ]]; then
  say_action "Restore $LEGACY_BACKUP_DIR/AGENTS.md -> $AGENTS_DIR/AGENTS.md"
  if ((!DRY_RUN)); then
    mkdir -p -- "$AGENTS_DIR"
    mv -- "$LEGACY_BACKUP_DIR/AGENTS.md" "$AGENTS_DIR/AGENTS.md"
  fi
fi

echo "Brigade-owned links removed. Non-Brigade paths were left unchanged."
