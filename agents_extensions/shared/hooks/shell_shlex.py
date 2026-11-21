"""Shared Bash reading for hook guards: words, comments and here-documents.

The guards need each word's original spelling, quotes included. Non-POSIX
shlex keeps that spelling but honours a quote only at the start of a token, so
a quote opening inside a word (`it'\\''s`, `a' 'b`) flipped quote parity.
split_quote_preserving tracks Bash quoting itself instead. parse_heredocs is
the one here-document parser; see the plan comment above it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import NamedTuple

_MAX_BACKTICK_DEPTH = 16
_SHELL_OPERATORS = (
    "&>>",
    "<<<",
    "<<-",
    "&>",
    ">>",
    ">|",
    "&&",
    "||",
    ";;",
    "|&",
    "<<",
    "<>",
    ">&",
    "<&",
    ">",
    "<",
    "|",
    "&",
    ";",
    "(",
    ")",
    "\n",
)
_OPERATOR_CHARS = frozenset("();<>|&\n")


class ShellPreprocessLimit(RuntimeError):
    """A nested command could not be exposed within the parser's depth cap."""


def normalize_io_descriptors(command: str, replace: Callable[[str, str], str] | None = None) -> str:
    """Preserve descriptor/operator identity before lexing loses adjacency.

    The default drops descriptors for argv/write-target readers. Output-flow
    readers can protect the pair through their lexer with ``replace``.
    """
    out: list[str] = []
    quote = ""
    word_start = 0
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            out.extend(command[index : index + 2])
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        else:
            if char in "<>":
                word = command[word_start:index]
                number = word.lstrip("0") or "0"
                if re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", word) or (
                    re.fullmatch(r"[0-9]+", word)
                    and (len(number) < 10 or (len(number) == 10 and number <= "2147483647"))
                ):
                    del out[-len(word) :]
                    operator = re.match(r"<<<|<<-?|[<>]&|<>|>\||>>?|<", command[index:]).group()
                    out.append(replace(word, operator) if replace else operator)
                    index += len(operator)
                    word_start = index
                    continue
            if char in " \t\n();<>|&":
                word_start = index + 1
        out.append(char)
        index += 1
    return "".join(out)


def split_operator_run(token: str) -> list[str]:
    """Split a pure shell-punctuation run, keeping redirects intact."""
    if token in _SHELL_OPERATORS or not token or not set(token) <= _OPERATOR_CHARS:
        return [token]
    parts: list[str] = []
    index = 0
    while index < len(token):
        operator = next(op for op in _SHELL_OPERATORS if token.startswith(op, index))
        parts.append(operator)
        index += len(operator)
    return parts


def collapse_line_continuations(command: str) -> str:
    """Join Bash continuations while respecting physical-line comments."""
    out: list[str] = []
    quote = ""
    outer_quote = ""
    backtick_outer_quote = ""
    substitution_depth = 0
    backticks = False
    comment = False
    word_start = True
    i = 0
    while i < len(command):
        char = command[i]
        if comment:
            # A backtick closes its body before Bash parses that body's
            # comment. Only an unescaped closer can resume the outer command.
            if backticks and char == "`":
                slash = i - 1
                while slash >= 0 and command[slash] == "\\":
                    slash -= 1
                if (i - 1 - slash) % 2 == 0:
                    comment = False
            if comment:
                out.append(char)
                if char == "\n":
                    comment = False
                    word_start = True
                i += 1
                continue
        if char == "\\" and quote != "'" and i + 1 < len(command):
            following = command[i + 1]
            if following != "\n":
                out.extend((char, following))
                word_start = False
            i += 2
            continue
        if char == "'" and quote != '"':
            quote = "" if quote == "'" else "'"
            word_start = False
        elif char == '"' and quote != "'":
            quote = "" if quote == '"' else '"'
            word_start = False
        elif char == "`" and quote != "'":
            if not backticks:
                backtick_outer_quote = quote
                quote = ""
            else:
                quote = backtick_outer_quote
            backticks = not backticks
            word_start = backticks
        elif quote != "'" and not backticks and command.startswith("$(", i):
            if not substitution_depth:
                outer_quote = quote
                quote = ""
            substitution_depth += 1
            out.append("$(")
            word_start = True
            i += 2
            continue
        elif substitution_depth and not quote and not backticks and char == "(":
            substitution_depth += 1
        elif substitution_depth and not quote and not backticks and char == ")":
            substitution_depth -= 1
            if not substitution_depth:
                quote = outer_quote
            word_start = False
        elif not quote:
            if char == "#" and word_start:
                comment = True
            word_start = char in " \t\n;&|()"
        out.append(char)
        i += 1
    return "".join(out)


