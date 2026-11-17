"""Catch guard bypass by unusual characters or traversal.

The existing character gap is quarantined per exact minimal input, never by
xfailing an entire generated property. Each strict xfail becomes an XPASS when
that character is rejected and must then be removed by the guard's owner.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lu_machinery.path_safety import safe_join

COMPONENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=16)
# C0/C1 controls, Unicode bidi controls, and Unicode line/paragraph separators.
UNSAFE_CHARACTERS = tuple(dict.fromkeys(
    [chr(n) for n in (*range(32), *range(127, 160))]
    + list("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u2028\u2029")
))


@pytest.mark.parametrize("character", [
    pytest.param(c, id=ascii(c), marks=() if c == "\0" else pytest.mark.xfail(
        strict=True, reason=f"Pre-existing safe_join character bypass: {c!a}", raises=pytest.fail.Exception,
    ))
    for c in UNSAFE_CHARACTERS
])
def test_unusual_character_bypass_is_refused(character: str) -> None:
    with TemporaryDirectory() as directory, pytest.raises(ValueError):
        safe_join(Path(directory), character)


@given(prefix=COMPONENT, suffix=COMPONENT)
def test_embedded_nul_is_refused(prefix: str, suffix: str) -> None:
    with TemporaryDirectory() as directory, pytest.raises(ValueError):
        safe_join(Path(directory), prefix + "\0" + suffix)


@given(parts=st.lists(st.one_of(COMPONENT, st.sampled_from(["..", ".", "", "../x", "/x", "a:b", "a\\b"])), min_size=1, max_size=6))
def test_every_accepted_path_resolves_under_root(parts: list[str]) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        try:
            accepted = safe_join(root, *parts)
        except ValueError:
            return
        assert accepted.resolve().is_relative_to(root)
        assert not any(parent.is_symlink() for parent in (accepted, *accepted.parents))


@given(parts=st.lists(COMPONENT, min_size=1, max_size=5))
def test_normalized_safe_paths_are_accepted(parts: list[str]) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        assert safe_join(root, "./" + "//".join(parts)).resolve() == root.joinpath(*parts)


@given(name=COMPONENT, tail=st.lists(COMPONENT, max_size=3))
def test_traversal_and_symlink_escape_are_refused(name: str, tail: list[str]) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory) / "root"
        root.mkdir()
        outside = Path(directory) / "outside"
        outside.mkdir()
        (root / name).symlink_to(outside, target_is_directory=True)
        for parts in (("..", *tail), (name, *tail)):
            with pytest.raises(ValueError):
                safe_join(root, *parts)
