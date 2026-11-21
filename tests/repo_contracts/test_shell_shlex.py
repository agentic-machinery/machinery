"""Shared shell preprocessing cases for executable substitution boundaries."""

from __future__ import annotations

import pytest

from agents_extensions.shared.hooks.shell_shlex import (
    ShellPreprocessLimit,
    _expose_backtick_bodies,
    collapse_line_continuations,
    drop_literal_cat_bodies,
    preprocess_shell_command,
    split_operator_run,
)


@pytest.mark.parametrize(
    "command",
    [
        "echo $(printf '%s' '# gh pr merge 5 --admin')",
        'echo $(printf "%s" "# gh pr merge 5 --admin")',
        "echo $(printf '%s' $# ${#var} a#b)",
        r"echo \`gh pr merge 5 --admin\`",
        "echo 'literal `gh pr merge 5 --admin`'",
    ],
)
def test_issue_9115_literal_hashes_and_backticks_are_preserved(command: str):
    assert preprocess_shell_command(command).splitlines()[0] == command
    assert "\ngh pr merge 5 --admin" not in preprocess_shell_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo $(echo foo # comment \\\ngh pr merge 5 --admin)",
        "echo `echo foo # comment \\\ngh pr merge 5 --admin`",
    ],
)
def test_issue_9115_comment_continuation_exposes_next_command(command: str):
    visible = preprocess_shell_command(command)
    assert "# comment" not in visible
    assert "\\\n" not in visible
    assert "gh pr merge 5 --admin" in visible


def test_issue_9115_nested_backtick_body_is_exposed():
    visible = preprocess_shell_command("echo $(echo `gh pr merge 5 --admin`)")
    assert visible == "echo $(echo $(gh pr merge 5 --admin))"


@pytest.mark.parametrize(
    "command, suffix",
    [
        ("echo `echo # `; tee AGENTS.md", "; tee AGENTS.md"),
        ("echo `echo # `; cat .env", "; cat .env"),
        (r"echo `echo # \` hidden `; tee AGENTS.md", "; tee AGENTS.md"),
        (r"echo `echo # \\`; tee AGENTS.md", "; tee AGENTS.md"),
        ("echo $(true )#; tee AGENTS.md", "#; tee AGENTS.md"),
    ],
)
def test_issue_9115_closing_substitution_keeps_following_command(command: str, suffix: str):
    assert collapse_line_continuations(command).endswith(suffix)
    assert preprocess_shell_command(command).endswith(suffix)


def test_issue_9115_escaped_nested_backticks_are_exposed():
    command = r"echo `echo \`gh pr merge 5 --admin\``"
    assert "$(gh pr merge 5 --admin)" in preprocess_shell_command(command)


def test_issue_9115_backtick_depth_limit_is_explicit():
    with pytest.raises(ShellPreprocessLimit, match="depth exceeded"):
        _expose_backtick_bodies("`gh pr merge 5 --admin`", depth=16)


def test_issue_9115_redirection_operators_stay_whole():
    assert split_operator_run("2>&1") == ["2>&1"]
    assert split_operator_run(">&") == [">&"]
    assert split_operator_run("<&") == ["<&"]
    assert split_operator_run("&>>") == ["&>>"]
    assert split_operator_run("|&") == ["|&"]
    assert split_operator_run(");>&") == [")", ";", ">&"]


def test_issue_9115_real_backtick_nesting_crosses_cap(monkeypatch):
    import agents_extensions.shared.hooks.shell_shlex as shared

    monkeypatch.setattr(shared, "_MAX_BACKTICK_DEPTH", 2)
    body = "cat .env"
    for _ in range(3):
        body = "`" + body.replace("\\", "\\\\").replace("`", r"\`") + "`"
    with pytest.raises(ShellPreprocessLimit, match="depth exceeded"):
        _expose_backtick_bodies("echo " + body)


def test_issue_9115_arithmetic_prefix_does_not_reset_backtick_cap():
    command = r"echo `echo \`cat .env\``"
    with pytest.raises(ShellPreprocessLimit, match="depth exceeded"):
        _expose_backtick_bodies("$(()) " + command, depth=15)


_MERGE = "gh pr merge 5 --admin"
_PROSE = "Use `gh pr merge` after review; $(gh pr merge 5) ${X} $'\\x41'."