def strip_shell_comments(command: str) -> str:
    """Remove Bash comments at word boundaries without changing quoted text."""
    out: list[str] = []
    quote = ""
    backticks = False
    substitution_depth = 0
    outer_quote = ""
    backtick_outer_quote = ""
    word_start = True
    i = 0
    while i < len(command):
        char = command[i]
        if char == "\\" and quote != "'" and i + 1 < len(command):
            out.append(command[i : i + 2])
            word_start = False
            i += 2
            continue
        if char == "'" and quote != '"':
            quote = "" if quote == "'" else "'"
            word_start = False
        elif char == '"' and quote != "'":
            quote = "" if quote == '"' else '"'
            word_start = False
        elif char == "`" and quote != "'":
            if not backticks:
                backtick_outer_quote = quote
                quote = ""
            else:
                quote = backtick_outer_quote
            backticks = not backticks
            word_start = backticks
        elif quote != "'" and not backticks and command.startswith("$(", i):
            if not substitution_depth:
                outer_quote = quote
                quote = ""
            substitution_depth += 1
            out.append("$(")
            word_start = True
            i += 2
            continue
        elif substitution_depth and not quote and not backticks and char == "(":
            substitution_depth += 1
        elif substitution_depth and not quote and not backticks and char == ")":
            substitution_depth -= 1
            if not substitution_depth:
                quote = outer_quote
            word_start = False
        elif not quote:
            if char == "#" and word_start:
                end = i + 1
                while end < len(command):
                    if command[end] == "\n" or (backticks and command[end] == "`"):
                        break
                    if backticks and command[end] == "\\" and end + 1 < len(command) and command[end + 1] != "\n":
                        end += 2
                    else:
                        end += 1
                if end == len(command):
                    break
                i = end
                continue
            word_start = char in " \t\n;&|()"
        out.append(char)
        i += 1
    return "".join(out)


def _expose_backtick_bodies(command: str, *, depth: int = 0) -> str:
    """Expose executable backticks as inline command substitutions for guards."""
    if depth >= _MAX_BACKTICK_DEPTH and "`" in command:
        raise ShellPreprocessLimit("nested backtick depth exceeded")
    out: list[str] = []
    quote = ""
    substitutions: list[tuple[str, int]] = []
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            out.append(command[index : index + 2])
            index += 2
            continue
        if char == "'" and quote != '"':
            quote = "" if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = "" if quote == '"' else '"'
        elif quote != "'" and command.startswith("$(", index):
            substitutions.append((quote, 1))
            quote = ""
            out.append("$(")
            index += 2
            continue
        elif quote != "'" and char == "`":
            end = index + 1
            while end < len(command) and command[end] != "`":
                end += 2 if command[end] == "\\" else 1
            if end < len(command):
                if quote == '"':
                    out.append('"')
                body = re.sub(r"\\([\\`])", r"\1", command[index + 1 : end])
                if "`" in body:
                    body = _expose_backtick_bodies(body, depth=depth + 1)
                out.extend(("$(", body, ")"))
                if quote == '"':
                    out.append('"')
                index = end + 1
                continue
        elif char == "(" and not quote and substitutions:
            outer_quote, paren_depth = substitutions[-1]
            substitutions[-1] = (outer_quote, paren_depth + 1)
        elif char == ")" and not quote and substitutions:
            outer_quote, paren_depth = substitutions[-1]
            if paren_depth == 1:
                substitutions.pop()
                quote = outer_quote
            else:
                substitutions[-1] = (outer_quote, paren_depth - 1)
        out.append(char)
        index += 1
    return "".join(out)


