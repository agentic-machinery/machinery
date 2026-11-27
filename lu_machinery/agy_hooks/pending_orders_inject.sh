#!/usr/bin/env bash
# Read-only AGY PreInvocation announcement. Never persist stdin or probe output.
# The product, shared-state anchor and interpreter come from the one hook rule,
# project_host_context (lib/project_interpreter.sh), before the child
# starts. Without that context the hook denies with the bootstrap diagnostic;
# "unknown" is kept for a child that ran and could not read its stores.
fleet_module=lu_machinery.fleet_comms
fallback='{"injectSteps":[{"ephemeralMessage":"PENDING ORDERS: unknown; authority: project interpreter -I -m '"$fleet_module"' deliveries --help"}]}'
bootstrap_failed() {
    cat >/dev/null 2>&1
    if [ "$#" -gt 0 ]; then
      printf 'HOOK-BOOTSTRAP-FAILED: [hook_unavailable] %s\n' "$1" >&2
  fi
    printf '%s\n' '{"decision":"deny","reason":"HOOK-BOOTSTRAP-FAILED: hook context unavailable."}'
    exit 0
}
code=$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd) || bootstrap_failed "installed hook directory is unavailable."
# shellcheck disable=SC1091
source "$code/lu_machinery/lib/project_interpreter.sh" || bootstrap_failed "installed hook code is unavailable."
project_host_context || bootstrap_failed
py="$LU_PYTHON"
# The child drains stdin; on early import/exec failure the parent drains the rest.
project_python_command "$py" || bootstrap_failed "interpreter command could not be constructed."
if output=$(cd "$LU_PRODUCT_ROOT" && "${PROJECT_PYTHON_COMMAND[@]}" -m lu_machinery.inbox_pending --agy-hook 2>/dev/null); then
    child_ok=1
else
    child_ok=0
fi
cat >/dev/null
# The child's own context refusal stays a refusal, never an "unknown" announcement.
if [ "$child_ok" = 1 ] && [ "$output" = '{"decision":"deny","reason":"HOOK-BOOTSTRAP-FAILED: hook context unavailable."}' ]; then
    bootstrap_failed "installed hook code is unavailable."
fi
if [ "$child_ok" = 1 ] && validated=$(printf '%s' "$output" | "$py" -I -c '
import json, sys
value = json.load(sys.stdin)
if value != {}:
    assert set(value) == {"injectSteps"}
    steps = value["injectSteps"]
    assert isinstance(steps, list) and len(steps) == 1
    assert set(steps[0]) == {"ephemeralMessage"}
    line = steps[0]["ephemeralMessage"]
    assert isinstance(line, str) and line.startswith("PENDING ORDERS: ")
    assert len(line) <= 200 and line.isascii() and all(32 <= ord(c) < 127 for c in line)
print(json.dumps(value, separators=(",", ":")))
' 2>/dev/null); then
    printf '%s\n' "$validated"
else
    printf '%s\n' "$fallback"
fi
exit 0
