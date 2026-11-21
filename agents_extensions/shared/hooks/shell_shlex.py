"""Quote-preserving Bash word splitting for shared hook guards.

Non-POSIX shlex preserves surrounding quotes, which these guards need, but
mistakes an escaped double quote for the end of a quoted word. Hide only that
quote while shlex splits, then restore the original spelling in each token.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable

_HEREDOC_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
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


def _body_substitutions(body: str) -> list[str] | None:
    """Expose complete substitutions, including ones spanning body lines.

    A substitution we cannot close leaves the entire body visible to guards.
    """
    found: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body.startswith("$((", i):
            i += 3
            continue
        if body.startswith("$(", i):
            start = i
            depth = 1
            quote = ""
            backticks = False
            comment = False
            word_start = True
            i += 2
            while i < len(body) and depth:
                char = body[i]
                if comment:
                    if char == "\n":
                        comment = False
                        word_start = True
                    i += 1
                    continue
                if char == "\\" and quote != "'":
                    i += 2
                    word_start = False
                    continue
                if char == "'" and quote != '"' and not backticks:
                    quote = "" if quote == "'" else "'"
                    word_start = False
                elif char == '"' and quote != "'" and not backticks:
                    quote = "" if quote == '"' else '"'
                    word_start = False
                elif char == "`" and quote != "'":
                    backticks = not backticks
                    word_start = False
                elif not quote and not backticks:
                    if char == "#" and word_start:
                        comment = True
                    elif char == "(":
                        depth += 1
                    elif char == ")":
                        depth -= 1
                    word_start = char in " \t\n;&|()"
                i += 1
            if depth:
                return None
            found.append(body[start:i])
            continue
        if body[i] == "`":
            start = i + 1
            i += 1
            while i < len(body) and body[i] != "`":
                i += 2 if body[i] == "\\" else 1
            if i >= len(body):
                return None
            found.append("$(" + body[start:i] + ")")
            i += 1
            continue
        i += 1
    return found


def preprocess_shell_command(command: str) -> str:
    """Prepare executable text for every guard in one conservative order.

    Comments are removed from opener lines before recognizing here-docs, but
    body lines are preserved until substitutions are extracted. A hash at the
    start of a body line is data, and its substitution can still execute.
    """
    collapsed = collapse_line_continuations(command)
    visible = strip_skippable_heredoc_bodies(
        collapsed, opener_transform=strip_shell_comments, body_substitutions=_body_substitutions
    )
    return _expose_backtick_bodies(strip_shell_comments(visible))


def skippable_heredoc_delimiters(line: str, *, initial_quote: str = "") -> list[tuple[str, bool, bool]] | None:
    """Find only here-doc words whose literal closer is unambiguous.

    Return (closer, strip_tabs, quoted), or None for any ambiguous opener.
    Callers then keep every remaining line visible to their command scanner.
    """
    delimiters: list[tuple[str, bool, bool]] = []
    ambiguous = False
    index = 0
    quote = initial_quote
    contexts: list[str] = []
    arithmetic_shift = False
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(line):
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char in "\"'":
            quote = char
            index += 1
            continue
        if line.startswith("$((", index):
            contexts.append("arithmetic")
            index += 3
            continue
        if line.startswith("$[", index):
            contexts.append("arithmetic_bracket")
            index += 2
            continue
        if char == "[" and re.search(r"[A-Za-z_][A-Za-z0-9_]*\Z", line[:index]):
            contexts.append("arithmetic_bracket")
            index += 1
            continue
        if line.startswith("${", index):
            contexts.append("parameter")
            index += 2
            continue
        if line.startswith("$(", index):
            contexts.append("command")
            index += 2
            continue
        if line.startswith("((", index):
            contexts.append("arithmetic")
            index += 2
            continue
        if contexts and contexts[-1] == "arithmetic" and line.startswith("))", index):
            contexts.pop()
            index += 2
            continue
        if contexts and contexts[-1] == "parameter" and char == "}":
            contexts.pop()
            index += 1
            continue
        if contexts and contexts[-1] == "arithmetic_bracket" and char == "]":
            contexts.pop()
            index += 1
            continue
        if contexts and contexts[-1] == "command" and char == ")":
            contexts.pop()
            index += 1
            continue
        if contexts and contexts[-1] == "command" and char == "(":
            contexts.append("command")
            index += 1
            continue
        if line.startswith("<<<", index):
            index += 3
            continue
        if not line.startswith("<<", index):
            index += 1
            continue
        if contexts and contexts[-1] in {"arithmetic", "arithmetic_bracket", "parameter"}:
            arithmetic_shift = True
            index += 2
            continue

        index += 2
        strip_tabs = index < len(line) and line[index] == "-"
        if strip_tabs:
            index += 1
        while index < len(line) and line[index] in " \t":
            index += 1
        start = index
        word_quote = ""
        while index < len(line):
            char = line[index]
            if char == "\\" and word_quote != "'" and index + 1 < len(line):
                index += 2
                continue
            if char in "\"'" and (not word_quote or word_quote == char):
                word_quote = "" if word_quote else char
            elif not word_quote and char in " \t\r\n;|&()<>":
                break
            index += 1
        word = line[start:index]
        if _HEREDOC_IDENTIFIER.fullmatch(word):
            delimiters.append((word, strip_tabs, False))
        elif len(word) >= 3 and word[0] in "\"'" and word[-1] == word[0] and _HEREDOC_IDENTIFIER.fullmatch(word[1:-1]):
            delimiters.append((word[1:-1], strip_tabs, True))
        else:
            ambiguous = True
    # A later safe-looking opener must not hide lines after an exotic opener
    # on the same command line.
    return (
        None
        if ambiguous
        or (arithmetic_shift and delimiters)
        or any(c in {"arithmetic", "arithmetic_bracket", "parameter"} for c in contexts)
        else delimiters
    )


def _quote_after(line: str, initial_quote: str) -> str:
    quote = initial_quote
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if char in "\"'" and (not quote or quote == char):
            quote = "" if quote else char
        i += 1
    return quote


def strip_skippable_heredoc_bodies(
    command: str,
    *,
    opener_transform: Callable[[str], str] | None = None,
    body_substitutions: Callable[[str], list[str] | None] | None = None,
) -> str:
    """Skip only closed, unambiguous here-doc bodies in all hook guards.

    Bash retains carriage returns in here-doc delimiters. A command containing
    one is left intact, so a mismatched closer can never conceal later commands.
    """
    if "<<" not in command or "\r" in command:
        return command

    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    quote = ""
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        opener = opener_transform(line) if opener_transform else line
        pending = skippable_heredoc_delimiters(opener, initial_quote=quote)
        quote = _quote_after(opener, quote)
        if pending is None:
            kept.extend(lines[index:])
            break
        body_start = index
        substitutions: list[str] = []
        undecidable_body = False
        while pending and index < len(lines):
            delimiter, strip_tabs, quoted = pending[0]
            part_start = index
            while index < len(lines):
                candidate = lines[index].lstrip("\t") if strip_tabs else lines[index]
                if candidate == delimiter:
                    break
                index += 1
            if index == len(lines):
                break
            if not quoted and body_substitutions:
                body = "\n".join(lines[part_start:index])
                extracted = body_substitutions(body)
                if extracted is None:
                    undecidable_body = True
                else:
                    substitutions.extend(extracted)
            pending.pop(0)
            index += 1
        if pending or undecidable_body:
            kept.extend(lines[body_start:index])
        else:
            kept.extend(substitutions)
    return "\n".join(kept)


# drop_literal_cat_bodies reads only this alphabet; any other character outside
# a here-document body leaves the command as it was.
_LETTERS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_NAME_CHARS = _LETTERS | frozenset("0123456789")
_WORD_CHARS = _NAME_CHARS | frozenset("./:@%+,=~-")
# A setup assignment may name only a variable of one or two characters: no
# variable that Bash, the loader or `cat` reads (PATH, IFS, BASH_ENV, LD_*)
# is that short, so the assignment cannot change what `cat` runs or reads.
_SETUP_NAME_MAX = 2


def _name_end(text: str, index: int) -> int | None:
    if index >= len(text) or text[index] not in _LETTERS:
        return None
    index += 1
    while index < len(text) and text[index] in _NAME_CHARS:
        index += 1
    return index


def _expansion_end(text: str, index: int) -> int | None:
    """End of `$NAME` or `${NAME}` at ``index``; any other `$` form is None."""
    if text.startswith("${", index):
        end = _name_end(text, index + 2)
        return end + 1 if end is not None and text.startswith("}", end) else None
    return _name_end(text, index + 1)


def _word_end(text: str, index: int) -> int | None:
    """End of one word of plain characters, `$NAME`/`${NAME}` and blank-free quotes."""
    start = index
    while index < len(text):
        char = text[index]
        if char in _WORD_CHARS:
            index += 1
        elif char == "$":
            end = _expansion_end(text, index)
            if end is None:
                return None
            index = end
        elif char in "'\"":
            index += 1
            while index < len(text) and text[index] != char:
                if text[index] in _WORD_CHARS:
                    index += 1
                elif char == '"' and text[index] == "$":
                    end = _expansion_end(text, index)
                    if end is None:
                        return None
                    index = end
                else:
                    return None
            if index == len(text):
                return None
            index += 1
        else:
            break
    return index if index > start else None


def _blank_end(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _line_items(line: str) -> list[list[tuple[str, str]]] | None:
    """Split one line at `;` and `&&` into (kind, text) tokens, or None.

    A kind is "word", ">" or ">>" (with the target word), or "<<" or "<<-"
    (with the delimiter of a single- or double-quoted identifier). None means
    the line holds anything else, such as a pipe, a group, a comment, a
    substitution or a backslash.
    """
    items: list[list[tuple[str, str]]] = [[]]
    index = _blank_end(line, 0)
    while index < len(line):
        if line.startswith("&&", index) or line[index] == ";":
            if not items[-1]:
                return None
            items.append([])
            index = _blank_end(line, index + (2 if line[index] == "&" else 1))
            continue
        if line[index] == ">":
            operator = ">>" if line.startswith(">>", index) else ">"
            start = _blank_end(line, index + len(operator))
            end = _word_end(line, start)
            if end is None:
                return None
            items[-1].append((operator, line[start:end]))
        elif line.startswith("<<", index):
            operator = "<<-" if line.startswith("<<-", index) else "<<"
            start = _blank_end(line, index + len(operator))
            quote = line[start : start + 1]
            end = _name_end(line, start + 1) if quote in ("'", '"') else None
            if end is None or not line.startswith(quote, end):
                return None
            items[-1].append((operator, line[start + 1 : end]))
            end += 1
        else:
            end = _word_end(line, index)
            if end is None:
                return None
            items[-1].append(("word", line[index:end]))
        if end < len(line) and line[end] not in " \t;&<>":
            return None
        index = _blank_end(line, end)
    if not items[-1] and len(items) > 1:
        return None
    return items


def _setup_item(item: list[tuple[str, str]]) -> bool:
    """`cd WORD`, `mkdir [-p] WORD...` or `NAME=WORD` with a short name."""
    if not item or any(kind != "word" for kind, _ in item):
        return False
    words = [text for _, text in item]
    if words[0] == "cd":
        return len(words) == 2
    if words[0] == "mkdir":
        return len(words) > 1 and all(word == "-p" or not word.startswith("-") for word in words[1:])
    end = _name_end(words[0], 0)
    return len(words) == 1 and end is not None and end <= _SETUP_NAME_MAX and words[0][end : end + 1] == "="


def _cat_document(item: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(operator, delimiter) of `cat [>|>> WORD] <<'ID'`, in any order, else None."""
    if item[:1] != [("word", "cat")]:
        return None
    documents = [token for token in item[1:] if token[0] in ("<<", "<<-")]
    outputs = [token for token in item[1:] if token[0] in (">", ">>")]
    if len(documents) != 1 or len(outputs) > 1 or len(documents) + len(outputs) != len(item) - 1:
        return None
    return documents[0]