def preprocess_shell_command(command: str) -> str:
    """Prepare executable text for every guard in one conservative order.

    The here-document parser below joins line continuations in parser
    context and shows each body as plain words; then comments are removed
    and backticks are exposed as `$(...)`.
    """
    return _expose_backtick_bodies(strip_shell_comments(heredoc_view(command)))


# Here-document plan: one parser for every guard.
#
# parse_heredocs reads a command the way Bash finds here-documents. It tracks
# quotes ('...', "...", $'...', $"..."), backslashes, comments, `$(...)`,
# backticks, `${...}` and arithmetic, so a `<<` is an operator only in command
# context. Each body starts after the first newline Bash reads as a token once
# its operator is seen (a newline inside quotes does not count), and ends at
# the line equal to the closer. A quoted delimiter keeps physical lines; an
# unquoted one joins backslash-newline continuations before comparing, as
# Bash does. Line continuations in command text are joined in the same pass.
#
# A body is never read as shell words: an apostrophe in prose is data. No
# body is dropped either, whatever reads it, because no reader name proves
# what will consume it (a function, alias or PATH entry can stand behind any
# name). heredoc_view shows every body to the guards in two parts:
#
# - its words (_body_words), in one subshell line `( ... ; ... )`. Quotes,
#   backslashes, comments, redirects and expansion punctuation are removed;
#   `;`, `&`, `|`, `)`, backticks and a `(` that does not follow a word
#   character start a new command, and a reserved word leading a command is
#   dropped. So any command a shell could
#   run from the body starts a command, no body opens syntax, and a `cd` in it
#   cannot move a guard's idea of the working directory;
# - for an unquoted delimiter, the expansions the outer shell performs on the
#   body (`$(...)`, backticks, `${...}`, `$((...))`), as real shell text, each
#   on its own line: they run whatever reads the body.
#
# Anything the parser cannot resolve refuses with ShellPreprocessLimit: a
# delimiter that may expand or is empty, a body or expansion that never
# closes, a `case` inside a substitution (its patterns end in `)`), an
# unbalanced arithmetic context, nesting deeper than _MAX_NESTING, or a quote
# or substitution still open at the end of a command that has a
# here-document.

_MAX_NESTING = 16
_DELIMITER_END = frozenset(" \t\n;|&()<>")
_SUBSCRIPTED = re.compile(r"(?:^|[ \t\n;&|(])[A-Za-z_][A-Za-z0-9_]*\Z")
_BODY_BREAKS = str.maketrans({char: "\n" for char in ";&|()`"})
# Quote removal joins a word (`g"h"` is `gh`): quotes and backslashes go.
# Other punctuation only separates words.
_BODY_DROPS = str.maketrans({"'": None, '"': None, "\\": None, **{char: " " for char in "#<>${}"}})
_RESERVED_WORDS = frozenset(
    {"!", "[[", "]]", "case", "coproc", "do", "done", "elif", "else", "esac", "fi", "for", "function", "if", "in"}
    | {"select", "then", "time", "until", "while"}
)
_EXPANSIONS = ("$((", "$(", "${", "$[")
_CLOSERS = {"((": "))", "$[": "]", "${": "}"}


class HereDocument(NamedTuple):
    """One here-document, bound to the command and descriptor that own it.

    Offsets are into the text the parser read; inside backticks, that is the
    backtick body after Bash removes its escapes.
    """

    operator: int  # offset of its `<<`
    owner: int  # offset where its simple command starts
    descriptor: str  # the descriptor word before `<<`; "" for standard input
    closer: str
    quoted: bool  # a quoted delimiter: the body is literal
    strip_tabs: bool  # `<<-`
    lines: tuple[str, ...]  # physical body lines, leading tabs removed for `<<-`


