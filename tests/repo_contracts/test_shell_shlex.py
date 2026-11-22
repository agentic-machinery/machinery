"""Shared shell preprocessing cases for executable substitution boundaries."""

from __future__ import annotations

import random
import subprocess

import pytest

from agents_extensions.shared.hooks.shell_shlex import (
    ShellPreprocessLimit,
    _expose_backtick_bodies,
    collapse_line_continuations,
    normalize_io_descriptors,
    preprocess_shell_command,
    split_operator_run,
    split_quote_preserving,
    strip_shell_comments,
    strip_skippable_heredoc_bodies,
)
from tests.helpers.product_tree import configured_roots as configured_roots


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


@pytest.mark.parametrize(
    "command, expected_live",
    [
        # Reported class: escaped quote inside $'...' followed by quoted space-hash
        # and a live command, with an unclosed quote on line 2.
        ("echo $'\\'' ' #' ; printf LIVE\n'", "printf LIVE"),
        ("echo $'a\\'b' ' #' ; printf LIVE\n'", "printf LIVE"),
        ("echo $'\\\\\\'' ' #' ; printf LIVE\n'", "printf LIVE"),
        ("echo $'\\c\\'' ' #' ; printf LIVE\n'", "printf LIVE"),
        # Line continuation joining $ and ' before the escaped quote.
        ("echo $\\\n'\\'' ' #' ; printf LIVE\n'", "printf LIVE"),
        ("echo $\\\n\\\n'\\'' ' #' ; printf LIVE\n'", "printf LIVE"),
        # Backslash-newline inside $'...' is literal in Bash (not a continuation).
        ("echo $'a\\\n' ' #' ; printf LIVE\n'", "printf LIVE"),
        ("echo $'a\\\n\\'b' ' #' ; printf LIVE", "printf LIVE"),
        # Inside $(...) and unquoted ${...}, ANSI-C quotes preserve live suffixes.
        ("echo $(printf %s $')\\'' ' #' ; printf LIVE)", "printf LIVE"),
        ('echo "$(printf %s $\'\\"\' \' #\' ; printf LIVE)"', "printf LIVE"),
        ("x=; echo ${x:-$'a}\\'' # not comment} ; printf LIVE", "printf LIVE"),
        # Adjacent hash after closed $'...' is word text, not a comment.
        ("echo $'a\\''#word ; printf LIVE", "printf LIVE"),
        # Backticks and double quotes inside $'...' are literal data.
        ("echo $'`printf NO`' ' #' ; printf LIVE", "printf LIVE"),
        ('echo $\'"\' " #" ; printf LIVE', "printf LIVE"),
        # Even runs of `$` lex as `$$` PID token(s) first, so the following `'` is a plain single quote.
        ("echo $$'\\'' #' ; printf LIVE", "printf LIVE"),
        ("echo $$$$'\\'' #' ; printf LIVE", "printf LIVE"),
        ("echo $\\\n$'\\'' #' ; printf LIVE", "printf LIVE"),
        ("echo $$\\\n'\\'' #' ; printf LIVE", "printf LIVE"),
        ('echo "$(echo $$\'\\\'\' #\' ; printf LIVE)"', "printf LIVE"),
        # Odd runs (`$$$'`) lex `$$` then ANSI-C `$'`.
        ("echo $$$'\\'' ' #' ; printf LIVE\n'", "printf LIVE"),
        # `$$(` and `$${` inside double quotes lex as PID `$$`, not substitutions.
        ('echo "$$(printf no)" " #" ; printf LIVE', "printf LIVE"),
        ('echo "$${x:-a}" " #" ; printf LIVE', "printf LIVE"),
    ],
)
def test_ansi_c_quoting_preserves_live_code_matching_bash(command: str, expected_live: str):
    """Defect class: ANSI-C escape quote-flip deleting live commands during preprocessing."""
    bash_out = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False, timeout=5).stdout
    assert "LIVE" in bash_out
    collapsed = collapse_line_continuations(command)
    stripped = strip_shell_comments(collapsed)
    exposed = _expose_backtick_bodies(stripped)
    visible = preprocess_shell_command(command)
    assert expected_live in collapsed
    assert expected_live in stripped
    assert expected_live in exposed
    assert expected_live in visible


