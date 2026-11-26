"""The files a dispatch owns because a commit gate regenerates them from what it owns (#12169).

A gate that rewrites a generated file whenever its input changes blocks a worker that owns the
input but not the file. The dispatcher adds the file only for tasks targeting the gate's repository.
The task record carries the expanded paths, so hooks and settlement need no rule of their own.
"""

from collections.abc import Iterable
from pathlib import PurePosixPath

# Within each fleet repository key, map the input tree a commit gate watches to the file it
# regenerates. Repositories without a matching key receive no generated paths.
GENERATED_OUTPUTS = {
    "public": {
        "scripts/lexicon": "site/src/data/lexicon-manifest.fingerprint.json",  # atlas-lexicon-fingerprint-drift
        "site/src/components": "docs/lesson-schema.yaml",  # lesson-schema-drift
    },
}


def _literal_root(raw: str) -> str | None:
    """The owned path's literal part before any glob, or None when it has none."""
    path = PurePosixPath(raw.strip().replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        return None
    literal = []
    for part in path.parts:
        if any(char in part for char in "*?["):
            break
        literal.append(part)
    return "/".join(literal) or None


def with_generated_outputs(owned_paths: Iterable[str], repo_key: str | None) -> tuple[list[str], list[str]]:
    """Return the expanded owned paths and the files added for the target repository's gates.

    An owned path needs a file when its literal part is the gate's input tree, inside it or above it.
    Unknown repository keys add nothing. A file the owned paths already cover is not added again.
    """
    from lu_machinery.guardrails.delegate_ownership import path_is_owned

    owned = list(owned_paths)
    roots = [root for root in map(_literal_root, owned) if root]
    gates = GENERATED_OUTPUTS.get(repo_key, {}) if isinstance(repo_key, str) else {}
    added = [output for prefix, output in gates.items()
             if not path_is_owned(output, owned)
             and any(root == prefix or root.startswith(prefix + "/") or prefix.startswith(root + "/") for root in roots)]
    return owned + added, added
