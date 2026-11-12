#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
STACK_REPO=$(dirname -- "$SCRIPT_DIR")
mapfile -t OWNED_SKILLS < "$STACK_REPO/skills/owned.txt"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

for required in \
  AGENTS.md \
  README.md \
  bin/brigade \
  bin/5stack \
  lib/worker_control.py \
  references/trust-handoff.md \
  scripts/worker.py \
  evals/scenarios.md; do
  [[ -f "$STACK_REPO/$required" ]] || fail "missing $required"
done

skill_names=()
for skill in "${OWNED_SKILLS[@]}"; do
  skill_file="$STACK_REPO/skills/$skill/SKILL.md"
  [[ -f "$skill_file" ]] || fail "missing skill $skill"
  [[ "$(sed -n '1p' "$skill_file")" == "---" ]] || fail "$skill has no opening frontmatter delimiter"
  closing_line=$(awk 'NR > 1 && $0 == "---" { print NR; exit }' "$skill_file")
  [[ -n "$closing_line" ]] || fail "$skill has no closing frontmatter delimiter"
  declared=$(sed -n 's/^name: *//p' "$skill_file" | head -n 1)
  [[ "$declared" == "$skill" ]] || fail "$skill declares name '$declared'"
  [[ "$(sed -n 's/^description: *//p' "$skill_file" | wc -l)" -eq 1 ]] || fail "$skill needs one description"
  skill_names+=("$declared")
done

duplicates=$(printf '%s\n' "${skill_names[@]}" | sort | uniq -d)
[[ -z "$duplicates" ]] || fail "duplicate skill names: $duplicates"

for skill in give-brigade-feedback reflect-brigade review-brigade-feedback; do
  skill_file="$STACK_REPO/skills/$skill/SKILL.md"
  ui_file="$STACK_REPO/skills/$skill/agents/openai.yaml"
  grep -qx 'disable-model-invocation: true' "$skill_file" || fail "$skill must require explicit invocation"
  grep -qx '  allow_implicit_invocation: false' "$ui_file" || fail "$skill UI metadata must disable implicit invocation"
done

if command -v ruby >/dev/null; then
  ruby -ryaml -e '
    ARGV.each do |file|
      parts = File.read(file).split(/^---\s*$\n?/, 3)
      raise "invalid frontmatter: #{file}" unless parts.length == 3
      data = YAML.safe_load(parts[1])
      raise "invalid skill metadata: #{file}" unless data.is_a?(Hash) && data["name"] && data["description"]
    end
  ' "$STACK_REPO"/skills/*/SKILL.md || fail "skill YAML validation failed"
fi

if LC_ALL=C grep -RIl $'\342\200\224' \
  "$STACK_REPO/AGENTS.md" \
  "$STACK_REPO/README.md" \
  "$STACK_REPO/references" \
  "$STACK_REPO/skills" \
  "$STACK_REPO/evals" >/dev/null; then
  fail "em dash character found"
fi

bash -n "$STACK_REPO/scripts/install.sh"
bash -n "$STACK_REPO/scripts/uninstall.sh"
bash -n "$STACK_REPO/scripts/check.sh"
bash -n "$STACK_REPO/bin/brigade"
bash -n "$STACK_REPO/bin/5stack"
python3 -m py_compile "$STACK_REPO/lib/worker_control.py" "$STACK_REPO/scripts/worker.py"
python3 -m unittest discover -s "$STACK_REPO/tests"