@pytest.mark.parametrize(
    "command, expected_args",
    [
        ("printf '<%s>' $'a\\'b' ' #' # comment", ["<a'b>", "< #>"]),
        ("printf '<%s>' $'a\\\\' ' #' # comment", ["<a\\>", "< #>"]),
        ("printf '<%s>' $'a\\\nb' ' #' # comment", ["<a\\\nb>", "< #>"]),
        ("printf '<%s>' $\\\n'a\\'b' ' #' # comment", ["<a'b>", "< #>"]),
        ("printf '<%s>' $'\\c' ' #' # comment", ["<\\c>", "< #>"]),
        ("printf '<%s>' $''#tail # comment", ["<#tail>"]),
        ('printf "<%s>" "$\'a\\\'b\'" " #" # comment', ["<$'a\\'b'>", "< #>"]),
    ],
)
def test_ansi_c_comment_stripping_preserves_bash_word_values(command: str, expected_args: list[str]):
    """Defect class: preprocessing alters Bash word values when stripping comments or continuations."""
    raw_run = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True, timeout=5)
    processed = preprocess_shell_command(command)
    processed_run = subprocess.run(["bash", "-c", processed], capture_output=True, text=True, check=True, timeout=5)
    assert raw_run.stdout == "".join(expected_args)
    assert processed_run.stdout == raw_run.stdout
    assert "# comment" not in processed


def test_ansi_c_heredoc_io_and_split_helpers_track_escapes():
    """Defect class: ANSI-C escaped quote flipping heredoc, descriptor, or quote-preserving scans."""
    # Quoted <<EOF after $'\'' must not hide the following line as a heredoc body.
    heredoc_cmd = "echo $'\\'' '<<EOF'\nprintf LIVE\nEOF"
    assert "printf LIVE" in preprocess_shell_command(heredoc_cmd)
    assert "printf LIVE" in strip_skippable_heredoc_bodies(heredoc_cmd, opener_transform=strip_shell_comments)
    # Unquoted heredoc body substitution with $'\')' must not close $(...) early.
    body_subst = "cat <<EOF\n$(echo $'\\')' ; printf LIVE)\nEOF"
    assert "printf LIVE" in preprocess_shell_command(body_subst)
    # Multi-line ANSI-C string containing <<EOF must not skip later live lines.
    multiline_ansi = "echo $'line1\n# <<EOF\n'\nprintf LIVE\nEOF"
    assert "printf LIVE" in preprocess_shell_command(multiline_ansi)
    # IO descriptor normalization ignores redirects inside $'...' and sees those after $'\''.
    assert normalize_io_descriptors("echo $'\\'2>a' 2>b") == "echo $'\\'2>a' >b"
    # Quote-preserving split keeps \" inside "..." protected after $'\'', \$$'\'', and unquoted \'.
    tokens = split_quote_preserving('echo $\'\\\'\' "a\\"b"', punctuation_chars="();&|<>\n", whitespace=" \t")
    assert '"a\\"b"' in tokens
    escaped_dd = split_quote_preserving(
        'echo \\$$\'\\\'\' "a\\" " ; env ; echo \\"', punctuation_chars="();&|<>\n", whitespace=" \t"
    )
    assert "env" in escaped_dd
    escaped_sq = split_quote_preserving(
        'echo \\\' "a\\" " ; env ; echo \\"', punctuation_chars="();&|<>\n", whitespace=" \t"
    )
    assert "env" in escaped_sq


