#!/usr/bin/env bash
# Run macOS-authored filesystem guardrails in Linux before opening a PR.
# Add a test file here only when it verifies OS filesystem semantics; keep this
# list focused so this remains a fast local parity probe rather than a suite.
set -euo pipefail

script_dir="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly script_dir
source "$script_dir/../lib/project_interpreter.sh"
repo_root="$(project_product_root_resolve)" || exit 1
readonly repo_root

pins="$PROJECT_CODE_ROOT/.github/workflows/pins.txt"
case "$PROJECT_CODE_ROOT" in
  */lib/python*/site-packages|*/lib64/python*/site-packages)
    pins="${PROJECT_CODE_ROOT%/*/*/*}/share/lu-machinery/ci/pins.txt"
    ;;
esac
if [[ ! -f "$pins" || ! -r "$pins" ]]; then
  echo "linux_fs_parity: cannot read machinery pins at $pins" >&2
  exit 1
fi

if [[ ${LINUX_FS_PARITY_IMAGE+x} ]]; then
  image="$LINUX_FS_PARITY_IMAGE"
else
  mapfile -t images < <(sed -n 's/^LINUX_FS_PARITY_IMAGE=//p' "$pins")
  if [[ ${#images[@]} -ne 1 ]]; then
    echo "linux_fs_parity: machinery pins must contain one LINUX_FS_PARITY_IMAGE" >&2
    exit 1
  fi
  image="${images[0]}"
fi
if ! [[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]; then
  echo "linux_fs_parity: image must be digest-qualified as name@sha256:<64 lowercase hex characters>" >&2
  exit 1
fi
readonly image

# The container has no uv. It installs the pins.txt uv release, checks that
# archive, and exports pytest from the mounted uv.lock. The prune list only
# keeps the probe small: a core package that is not listed is still installed,
# and only with lock hashes.
# Stream machinery's pins through stdin before running any product tests.
docker run --rm --init --interactive \
  --mount "type=bind,src=${repo_root},dst=/workspace,readonly" \
  --workdir /workspace \
  --env PIP_DISABLE_PIP_VERSION_CHECK=1 \
  --env PYTHONDONTWRITEBYTECODE=1 \
  "${image}" \
  bash -lc "$(cat <<'INNER'
    set -euo pipefail
    command -v git >/dev/null
    if [ "$(uname -m)" != x86_64 ]; then
      echo "linux_fs_parity: the uv pin is linux x86_64 only (got $(uname -m))" >&2
      exit 1
    fi
    if IFS= read -r -d '' pins; then
      echo "linux_fs_parity: machinery pins must be a text file" >&2
      exit 1
    fi
    mapfile -t uv_versions < <(sed -n "s/^UV_VERSION=//p" <<< "$pins")
    mapfile -t uv_shas < <(sed -n "s/^UV_LINUX_X86_64_SHA256=//p" <<< "$pins")
    if [ "${#uv_versions[@]}" -ne 1 ] || [ "${#uv_shas[@]}" -ne 1 ]; then
      echo "linux_fs_parity: machinery pins must contain one UV_VERSION and one UV_LINUX_X86_64_SHA256" >&2
      exit 1
    fi
    uv_version="${uv_versions[0]}"
    uv_sha="${uv_shas[0]}"
    if ! [[ "$uv_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      echo "linux_fs_parity: UV_VERSION is not a release number" >&2
      exit 1
    fi
    if ! [[ "$uv_sha" =~ ^[0-9a-f]{64}$ ]]; then
      echo "linux_fs_parity: UV_LINUX_X86_64_SHA256 is not 64 hex characters" >&2
      exit 1
    fi
    work="$(mktemp -d)"
    python - "$uv_version" "$uv_sha" "$work/uv.tar.gz" <<'PY'
import hashlib
import sys
import urllib.request

version, want, dest = sys.argv[1:]
url = (
    "https://github.com/astral-sh/uv/releases/download/"
    f"{version}/uv-x86_64-unknown-linux-gnu.tar.gz"
)
urllib.request.urlretrieve(url, dest)
got = hashlib.sha256(open(dest, "rb").read()).hexdigest()
if got != want:
    raise SystemExit(f"linux_fs_parity: uv archive sha256 {got} != {want}")
PY
    tar -xzf "$work/uv.tar.gz" -C "$work"
    uv="$work/uv-x86_64-unknown-linux-gnu/uv"
    if [ ! -x "$uv" ]; then
      echo "linux_fs_parity: pinned uv archive has no uv binary" >&2
      exit 1
    fi
    prunes=(
      fastapi filelock graphql-core ijson jsonschema markdown-it-py
      pre-commit prometheus-client psutil pydantic pyjwt pyyaml
      referencing starlette coverage pytest-xdist execnet
    )
    prune_args=()
    for package in "${prunes[@]}"; do
      prune_args+=(--prune "$package")
    done
    "$uv" export --locked --offline --quiet --format requirements.txt \
      --no-emit-project --no-default-groups --extra pytest --no-header \
      --no-annotate "${prune_args[@]}" -o "$work/pytest.txt"
    if ! grep -q '^pytest==' "$work/pytest.txt"; then
      echo "linux_fs_parity: uv export did not pin pytest" >&2
      exit 1
    fi
    python -m pip install --quiet --no-cache-dir --require-hashes \
      --only-binary=:all: --no-deps -r "$work/pytest.txt"
    python -m pytest -q -p no:cacheprovider \
      tests/test_worktree_containment.py \
      tests/test_guard_primary_checkout_write.py \
      tests/test_hooks_executable.py \
      tests/test_deploy_script_idempotency.py::test_agent_manifest_rejects_symlinked_intermediate_component \
      tests/test_deploy_script_idempotency.py::test_agent_manifest_unlinks_symlink_leaf_without_following_target
INNER
)" < "$pins"