_CAT_FORM_ROWS = [
        # The certain form: setup items, then a top-level `cat` with a quoted delimiter.
        (f"cat > /tmp/b.md <<'EOF'\n{_PROSE}\nEOF", "cat > /tmp/b.md <<'EOF'\nEOF"),
        (
            f"A=/tmp/a; mkdir -p \"$A\" && cd /tmp && cat > \"$A/b.md\" <<'EOF'\n{_PROSE}\nEOF\n{_MERGE}",
            f"A=/tmp/a; mkdir -p \"$A\" && cd /tmp && cat > \"$A/b.md\" <<'EOF'\nEOF\n{_MERGE}",
        ),
        (f'cat <<-"EOF" >>${{T}}/b\n\t{_PROSE}\n\tEOF', 'cat <<-"EOF" >>${T}/b\n\tEOF'),
        (f"cat >'b.md'<<'A'\n{_PROSE}\nA\n\ncat <<'B'\n{_MERGE}\nB", "cat >'b.md'<<'A'\nA\n\ncat <<'B'\nB"),
        # The head ends at the first other command; a later body is read as before.
        (
            f"cat > a <<'A'\nx\nA\nalias cat=bash\ncat <<'B'\n{_MERGE}\nB",
            f"cat > a <<'A'\nA\nalias cat=bash\ncat <<'B'\n{_MERGE}\nB",
        ),
        # Every other construct leaves the command unchanged (None).
        (f"X=$(case y in y) echo;; esac); cat <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > \"$(if true; then echo f; fi)\" <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > $(for f in a; do echo $f; done) <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > `while false; do :; done` <<'EOF'\n{_MERGE}\nEOF", None),
        (f"time -p cat <<'EOF'\n{_MERGE}\nEOF", None),
        (f"coproc cat <<'EOF'\n{_MERGE}\nEOF", None),
        (f"f() {{ bash; }}; cat <<'EOF'\n{_MERGE}\nEOF", None),
        (f"function cat {{ bash; }}\ncat <<'EOF'\n{_MERGE}\nEOF", None),
        (f"(cat <<'EOF'\n{_MERGE}\nEOF\n) | bash", None),
        (f"{{\ncat > /dev/stdout <<'EOF'\n{_MERGE}\nEOF\n}} | bash", None),
        (f"cat <<'EOF' | bash\n{_MERGE}\nEOF", None),
        (f"cat > >(bash) <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat <<'EOF' > >(bash)\n{_MERGE}\nEOF", None),
        (f"cat > 'a b' <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > \"a b\" <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > $'x' <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > '$x' <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > >&2 <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat 2>/dev/null <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat >1<<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > /dev/fd/3 <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat >> \"/de\"v/tcp/127.0.0.1/9 <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > /proc/self/fd/1 <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cd / && cat > dev/fd/3 <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cd /dev && cat > fd/3 <<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat >>12<<-'EOF'\n{_MERGE}\nEOF", None),
        (f"cat <<<'x'\n{_MERGE}", None),
        (f"cat <<'A' <<'B'\nx\nA\n{_MERGE}\nB", None),
        (f"cat > f <<EOF\n$({_MERGE})\nEOF", None),
        (f"cat > f <<E'OF'\n{_MERGE}\nEOF", None),
        (f"cat > f <<\\EOF\n{_MERGE}\nEOF", None),
        (f"cat <<$'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > f \\\n<<'EOF'\n{_MERGE}\nEOF", None),
        (f"cat > f <<'EOF' \\\n\n{_MERGE}\nEOF", None),
        (f"cat > f <<'EOF'\n{_MERGE}", None),
        (f"cat > f <<'EOF'\r\n{_MERGE}\r\nEOF\r", None),
        (f"cat > f <<'EOF' # it's\n{_MERGE}\nEOF", None),
        (f"cat > f <<'EOF' && bash f\n{_MERGE}\nEOF", None),
        (f"PATH=/tmp/evil:$PATH; cat <<'EOF'\n{_MERGE}\nEOF", None),
        (f"sleep 1 & cat > f <<'EOF'\n{_MERGE}\nEOF", None),
        (f"\"cat\" <<'EOF'\n{_MERGE}\nEOF", None),
        (f"bash <<'EOF'\n{_MERGE}\nEOF", None),
        (f"python3 - <<'EOF'\nimport os; os.system('{_MERGE}')\nEOF", None),
]
# Rows whose answer depends on the directories the command may start in.
_START = ("/tmp/work",)
_DIRECTORY_ROWS = [
    (f"cat > fd/3 <<'EOF'\n{_MERGE}\nEOF", ("/dev",), None),
    (f"cat > fd/1 <<'EOF'\n{_MERGE}\nEOF", ("/tmp", "/proc/self"), None),
    (f"cd /nonexistent; cat > fd/3 <<'EOF'\n{_MERGE}\nEOF", ("/dev",), None),
    (f"cat > n.md <<'EOF'\n{_MERGE}\nEOF", (), None),
    (f"cat > n.md <<'EOF'\n{_MERGE}\nEOF", ("relative/dir",), None),
    (f"cd x && cat > n.md <<'EOF'\n{_MERGE}\nEOF", _START, None),
    (f"cd ~ && cat > n.md <<'EOF'\n{_MERGE}\nEOF", _START, None),
    (f"cd \"$W\" && cat > n.md <<'EOF'\n{_MERGE}\nEOF", _START, None),
    (f"cd /tmp && cat > n.md <<'EOF'\n{_MERGE}\nEOF", ("/home/x",), "cd /tmp && cat > n.md <<'EOF'\nEOF"),
    (f"cat > \"$A/n.md\" <<'EOF'\n{_MERGE}\nEOF", (), "cat > \"$A/n.md\" <<'EOF'\nEOF"),
]


@pytest.mark.parametrize(
    "command,directories,expected",
    [(command, _START, expected) for command, expected in _CAT_FORM_ROWS] + _DIRECTORY_ROWS,
)
def test_c6323_body_dropped_only_in_the_certain_cat_form(
    command: str, directories: tuple[str, ...], expected: str | None
):
    """Defect class: a here-document body dropped outside the certain form (C-6323).

    Only a quoted-delimiter body read by a top-level `cat` after setup items is
    data; any other shape stays as the guards read it before, so no command
    Bash could run from the text is hidden.
    """
    assert drop_literal_cat_bodies(command, directories) == (command if expected is None else expected)
