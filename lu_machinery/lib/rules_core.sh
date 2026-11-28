#!/usr/bin/env bash
# Rules core for launchers. Reads the core from this release through the
# sibling rules_core.py (offline; never the Monitor API) and hands provider
# adapters the text in the form their harness accepts:
#   claude-code  one --append-system-prompt: the core, then a caller's one
#                addition (launcher_claude_system_prompt; Claude keeps the last)
#   codex        one -c developer_instructions: the core, then a caller's one
#                setting (launcher_codex_developer_instructions; Codex keeps the last)
#   grok         --rules "$LC_RULES_CORE"
#   agy, cursor-agent, opencode: the core leads the initial prompt
# Fail-closed: a missing or unreadable core, seat or loader refuses the launch
# (rules_core_load returns 1) before any adapter check or side effect runs.

# The rules-core loader: this file's sibling, selected once from this file's
# unresolved location (design-v1 amendment 1), whatever the machinery package
# directory is called. Fleet sources belong to this machinery release;
# product selection belongs to product_root(), including LU_PRODUCT_ROOT.
_rules_core_lib_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || _rules_core_lib_dir=''
RULES_CORE_LOADER="${_rules_core_lib_dir:+$_rules_core_lib_dir/rules_core.py}"
# The loader runs as a module of this tree through the shared bootstrap (#100 R68).
# shellcheck source=scripts/lib/project_interpreter.sh
[ -z "$_rules_core_lib_dir" ] || source "$_rules_core_lib_dir/project_interpreter.sh"
unset _rules_core_lib_dir

# rules_core_refuse REASON — the refusal line for a launch that cannot load the core.
rules_core_refuse() {
  printf 'Error: refusing to launch: rules core unavailable (%s).\n' "$*" >&2
}

# rules_core_load PYTHON LANE PROVIDER
# Sets LC_RULES_SEAT, LC_RULES_CORE, LC_RULES_CORE_BYTES and exports LU_RULES_SEAT
# so dispatch.py and ACP calls made from the session inherit the seat. Returns 1,
# naming the path, when the core cannot be loaded.
rules_core_load() {
  local py="$1" lane="${2:-}" provider="${3:-claude}"
  local script="$RULES_CORE_LOADER"
  local -a lane_args=()
  LC_RULES_SEAT=""
  LC_RULES_CORE=""
  LC_RULES_CORE_BYTES=0
  if [ ! -x "$py" ] || [ ! -f "$script" ]; then
    rules_core_refuse "loader unavailable: $script"
    return 1
  fi
  if [ -n "$lane" ]; then
    lane_args=(--lane "$lane" --provider "$provider")
  fi
  if ! LC_RULES_SEAT="$(project_python_run "$py" -m "$PROJECT_PACKAGE.lib.rules_core" ${lane_args[@]+"${lane_args[@]}"} --format seat)"; then
    LC_RULES_SEAT=""
    rules_core_refuse "seat resolution failed: $script"
    return 1
  fi
  if ! LC_RULES_CORE="$(project_python_run "$py" -m "$PROJECT_PACKAGE.lib.rules_core" --seat "$LC_RULES_SEAT" --format block)"; then
    LC_RULES_CORE=""
    rules_core_refuse "seat $LC_RULES_SEAT needs readable fleet core.md" \
      "and, for content, product agents_extensions/shared/rules/core-curriculum.md"
    return 1
  fi
  # shellcheck disable=SC2034  # read by launcher_core.sh (dry-run report, argv placeholder)
  LC_RULES_CORE_BYTES="$(printf '%s' "$LC_RULES_CORE" | wc -c | tr -d ' ')"
  export LU_RULES_SEAT="$LC_RULES_SEAT"
}

# rules_core_prefix TEXT — the core, a blank line, then TEXT (the ack when empty).
rules_core_prefix() {
  local text="${1:-}"
  if [ -z "$text" ]; then
    text="The rules core above is loaded for this session. Reply only 'Rules core loaded.' and wait for instructions."
  fi
  if [ -z "${LC_RULES_CORE:-}" ]; then
    printf '%s' "$text"
    return 0
  fi
  printf '%s\n\n%s' "$LC_RULES_CORE" "$text"
}

# rules_core_toml PYTHON — the framed core as one TOML basic string.
rules_core_toml() {
  project_python_run "$1" -m "$PROJECT_PACKAGE.lib.rules_core" --seat "$LC_RULES_SEAT" --format toml
}

# rules_core_classify_codex_payload PYTHON PAYLOAD — classify a Codex -c/--config
# payload using runtime_context.classify_codex_config_payload (Codex's Unicode
# White_Space trimming) and print one of: setting, missing_value, subkey, other.
rules_core_classify_codex_payload() {
  local py="$1" payload="$2"
  printf '%s' "$payload" | project_python_run "$py" -c '
import importlib, sys

classify = importlib.import_module(f"{sys.argv[1]}.dispatcher.runtime_context").classify_codex_config_payload
res = classify(sys.stdin.read())
sys.stdout.write("other\n" if res is None else f"{res[0]}\n")
' "$PROJECT_PACKAGE"
}

# rules_core_merge_toml PYTHON RAW_VALUE — decode RAW_VALUE through the shared
# runtime_context.decode_codex_config_string decoder and print two lines: the
# framed core as a TOML basic string, then "<core>\n\n<decoded>" as one TOML
# basic string. Exits 3 when RAW_VALUE is a valid TOML non-string so callers
# keep main's argv and let Codex reject the invalid type unchanged.
rules_core_merge_toml() {
  local py="$1" raw_value="$2"
  printf '%s\0%s' "${LC_RULES_CORE:-}" "$raw_value" | project_python_run "$py" -c '
import importlib, sys

toml_basic_string = importlib.import_module(f"{sys.argv[1]}.lib.rules_core").toml_basic_string
decode = importlib.import_module(f"{sys.argv[1]}.dispatcher.runtime_context").decode_codex_config_string
core, raw = sys.stdin.read().split("\0", 1)
decoded = decode(raw)
if decoded is None:
    raise SystemExit(3)
merged = f"{core}\n\n{decoded}" if core else decoded
sys.stdout.write(toml_basic_string(core) + "\n" + toml_basic_string(merged) + "\n")
' "$PROJECT_PACKAGE"
}
