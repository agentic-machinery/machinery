"""Catch guard bypass by unusual characters or traversal."""

import unicodedata
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lu_machinery.path_safety import safe_join, trusted_join

COMPONENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=16)
# Every code point in the refused class, derived from the Unicode database so a
# guard narrowed to a hand-written subset fails: controls (Cc), format
# characters such as bidi controls and zero-width characters (Cf), and
# line/paragraph separators (Zl, Zp).
UNSAFE_CHARACTERS = tuple(
    chr(n) for n in range(0x110000) if unicodedata.category(chr(n)) in {"Cc", "Cf", "Zl", "Zp"}
)
# Ukrainian and other legitimate non-ASCII names: Cyrillic, the U+02BC
# apostrophe, the U+0301 combining stress mark, NBSP and an emoji.
LEGITIMATE_COMPONENTS = ("урок-01", "м\u02bcята", "на\u0301голос", "a\u00a0b", "emoji-\U0001f600")


@pytest.mark.parametrize("character", [pytest.param(c, id=ascii(c)) for c in UNSAFE_CHARACTERS])
def test_unusual_character_bypass_is_refused(character: str) -> None:
    with TemporaryDirectory() as directory, pytest.raises(ValueError):
        safe_join(Path(directory), character)


@pytest.mark.parametrize("join", [safe_join, trusted_join])
@pytest.mark.parametrize("component", LEGITIMATE_COMPONENTS, ids=ascii)
def test_legitimate_non_ascii_component_is_accepted(join, component: str) -> None:
    """Catch an over-broad character refusal that would reject Ukrainian paths."""
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        assert join(root, component) == root / component


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