def _ansi_c(text: str) -> str:
    """Decode the escapes of a $'...' body so its words are what Bash runs."""

    def decode(match: re.Match[str]) -> str:
        escape = match.group(1)
        if escape[0] in "xuU":
            return chr(int(escape[1:], 16)) if len(escape) > 1 else escape
        if escape[0] in "01234567":
            return chr(int(escape, 8) & 0xFF)
        return {"n": "\n", "t": "\t", "r": "\r", "e": "\x1b", "E": "\x1b", "a": "\a", "b": "\b"}.get(escape, escape)

    return re.sub(r"\\(x[0-9A-Fa-f]{0,2}|u[0-9A-Fa-f]{0,4}|U[0-9A-Fa-f]{0,8}|[0-7]{1,3}|.)", decode, text, flags=re.S)


def _body_words(text: str) -> list[str]:
    """A body as plain words, one entry per command a shell could start."""
    # A reading shell joins continuations, quoted delimiter or not.
    text = re.sub(r"(?<!\\)((?:\\\\)*)\\\n", r"\1", text)
    text = re.sub(r"\$'((?:[^'\\]|\\.)*)'?", lambda match: _ansi_c(match.group(1)), text, flags=re.S)
    # A `(` right after a word character never starts a command in Bash
    # (`name()` opens a function definition), so call syntax in code or prose
    # (`set()`, `dict(env)`) cannot turn an argument into a command.
    text = re.sub(r"(?<=[A-Za-z0-9_.\]])\(", " ", text)
    # A shell reading the body expands `$NAME` and `${NAME...}`: keep the name.
    text = re.sub(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", "\x00\\1", text)
    text = text.translate(_BODY_BREAKS).translate(_BODY_DROPS).replace("\x00", "$")
    words: list[str] = []
    for line in text.split("\n"):
        parts = line.split()
        # A reserved word only opens syntax; the command after it is what runs.
        while parts and parts[0] in _RESERVED_WORDS:
            parts.pop(0)
        if parts:
            words.append(" ".join(parts))
    return words


def _tail(out: list[str], size: int = 64) -> str:
    """The last `size` characters written to `out`, without joining all of it."""
    parts: list[str] = []
    count = 0
    for part in reversed(out):
        parts.append(part)
        count += len(part)
        if count >= size:
            break
    return "".join(reversed(parts))[-size:]


def document_words(document: HereDocument) -> list[str]:
    """A body as plain words, one entry per command (see the plan above).

    For guards that also scan body text for their own pattern: any program,
    not only a shell, may read the body.
    """
    return _body_words("\n".join(document.lines))


def _backtick_body(command: str, index: int) -> tuple[str, int]:
    """The command text of the backticks opening at `index`, and the offset after them."""
    end = index + 1
    while end < len(command) and command[end] != "`":
        end += 2 if command[end] == "\\" else 1
    if end >= len(command):
        raise ShellPreprocessLimit("backtick substitution does not close")
    return re.sub(r"\\([\\`$])", r"\1", command[index + 1 : end]), end + 1


def _body_expansions(text: str, documents: list[HereDocument], depth: int) -> list[str]:
    """The expansions the outer shell performs on an unquoted body, as shell text."""
    found: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == "`":
            body, index = _backtick_body(text, index)
            inner, view, _ = _parse(body, depth=depth + 1)
            documents.extend(inner)
            found.append("$(" + view + ")")
            continue
        opening = next((op for op in _EXPANSIONS if text.startswith(op, index)), "")
        if not opening:
            index += 1
            continue
        context = "cmd" if opening == "$(" else "((" if opening == "$((" else opening
        inner, view, index = _parse(text, index + len(opening), [context], depth + 1)
        documents.extend(inner)
        found.append(opening + view)
    return found


def heredoc_view(command: str) -> str:
    """The command as every guard reads it: continuations joined, bodies shown as above.

    The `<<` operators and closer lines stay (closers as view_closer writes
    them), so a reader can still pair a body with its command, and parsing the
    view again yields the same plan.
    """
    if "<<" not in command:
        return collapse_line_continuations(command)
    return _parse(command)[1]


def parse_heredocs(command: str) -> tuple[HereDocument, ...]:
    """Every here-document in `command`, in the order Bash reads them."""
    if "<<" not in command:
        return ()
    return _parse(command)[0]


def _parse(
    command: str, index: int = 0, inside: list[str] | None = None, depth: int = 0
) -> tuple[tuple[HereDocument, ...], str, int]:
    """Parse from `index`, at the top level or inside one expansion context.

    Return the documents, the view, and the offset where the expansion closed
    (the end of `command` at the top level).
    """
    if depth > _MAX_NESTING:
        raise ShellPreprocessLimit("shell nesting too deep to read safely")
    out: list[str] = []
    documents: list[HereDocument] = []
    pending: list[tuple[int, int, str, str, bool, bool]] = []
    # Contexts: "cmd" (top level or `$(`), quotes, "${", "((" and "$[".
    stack: list[str] = ["cmd", *(inside or [])]
    base = len(stack) - 1
    parens: list[int] = [0] * len(stack)
    word_start = True
    owner = index
    length = len(command)

    def close() -> bool:
        stack.pop()
        parens.pop()
        return len(stack) <= base

    while index < length:
        char = command[index]
        context = stack[-1]
        if stack.count("cmd") > _MAX_NESTING:
            raise ShellPreprocessLimit("shell nesting too deep to read safely")
        if context in {"'", "$'"}:
            if context == "$'" and char == "\\" and index + 1 < length:
                out.append(command[index : index + 2])
                index += 2
                continue
            out.append(char)
            index += 1
            if char == "'" and close():
                return tuple(documents), "".join(out), index
            continue
        if char == "\\":
            if command.startswith("\\\n", index):
                index += 2  # a line continuation, joined in parser context
                continue
            out.append(command[index : index + 2])
            index += 2
            word_start = False
            continue
        if char == '"':
            out.append(char)
            index += 1
            word_start = False
            if context != '"':
                stack.append('"')
                parens.append(0)
            elif close():
                return tuple(documents), "".join(out), index
            continue
        if char == "'" and context != '"':
            stack.append("$'" if out and out[-1] == "$" else "'")
            parens.append(0)
            out.append(char)
            index += 1
            word_start = False
            continue
        if char == "`":
            body, index = _backtick_body(command, index)
            inner, view, _ = _parse(body, depth=depth + 1)
            documents.extend(inner)
            out.append("$(" + view + ")")
            word_start = False
            continue
        opening = next((op for op in _EXPANSIONS if command.startswith(op, index)), "")
        if not opening and context == "cmd" and word_start and command.startswith("((", index):
            opening = "(("
        if not opening and context == "cmd" and char == "[" and _SUBSCRIPTED.search(_tail(out)):
            opening = "["  # an array subscript (`x[1 << 2]=1`) is arithmetic: its `<<` is a shift
        if opening:
            stack.append(
                "cmd" if opening == "$(" else "((" if opening == "$((" else "$[" if opening == "[" else opening
            )
            parens.append(0)
            if opening == "$(":
                owner = index + 2
            out.append(opening)
            index += len(opening)
            word_start = True
            continue
        if context in _CLOSERS:
            closing = _CLOSERS[context]
            if context != "${" and char == "(":
                parens[-1] += 1
            elif context != "${" and char == ")" and parens[-1]:
                parens[-1] -= 1
            elif command.startswith(closing, index) and not parens[-1]:
                out.append(closing)
                index += len(closing)
                word_start = False
                if close():
                    return tuple(documents), "".join(out), index
                continue
            elif char == ")" and context == "((":
                raise ShellPreprocessLimit("arithmetic context cannot be read safely")
            if char == "\n" and pending:
                raise ShellPreprocessLimit("here-document opener spans an expansion")
            out.append(char)
            index += 1
            continue
        if context == '"':
            # A newline in quotes is not the one Bash reads bodies after.
            out.append(char)
            index += 1
            continue
        # Command context: the top level or `$(...)`.
        if char == "#" and word_start:
            end = command.find("\n", index)
            end = length if end < 0 else end
            out.append(command[index:end])
            index = end
            continue
        if len(stack) > 1 and word_start and re.match(r"case[ \t\n]", command[index : index + 5]):
            raise ShellPreprocessLimit("case command inside a substitution cannot be read safely")
        if char == "(":
            parens[-1] += 1
        elif char == ")":
            if parens[-1]:
                parens[-1] -= 1
            elif len(stack) > 1:
                out.append(char)
                index += 1
                word_start = False
                if close():
                    return tuple(documents), "".join(out), index
                continue
        if command.startswith("<<<", index):
            out.append("<<<")
            index += 3
            word_start = True
            continue
        if command.startswith("<<", index):
            index = _read_operator(command, index, out, pending, owner)
            word_start = True
            continue
        if char == "\n":
            out.append(char)
            index += 1
            if pending:
                index = _read_bodies(command, index, out, pending, documents, depth)
            word_start = True
            owner = index
            continue
        if char in ";&|":
            owner = index + 1
        out.append(char)
        index += 1
        word_start = char in " \t;&|()<>"
    if pending:
        raise ShellPreprocessLimit("here-document body does not close")
    if len(stack) > 1:
        raise ShellPreprocessLimit("quote or substitution left open in a command with a here-document")
    return tuple(documents), "".join(out), index


def _read_operator(
    command: str, index: int, out: list[str], pending: list[tuple[int, int, str, str, bool, bool]], owner: int
) -> int:
    """Read one `<<` or `<<-` operator and its delimiter word; return the next offset."""
    operator = index
    descriptor = re.search(r"(?:^|(?<=[ \t\n;&|()<>]))(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})\Z", _tail(out))
    index += 2
    strip_tabs = command.startswith("-", index)
    index += strip_tabs
    while index < len(command) and command[index] in " \t":
        index += 1
    start = index
    quote = ""
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if char in "\"'" and (not quote or quote == char):
            quote = "" if quote else char
        elif not quote and char in _DELIMITER_END:
            break
        index += 1
    word = command[start:index]
    closer = heredoc_closer(word) if "\r" not in word and "\n" not in word else None
    if closer is None:
        raise ShellPreprocessLimit("here-document delimiter cannot be read safely")
    pending.append((operator, owner, descriptor.group() if descriptor else "", closer[0], closer[1], strip_tabs))
    name = view_closer(closer[0])
    out.append(("<<-" if strip_tabs else "<<") + (f"'{name}'" if closer[1] else name))
    return index


def view_closer(closer: str) -> str:
    """The closer heredoc_view writes for `closer`: itself when it is a plain word.

    Any other closer (`EO"F`, `END-DATA`) becomes an identifier that encodes it,
    so the view's closer line opens no quote and parses to the same plan.
    """
    if re.fullmatch(r"[A-Za-z0-9_]+", closer):
        return closer
    return "LU_HEREDOC_" + closer.encode("utf-8").hex()


def _read_bodies(
    command: str,
    index: int,
    out: list[str],
    pending: list[tuple[int, int, str, str, bool, bool]],
    documents: list[HereDocument],
    depth: int,
) -> int:
    """Read the pending bodies that start at `index`; return the offset after them."""
    for operator, owner, descriptor, closer, quoted, strip_tabs in pending:
        lines: list[str] = []
        while True:
            if index > len(command) or (index == len(command) and command.endswith("\n")):
                raise ShellPreprocessLimit("here-document body does not close")
            end = command.find("\n", index)
            end = len(command) if end < 0 else end
            line = command[index:end]
            logical = line
            # An unquoted body joins continuations before Bash compares the closer.
            while not quoted and (len(logical) - len(logical.rstrip("\\"))) % 2 and end < len(command):
                following = command.find("\n", end + 1)
                following = len(command) if following < 0 else following
                logical = logical[:-1] + command[end + 1 : following]
                line = line + "\n" + command[end + 1 : following]
                end = following
            index = end + 1
            if (logical.lstrip("\t") if strip_tabs else logical) == closer:
                break
            lines.extend(part.lstrip("\t") if strip_tabs else part for part in line.split("\n"))
        documents.append(HereDocument(operator, owner, descriptor, closer, quoted, strip_tabs, tuple(lines)))
        text = "\n".join(lines)
        words = _body_words(text)
        if words:
            out.append("( " + " ; ".join(words) + " )\n")
        if not quoted:
            out.extend(expansion + "\n" for expansion in _body_expansions(text, documents, depth))
        out.append(view_closer(closer) + ("\n" if index <= len(command) else ""))
    pending.clear()
    return min(index, len(command))


def static_word(word: str) -> tuple[str, bool] | None:
    """A literal word after Bash quote removal, and whether it held any quoting.

    None for an empty word, an unclosed quote, a trailing backslash, or a
    word that may expand (`$`, a backtick); callers fail closed on None.
    """
    if not word or "$" in word or "`" in word:
        return None
    out: list[str] = []
    quoted = False
    quote = ""
    index = 0
    while index < len(word):
        char = word[index]
        if quote == "'":
            if char == "'":
                quote = ""
            else:
                out.append(char)
        elif char == "\\":
            if index + 1 >= len(word):
                return None
            following = word[index + 1]
            quoted = True
            # In double quotes a backslash escapes only these characters.
            if quote == '"' and following not in '"\\\n':
                out.append(char)
            out.append(following)
            index += 2
            continue
        elif char == '"':
            quoted = True
            quote = "" if quote else '"'
        elif char == "'" and not quote:
            quoted = True
            quote = "'"
        else:
            out.append(char)
        index += 1
    if quote:
        return None
    return "".join(out), quoted


def heredoc_closer(word: str) -> tuple[str, bool] | None:
    """The closer and literal flag of a static here-document delimiter word."""
    closer = static_word(word)
    return None if closer is None or not closer[0] else closer


def split_quote_preserving(command: str, *, punctuation_chars: str | bool, whitespace: str) -> list[str]:
    """Split `command` into Bash words and operator runs, keeping each spelling.

    Quotes and backslashes are tracked the way Bash reads them: a quote opens
    wherever it stands in a word, a backslash outside single quotes takes the
    next character, and only unquoted blanks and operator characters split.
    A word that starts with a quote ends at its closing quote, so adjacent
    quote fragments (`'pk'"ill"`) stay separate tokens for the callers that
    rejoin them. Operator characters are grouped into runs (`);`) as shlex
    does. An unterminated quote raises ValueError, like shlex.
    """
    punctuation = "();<>|&" if punctuation_chars is True else punctuation_chars or ""
    tokens: list[str] = []
    index = 0
    while index < len(command):
        char = command[index]
        if char in whitespace:
            index += 1
            continue
        start = index
        if char in punctuation:
            while index < len(command) and command[index] in punctuation:
                index += 1
            tokens.append(command[start:index])
            continue
        fragment = char in "'\""
        while index < len(command):
            char = command[index]
            if char in "'\"":
                close = index + 1
                while close < len(command) and command[close] != char:
                    close += 2 if char == '"' and command[close] == "\\" else 1
                if close >= len(command):
                    raise ValueError("No closing quotation")
                index = close + 1
                if fragment:
                    break
            elif char in whitespace or char in punctuation:
                break
            else:
                index += 2 if char == "\\" else 1
        tokens.append(command[start : min(index, len(command))])
    return tokens