CHECK_TMP=$(mktemp -d)
case "$CHECK_TMP" in
  /tmp/*) ;;
  *) fail "unexpected temporary path: $CHECK_TMP" ;;
esac
trap 'rm -rf -- "$CHECK_TMP"' EXIT

TEST_AGENTS_DIR="$CHECK_TMP/upgrade-agents"
TEST_BIN_DIR="$CHECK_TMP/bin"

EMPTY_AGENTS_DIR="$CHECK_TMP/empty-agents"
FRESH_BIN_DIR="$CHECK_TMP/fresh-bin"
if bash "$STACK_REPO/scripts/install.sh" --agents-dir "$EMPTY_AGENTS_DIR" --bin-dir "$FRESH_BIN_DIR" >/dev/null 2>&1; then
  fail "installer created AGENTS.md without confirmation"
fi
[[ ! -e "$EMPTY_AGENTS_DIR/AGENTS.md" && ! -L "$EMPTY_AGENTS_DIR/AGENTS.md" ]] || fail "installer changed empty agents directory without confirmation"
bash "$STACK_REPO/scripts/install.sh" --yes --agents-dir "$EMPTY_AGENTS_DIR" --bin-dir "$FRESH_BIN_DIR" >/dev/null
[[ -L "$EMPTY_AGENTS_DIR/AGENTS.md" ]] || fail "installer did not create AGENTS.md with explicit approval"
[[ -L "$EMPTY_AGENTS_DIR/brigade" ]] || fail "installer did not link the Brigade repository"
[[ -L "$FRESH_BIN_DIR/brigade" ]] || fail "installer did not link the Brigade command"
[[ -L "$FRESH_BIN_DIR/5stack" ]] || fail "installer did not link the legacy 5stack command"
XDG_STATE_HOME="$CHECK_TMP/fresh-state" "$FRESH_BIN_DIR/brigade" worker list >/dev/null || fail "installed Brigade command did not run"
XDG_STATE_HOME="$CHECK_TMP/fresh-state" "$FRESH_BIN_DIR/5stack" worker list >/dev/null || fail "installed legacy 5stack command did not run"
mkdir -p "$CHECK_TMP/legacy-state/5stack"
printf '{"version": 1, "workers": [{"id": "w_legacy"}]}\n' > "$CHECK_TMP/legacy-state/5stack/workers.json"
XDG_STATE_HOME="$CHECK_TMP/legacy-state" "$FRESH_BIN_DIR/brigade" worker list --all --json | grep -q 'w_legacy' || fail "Brigade did not retain legacy worker history"

mkdir -p "$TEST_BIN_DIR" "$TEST_AGENTS_DIR/skills" "$TEST_AGENTS_DIR/5stack-backups/v1/skills" "$TEST_AGENTS_DIR/5stack-backups/v1/bin"
printf 'original global instructions\n' > "$TEST_AGENTS_DIR/5stack-backups/v1/AGENTS.md"
printf 'original old root\n' > "$TEST_AGENTS_DIR/5stack-backups/v1/root"
printf 'original feedback skill\n' > "$TEST_AGENTS_DIR/5stack-backups/v1/skills/give-5stack-feedback"
printf 'original 5stack command\n' > "$TEST_AGENTS_DIR/5stack-backups/v1/bin/5stack"
ln -s "$STACK_REPO/AGENTS.md" "$TEST_AGENTS_DIR/AGENTS.md"
ln -s "$STACK_REPO" "$TEST_AGENTS_DIR/5stack"
ln -s "$STACK_REPO/skills/give-5stack-feedback" "$TEST_AGENTS_DIR/skills/give-5stack-feedback"
ln -s "$STACK_REPO/bin/5stack" "$TEST_BIN_DIR/5stack"

bash "$STACK_REPO/scripts/install.sh" --agents-dir "$TEST_AGENTS_DIR" --bin-dir "$TEST_BIN_DIR" >/dev/null
[[ -L "$TEST_AGENTS_DIR/AGENTS.md" ]] || fail "installer did not link AGENTS.md"
[[ -L "$TEST_AGENTS_DIR/brigade" ]] || fail "installer did not link the Brigade repository"
[[ -f "$TEST_AGENTS_DIR/5stack" ]] || fail "installer did not restore legacy root backup"
grep -q 'original old root' "$TEST_AGENTS_DIR/5stack" || fail "installer restored wrong legacy root backup"
[[ -f "$TEST_AGENTS_DIR/skills/give-5stack-feedback" ]] || fail "installer did not restore legacy feedback backup"
grep -q 'original feedback skill' "$TEST_AGENTS_DIR/skills/give-5stack-feedback" || fail "installer restored wrong legacy feedback backup"
[[ -L "$TEST_BIN_DIR/brigade" ]] || fail "installer did not link the Brigade command"
[[ -L "$TEST_BIN_DIR/5stack" ]] || fail "installer did not link the 5stack command"
for skill in "${OWNED_SKILLS[@]}"; do
  [[ -L "$TEST_AGENTS_DIR/skills/$skill" ]] || fail "installer did not link $skill"
done

bash "$STACK_REPO/scripts/uninstall.sh" --agents-dir "$TEST_AGENTS_DIR" --bin-dir "$TEST_BIN_DIR" >/dev/null
[[ -f "$TEST_AGENTS_DIR/AGENTS.md" ]] || fail "uninstaller did not restore AGENTS.md"
[[ ! -e "$TEST_AGENTS_DIR/brigade" && ! -L "$TEST_AGENTS_DIR/brigade" ]] || fail "uninstaller left the Brigade repository link"
[[ -f "$TEST_AGENTS_DIR/5stack" ]] || fail "uninstaller removed restored legacy root"
[[ ! -e "$TEST_BIN_DIR/brigade" && ! -L "$TEST_BIN_DIR/brigade" ]] || fail "uninstaller left the Brigade command"
[[ -f "$TEST_BIN_DIR/5stack" ]] || fail "uninstaller did not restore legacy 5stack command"
for skill in "${OWNED_SKILLS[@]}"; do
  [[ ! -e "$TEST_AGENTS_DIR/skills/$skill" && ! -L "$TEST_AGENTS_DIR/skills/$skill" ]] || fail "uninstaller left $skill"
done
grep -q 'original global instructions' "$TEST_AGENTS_DIR/AGENTS.md" || fail "wrong AGENTS.md restored"
grep -q 'original feedback skill' "$TEST_AGENTS_DIR/skills/give-5stack-feedback" || fail "uninstaller changed restored legacy feedback backup"
grep -q 'original 5stack command' "$TEST_BIN_DIR/5stack" || fail "uninstaller restored wrong 5stack command backup"

echo "Brigade static and installer checks passed."