@pytest.mark.parametrize(
    "command",
    [
        # Backtick phase-1 unescaping changes backslash parity inside $'...' (\\ -> \, \$ -> $).
        r"echo `printf '%s' $'\\'' ' #' ; env`",
        r"echo `printf '%s' \$'\'' ' #' ; env`",
        "echo `printf '%s' $\\\n'\\'' ' #' ; env`",
        r"""echo "`printf '%s' $'\\'' ' #' ; env`" """,
        r"echo `echo $'a` ; env # '`",
        "echo `echo $'unclosed",
        r"echo `echo \`printf '%s' \\\$'\''\``",
        # Double-quoted parameter expansion extquote alters quote/brace parity in Bash 5.3.
        '''x=; echo "${x:-$'a}"'}" " #" ; env''',
        'x=; echo "$\\\n{x:-$\'a}"\'}" " #" ; env',
        # Unterminated ANSI-C quote inside shell expansion cannot close safely.
        "echo $(echo $'unclosed) ; env",
        "echo ${x:-$'unclosed} ; env",
    ],
)
def test_ansi_c_ambiguous_contexts_fail_closed_across_all_preprocessors(command: str):
    """Defect class: unmodeled ANSI-C contexts silently stripping code instead of refusing."""
    for fn in (collapse_line_continuations, strip_shell_comments, _expose_backtick_bodies, preprocess_shell_command):
        with pytest.raises(ShellPreprocessLimit):
            fn(command)


def test_ansi_c_unterminated_top_level_never_strips():
    """Defect class: unterminated top-level ANSI-C string stripping trailing text."""
    for command in ("tee $'", "echo $'abc # ; env", "echo $'abc\\\n; env", "echo $'abc\\"):
        assert collapse_line_continuations(command) == command
        assert strip_shell_comments(command) == command
        assert _expose_backtick_bodies(command) == command
        assert preprocess_shell_command(command) == command


def test_ansi_c_bash_oracle_fuzz_never_hides_live_code():
    """Defect class: divergence from real Bash quoting across mixed ANSI-C, quotes, comments and continuations."""
    rng = random.Random(20261122)
    marker = "ORACLE_LIVE_TOKEN_9917"
    ansi_pieces = (
        "$''",
        "$'a'",
        r"$'\''",
        r"$'\\'",
        r"$'\\\''",
        r"$'a\'b'",
        r"$'a # b'",
        r"$'a\' # b'",
        r"$'\"'",
        r"$'`echo no`'",
        "$'a\\\nb'",
        "$'a\\\n'",
        "$\\\n'a\\'b'",
        r"$'\c'",
        r"$'\c\''",
        r"$'a)b'",
        r"$'a}b'",
    )
    quote_pieces = (
        "' #'",
        "'a'",
        '" #"',
        '"a\\"b"',
        '"$\'a\\\'b\'"',
        "'$'a'",
        "\\$",
        "\\\\",
        "\\#",
        "\\\n",
        "foo#bar",
        "$$",
        "$",
        "$$$'\\''",
        "$$'\\'' #'",
    )

    live_checked = 0
    refused = 0
    for _ in range(160):
        parts = ["printf %s"]
        for _ in range(rng.randint(1, 4)):
            kind = rng.choice(("ansi", "quote", "cmdsubst", "param", "dq_param", "bt", "dollar_prefixed"))
            if kind == "ansi":
                parts.append(" " + rng.choice(ansi_pieces))
            elif kind == "quote":
                parts.append(" " + rng.choice(quote_pieces))
            elif kind == "dollar_prefixed":
                prefix = rng.choice(("$", "$$", "$$$", "$\\\n$"))
                parts.append(" " + prefix + rng.choice(("'\\'' #'", "$'\\'' ' #'", "'a'")))
            elif kind == "cmdsubst":
                inner = rng.choice(ansi_pieces) + " " + rng.choice(quote_pieces)
                if rng.random() < 0.35:
                    inner += f" ; echo {marker}"
                parts.append(f" $(printf %s {inner})")
            elif kind == "param":
                parts.append(f" ${{UNSET_VAR:-{rng.choice(ansi_pieces)}}}")
            elif kind == "dq_param":
                parts.append(f' "${{UNSET_VAR:-{rng.choice((*ansi_pieces[:4], chr(34) + "a}" + chr(34)))}}}"')
            else:
                parts.append(f" `printf %s {rng.choice((*ansi_pieces[:3], quote_pieces[0]))}`")

        tail = rng.choice(("live", "comment_then_live", "live_then_unclosed", "comment_only", "cont_live"))
        if tail == "live":
            parts.append(f" ; echo {marker}")
        elif tail == "comment_then_live":
            parts.append(f" # comment \\\necho {marker}")
        elif tail == "live_then_unclosed":
            parts.append(f" ; echo {marker}\n'")
        elif tail == "comment_only":
            parts.append(f" # {marker}")
        else:
            parts.append(f" ; ec\\\nho {marker}")

        command = "".join(parts)
        bash_ran = (
            marker
            in subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False, timeout=5).stdout
        )
        try:
            visible = preprocess_shell_command(command)
        except ShellPreprocessLimit:
            refused += 1
            continue
        if bash_ran:
            live_checked += 1
            assert marker in visible, f"Bash ran marker but preprocessor hid it: {command!r} -> {visible!r}"
            if tail == "cont_live":
                assert f"echo {marker}" in visible, f"Continuation not joined: {command!r} -> {visible!r}"

    assert live_checked >= 20
    assert refused >= 10


