#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_package="${1:-$repo_root}"
skills_cli_version="${SKILLS_CLI_VERSION:-1.5.23}"
probe_root="$(mktemp -d)"

cleanup() {
  case "$probe_root" in
    "${TMPDIR:-/tmp}"/*|/tmp/*|/var/folders/*/T/*) rm -rf -- "$probe_root" ;;
    *) printf 'Refusing to remove unexpected probe path: %s\n' "$probe_root" >&2 ;;
  esac
}
trap cleanup EXIT

probe_home="$probe_root/home"
probe_project="$probe_root/project with spaces"
probe_cache="$probe_root/npm-cache"
mkdir -p "$probe_home" "$probe_project" "$probe_cache"
: > "$probe_root/npmrc"

required_files=(
  "SKILL.md"
  "README.md"
  "LICENSE"
  "requirements.txt"
  "scripts/fetch_transcript.py"
  "scripts/fetch_media.py"
  "scripts/browser_media_links.js"
  "assets/banner.png"
  "agents/openai.yaml"
)

# Do not feed local worktrees, virtual environments, or private files to the installer.
if [[ -d "$source_package" ]]; then
  package_root="$probe_root/package"
  for relative_path in "${required_files[@]}"; do
    mkdir -p "$package_root/$(dirname "$relative_path")"
    cp "$source_package/$relative_path" "$package_root/$relative_path"
  done
  source_package="$package_root"
fi

(
  cd "$probe_project"
  HOME="$probe_home" \
  XDG_CONFIG_HOME="$probe_home/.config" \
  XDG_CACHE_HOME="$probe_home/.cache" \
  npm_config_cache="$probe_cache" \
  npm_config_userconfig="$probe_root/npmrc" \
  DO_NOT_TRACK=1 \
    npx --yes "skills@$skills_cli_version" add "$source_package" \
      --skill youtube-fetcher \
      --agent codex \
      --copy \
      --yes
)

installed_skill="$probe_project/.agents/skills/youtube-fetcher"
for relative_path in "${required_files[@]}"; do
  if [[ ! -f "$installed_skill/$relative_path" ]]; then
    printf 'Missing installed file: %s\n' "$relative_path" >&2
    exit 1
  fi
  printf 'verified %s\n' "$relative_path"
done

probe_python="${YOUTUBE_FETCHER_PYTHON:-python3}"
"$probe_python" "$installed_skill/scripts/fetch_transcript.py" --check-deps
"$probe_python" "$installed_skill/scripts/fetch_transcript.py" --help > "$probe_root/help.txt"
"$probe_python" "$installed_skill/scripts/fetch_media.py" --help > "$probe_root/media-help.txt"

# Exercise the copied script from an unrelated cwd, including offline file protection.
(
  cd "$probe_root"
  "$probe_python" - "$installed_skill" <<'PY'
import importlib.util
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("installed_fetcher", Path(sys.argv[1]) / "scripts/fetch_transcript.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory() as tmp:
    dest = Path(tmp) / "my annotated note.md"
    dest.write_text("Keep my annotations", encoding="utf-8")
    with patch.object(module, "check_dependencies", side_effect=AssertionError("Network/dependencies must not be needed")):
        assert module.main(["--output", str(dest), "--", "dQw4w9WgXcQ"]) == 3
    assert dest.read_text(encoding="utf-8") == "Keep my annotations"
print("Copied skill preserves an existing note offline from a path containing spaces.")
PY
)
printf 'Isolated install verified with skills CLI %s.\n' "$skills_cli_version"