def drop_literal_cat_bodies(command: str) -> str:
    """Drop the bodies Bash certainly hands, unread, to a top-level `cat` (C-6323).

    Only the leading lines of ``command`` are read, and only while each one is
    setup items (`cd WORD`, `mkdir [-p] WORD...`, `NAME=WORD` with a one- or
    two-character name) joined by `;` or `&&`, optionally ending in
    `cat [>|>> WORD] <<'ID'` (or `<<"ID"`, `<<-`). A quoted delimiter makes the
    body literal text, which `cat` copies to a file or to the command's own
    output; nothing before it can redefine `cat` or redirect that output, and
    the body ends at the first line equal to the delimiter. Each such body is
    removed; its opener and closer lines and every later line are kept for the
    guards to judge as before.

    Anything else returns ``command`` unchanged, so the guards read it exactly
    as they did before: a pipe, group, substitution, comment, backslash,
    unquoted or unclosed delimiter, carriage return or any other command before
    the body. A later command that runs the written file is not followed, the
    same limit as a file written by `printf` or an earlier command.
    """
    if "<<" not in command or "\r" in command or "\0" in command:
        return command
    lines = command.split("\n")
    kept: list[str] = []
    index = 0
    dropped = False
    while index < len(lines):
        items = _line_items(lines[index])
        if items is None or not all(_setup_item(item) for item in items[:-1]):
            break
        if not items[-1] or _setup_item(items[-1]):
            kept.append(lines[index])
            index += 1
            continue
        document = _cat_document(items[-1])
        if document is None:
            break
        operator, closer = document
        end = index + 1
        while end < len(lines) and (lines[end].lstrip("\t") if operator == "<<-" else lines[end]) != closer:
            end += 1
        if end == len(lines):
            return command
        kept.extend((lines[index], lines[end]))
        index = end + 1
        dropped = True
    return "\n".join(kept + lines[index:]) if dropped else command


def split_quote_preserving(command: str, *, punctuation_chars: str | bool, whitespace: str) -> list[str]:
    marker_code = 0xE000
    while chr(marker_code) in command:
        marker_code += 1
        if marker_code > 0xF8FF:
            raise ValueError("no unused quote marker")
    marker = chr(marker_code)

    protected: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote == '"' and index + 1 < len(command):
            following = command[index + 1]
            if following in {'"', "\\", "$", "`"}:
                protected.append(marker if following == '"' else command[index : index + 2])
                index += 2
                continue
        if char == "'" and quote != '"':
            quote = "" if quote else "'"
        elif char == '"' and quote != "'":
            quote = "" if quote else '"'
        protected.append(char)
        index += 1

    lexer = shlex.shlex("".join(protected), posix=False, punctuation_chars=punctuation_chars)
    lexer.whitespace_split = True
    lexer.whitespace = whitespace
    lexer.commenters = ""
    return [token.replace(marker, '\\"') for token in lexer]