def test_ansi_c_reported_class_refused_by_secret_pr_and_write_guards(configured_roots):
    """Defect class: shared preprocessor ANSI-C escape bypass across secret, PR merge, and write guards."""
    import importlib.util
    from pathlib import Path

    hooks_dir = Path(__file__).resolve().parents[2] / "agents_extensions" / "shared" / "hooks"

    def load_hook(filename: str, mod_name: str):
        spec = importlib.util.spec_from_file_location(mod_name, hooks_dir / filename)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    secret = load_hook("guard-secret-print.py", "test_ansic_secret_print")
    pr_merge = load_hook("guard-pr-merge.py", "test_ansic_pr_merge")
    primary_write = load_hook("guard-primary-checkout-write.py", "test_ansic_primary_write")

    for cmd in (
        "echo $'\\'' ' #' ; env\n'",
        "echo $\\\n'\\'' ' #' ; env\n'",
        "echo $$'\\'' #' ; env",
        "echo $$$$'\\'' #' ; env",
        "echo $\\\n$'\\'' #' ; env",
        'echo \\$$\'\\\'\' "a\\" " ; env ; echo \\"',
        'echo \\\' "a\\" " ; env ; echo \\"',
        "echo $'\\'' '<<EOF'\nenv\nEOF",
        "cat <<EOF\n$(echo $'\\')' ; env)\nEOF",
    ):
        assert secret._scan_command(cmd, set()) is not None, cmd

    for cmd in (
        r"echo `printf '%s' $'\\'' ' #' ; env`",
        '''x=; echo "${x:-$'a}"'}" " #" ; env''',
    ):
        assert "limit" in secret._process_facts(cmd), cmd
        with pytest.raises(ShellPreprocessLimit):
            secret._scan_command(cmd, set())

    assert any(
        pr_merge._merge_args(seg) is not None or seg == pr_merge._UNPARSED
        for _, seg in pr_merge._scope_events("echo $'\\'' ' #' ; gh pr merge 5\n'")
    )
    for cmd in (
        r"echo `printf '%s' $'\\'' ' #' ; gh pr merge 5`",
        '''x=; echo "${x:-$'a}"'}" " #" ; gh pr merge 5''',
    ):
        with pytest.raises(ShellPreprocessLimit):
            pr_merge._scope_events(cmd)

    assert primary_write._has_ansi_c_quoting("echo $'\\'' ' #' ; tee AGENTS.md\n'")
    with pytest.raises(ShellPreprocessLimit):
        primary_write.bash_write_targets(r"echo `printf '%s' $'\\'' ' #' ; tee AGENTS.md`")
