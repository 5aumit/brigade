#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
STACK_REPO=$(dirname -- "$SCRIPT_DIR")
AGENTS_DIR="${HOME}/.agents"
CLAUDE_DIR="${HOME}/.claude"
ANTIGRAVITY_DIR="${HOME}/.gemini/config"
BIN_DIR="${HOME}/.local/bin"
DRY_RUN=0
ASSUME_YES=0

usage() {
  echo "Usage: $0 [--dry-run] [--yes] [--agents-dir PATH] [--claude-dir PATH] [--antigravity-dir PATH] [--bin-dir PATH]"
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -y|--yes)
      ASSUME_YES=1
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

git -C "$STACK_REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Not a Git repository: $STACK_REPO" >&2; exit 1; }
[[ -f "$STACK_REPO/AGENTS.md" ]] || { echo "Missing $STACK_REPO/AGENTS.md" >&2; exit 1; }

BACKUP_DIR="$AGENTS_DIR/brigade-backups/v1"
CLAUDE_BACKUP_DIR="$CLAUDE_DIR/brigade-backups/v1"
ANTIGRAVITY_BACKUP_DIR="$ANTIGRAVITY_DIR/brigade-backups/v1"
LEGACY_BACKUP_DIR="$AGENTS_DIR/5stack-backups/v1"
SKILLS_DIR="$AGENTS_DIR/skills"
CLAUDE_SKILLS_DIR="$CLAUDE_DIR/skills"
ANTIGRAVITY_SKILLS_DIR="$ANTIGRAVITY_DIR/skills"
ROOT_LINK="$AGENTS_DIR/brigade"
BIN_LINK="$BIN_DIR/brigade"
LEGACY_ROOT_LINK="$AGENTS_DIR/5stack"
LEGACY_BIN_LINK="$BIN_DIR/5stack"
mapfile -t OWNED_SKILLS < "$STACK_REPO/skills/owned.txt"
LEGACY_SKILLS=(5stack-setup feedback reflect give-5stack-feedback reflect-5stack review-5stack-feedback)

confirm_global_installation() {
  local target_path=$1 source_path=$2 backup_path=$3 resolved_source resolved_target response has_existing=0
  resolved_source=$(readlink -f -- "$source_path")

  if [[ -L "$target_path" ]]; then
    resolved_target=$(readlink -f -- "$target_path" 2>/dev/null || true)
    [[ "$resolved_target" == "$resolved_source" ]] && return
    has_existing=1
  elif [[ -e "$target_path" ]]; then
    has_existing=1
  fi

  if ((DRY_RUN || ASSUME_YES)); then
    return
  fi

  [[ -t 0 ]] || {
    echo "Refusing to install global instructions at $target_path without confirmation. Re-run interactively or pass --yes." >&2
    exit 1
  }

  if ((has_existing)); then
    echo "Brigade will replace $target_path with a symlink to $source_path."
    echo "The current path will be moved to $backup_path."
  else
    echo "Brigade will create $target_path as a symlink to $source_path."
  fi
  read -r -p "Continue? [y/N] " response
  [[ "$response" =~ ^[Yy]([Ee][Ss])?$ ]] || {
    echo "Installation cancelled."
    exit 1
  }
}

say_action() {
  if ((DRY_RUN)); then
    echo "DRY RUN: $*"
  else
    echo "$*"
  fi
}

confirm_global_installation "$AGENTS_DIR/AGENTS.md" "$STACK_REPO/AGENTS.md" "$BACKUP_DIR/AGENTS.md"
confirm_global_installation "$CLAUDE_DIR/CLAUDE.md" "$STACK_REPO/AGENTS.md" "$CLAUDE_BACKUP_DIR/CLAUDE.md"
confirm_global_installation "$ANTIGRAVITY_DIR/AGENTS.md" "$STACK_REPO/AGENTS.md" "$ANTIGRAVITY_BACKUP_DIR/AGENTS.md"

install_link() {
  local source_path=$1 target_path=$2 backup_path=$3 expected_source actual_target
  expected_source=$(readlink -m -- "$source_path")

  if [[ -L "$target_path" ]]; then
    actual_target=$(readlink -m -- "$target_path")
    if [[ "$actual_target" == "$expected_source" ]]; then
      echo "Already installed: $target_path"
      return
    fi
  fi

  if [[ -e "$target_path" || -L "$target_path" ]]; then
    if [[ -e "$backup_path" || -L "$backup_path" ]]; then
      echo "Refusing to overwrite existing backup: $backup_path" >&2
      exit 1
    fi
    say_action "Back up $target_path -> $backup_path"
    if ((!DRY_RUN)); then
      mkdir -p -- "$(dirname -- "$backup_path")"
      mv -- "$target_path" "$backup_path"
    fi
  fi

  say_action "Link $target_path -> $source_path"
  if ((!DRY_RUN)); then
    mkdir -p -- "$(dirname -- "$target_path")"
    ln -s -- "$source_path" "$target_path"
  fi
}

migrate_legacy_link() {
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

for skill in "${LEGACY_SKILLS[@]}"; do
  migrate_legacy_link \
    "$STACK_REPO/skills/$skill" \
    "$SKILLS_DIR/$skill" \
    "$LEGACY_BACKUP_DIR/skills/$skill"
done

install_link "$STACK_REPO" "$ROOT_LINK" "$BACKUP_DIR/root"
install_link "$STACK_REPO/AGENTS.md" "$AGENTS_DIR/AGENTS.md" "$BACKUP_DIR/AGENTS.md"
install_link "$STACK_REPO/AGENTS.md" "$CLAUDE_DIR/CLAUDE.md" "$CLAUDE_BACKUP_DIR/CLAUDE.md"
install_link "$STACK_REPO/AGENTS.md" "$ANTIGRAVITY_DIR/AGENTS.md" "$ANTIGRAVITY_BACKUP_DIR/AGENTS.md"
install_link "$STACK_REPO/bin/brigade" "$BIN_LINK" "$BACKUP_DIR/bin/brigade"
install_link "$STACK_REPO/bin/5stack" "$LEGACY_BIN_LINK" "$BACKUP_DIR/bin/5stack"

for skill in "${OWNED_SKILLS[@]}"; do
  [[ -f "$STACK_REPO/skills/$skill/SKILL.md" ]] || {
    echo "Missing owned skill: $skill" >&2
    exit 1
  }
  install_link \
    "$STACK_REPO/skills/$skill" \
    "$SKILLS_DIR/$skill" \
    "$BACKUP_DIR/skills/$skill"
done

for skill in "${OWNED_SKILLS[@]}"; do
  install_link \
    "$STACK_REPO/skills/$skill" \
    "$CLAUDE_SKILLS_DIR/$skill" \
    "$CLAUDE_BACKUP_DIR/skills/$skill"
  install_link \
    "$STACK_REPO/skills/$skill" \
    "$ANTIGRAVITY_SKILLS_DIR/$skill" \
    "$ANTIGRAVITY_BACKUP_DIR/skills/$skill"
done

migrate_legacy_link "$STACK_REPO" "$LEGACY_ROOT_LINK" "$LEGACY_BACKUP_DIR/root"

if ((DRY_RUN)); then
  echo "Brigade installation dry run complete."
else
  echo "Brigade installation complete. Start a fresh agent session to load it."
fi
echo "Backups, when needed: $BACKUP_DIR"
if [[ -d "$LEGACY_BACKUP_DIR" ]]; then
  echo "Retained legacy backups: $LEGACY_BACKUP_DIR"
fi
