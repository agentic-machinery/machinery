"""Quote-preserving Bash word splitting for shared hook guards.

The guards need each word's original spelling, quotes included. Non-POSIX
shlex keeps that spelling but honours a quote only at the start of a token, so
a quote opening inside a word (`it'\\''s`, `a' 'b`) flipped quote parity.
split_quote_preserving tracks Bash quoting itself instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable

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
    """Find only here-doc words whose literal closer is unambiguous (contract rule 1).

    Return (closer, strip_tabs, quoted), or None for any ambiguous opener.
    Callers then keep every remaining line visible to their command scanner.
    """
    openers = _heredoc_openers(line, initial_quote=initial_quote)
    return None if openers is None else [(closer, strip_tabs, quoted) for closer, strip_tabs, quoted, _ in openers]


def _heredoc_openers(line: str, *, initial_quote: str = "") -> list[tuple[str, bool, bool, int]] | None:
    """skippable_heredoc_delimiters, with the offset of each `<<` operator."""
    delimiters: list[tuple[str, bool, bool, int]] = []
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

        operator = index
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
        closer = heredoc_closer(line[start:index])
        if closer is None:
            ambiguous = True
        else:
            delimiters.append((closer[0], strip_tabs, closer[1], operator))
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


# Here-document contract, one copy for every guard. preprocess_shell_command
# applies it through strip_skippable_heredoc_bodies; shell_redirects'
# preprocess_branch_command refuses whatever it would leave visible.
#
# 1. Delimiter. A word with no `$` or backtick whose quotes close is static:
#    its closer is the word after Bash quote removal, and a quote or backslash
#    anywhere in it makes the body literal. Any other word is ambiguous, and
#    every line after its opener stays visible, as before.
# 2. Readers. Follow the body's text from the command that owns its `<<`
#    (_reads_as_data). A pass reader (HEREDOC_PASS_READERS: cat, tee, head
#    and so on) hands it to whatever reads its output: later pipe stages, the
#    pipes after an enclosing group or compound command, the command a
#    substitution is a word of (echo and printf print it on), or the command
#    that reads a `<(` file. A sink ends it: HEREDOC_SINK_READERS, a git
#    subcommand in _GIT_READERS, gh api/issue/pr/release (also behind env or
#    timeout), or a Python or node program. The body is data when every
#    reader on that path passes or sinks and is named by a literal word. Any
#    other reader keeps it visible: a shell, any other program (sort
#    --compress-program, awk system() and xargs run their input), an
#    expansion such as $PY, an unreadable word, or an output process
#    substitution `>(`. Unknown fails closed. Python and node programs are
#    opaque to the shell guards, as their inline programs (python -c) already
#    are; a guard with its own program reader keeps it.
# 3. A data body is inert: it is dropped, and an unquoted body keeps only its
#    command substitutions, which still run.
# 4. A visible body is read as a script of its own. It must close every quote,
#    group and nested here-document before its closer, or the command is
#    refused (ShellPreprocessLimit): Bash ends the body at the closer, while a
#    guard's lexer would carry the open quote over the commands that follow.
#
# So `{ cat <<X; } | bash` and `eval $(cat <<X ...)` keep the body visible.
# Not covered: text written to a file or a variable and run later, the same
# limit as any script file.

# Text readers whose output carries their input on, and readers that end it.
HEREDOC_PASS_READERS = frozenset({"base64", "cat", "column", "cut", "grep", "head", "jq", "tail", "tee", "tr", "uniq"})
HEREDOC_SINK_READERS = frozenset({":", "echo", "let", "md5sum", "printf", "read", "sha1sum", "sha256sum", "true", "wc"})
_PROGRAM_READER = re.compile(r"(?:python(?:[0-9]+(?:\.[0-9]+)*)?|node)\Z")
_GIT_READERS = frozenset({"am", "apply", "commit", "commit-tree", "hash-object", "merge", "notes", "tag"})
_GH_READERS = frozenset({"api", "issue", "pr", "release"})
_DURATION = re.compile(r"[0-9]+(?:\.[0-9]*)?[smhd]?\Z")
_MAX_HEREDOC_DEPTH = 16
_REDIRECT_OPERATORS = frozenset({"<", ">", ">>", ">|", "<>", "&>", "&>>", ">&", "<&", "<<<", "<<", "<<-"})
_PIPELINE_ENDS = frozenset({";", "&", "&&", "||", ";;", "(", ")", "\n"})
_STAGE_PREFIXES = frozenset({"!", "time", "if", "then", "elif", "else", "while", "until", "do", "{"})
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")


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
    """Contract rule 1: the closer and literal flag of a static delimiter word."""
    closer = static_word(word)
    return None if closer is None or not closer[0] else closer


def _line_words(line: str) -> list[tuple[str, int]] | None:
    """Whole shell words and single operators of one line, with their offsets."""
    try:
        tokens = split_quote_preserving(line, punctuation_chars="();<>|&\n", whitespace=" \t")
    except ValueError:
        return None
    words: list[tuple[str, int]] = []
    end = 0
    joinable = False
    for token in tokens:
        start = line.find(token, end)
        if start < 0:
            return None
        operator = set(token) <= _OPERATOR_CHARS
        if operator:
            offset = start
            for part in split_operator_run(token):
                words.append((part, offset))
                offset += len(part)
        elif joinable and start == end:
            words[-1] = (words[-1][0] + token, words[-1][1])
        else:
            words.append((token, start))
        end = start + len(token)
        joinable = not operator
    return words


def _reader(stage: list[str]) -> str | None:
    """Contract rule 2 for one simple command's words.

    "pass" when its output carries the body on, "sink" when the body ends
    there, None for any other reader.
    """
    words: list[str] = []
    index = 0
    while index < len(stage):
        if stage[index] in _REDIRECT_OPERATORS:
            index += 2
            continue
        words.append(stage[index])
        index += 1
    index = 0
    while index < len(words) and words[index] in _STAGE_PREFIXES:
        index += 1
    while index < len(words) and _ASSIGNMENT.match(words[index]):
        index += 1
    # Nothing runs at all: Bash reads the body and discards it.
    return "sink" if index >= len(words) else _program_reader(words[index:])


def _program_name(word: str) -> str | None:
    """The program a command word runs: its literal last path segment.

    A directory may expand (`$VENV/bin/python`); the name itself may not.
    """
    value = static_word(word)
    if value is not None:
        return value[0].rsplit("/", 1)[-1] or None
    if "/" not in word:
        return None
    tail = word.rsplit("/", 1)[1]
    if word.startswith('"') and tail.endswith('"'):
        tail = tail[:-1]
    if any(char in tail for char in "\"'"):
        return None
    value = static_word(tail)
    return value[0] if value is not None else None


def _program_reader(words: list[str]) -> str | None:
    """_reader for an argv. The command name must be literal, and so must every
    argument a decision reads."""
    name = _program_name(words[0])
    if name is None:
        return None
    if name in HEREDOC_PASS_READERS:
        return "pass"
    if name in HEREDOC_SINK_READERS or _PROGRAM_READER.match(name):
        return "sink"
    args: list[str] = []
    for word in words[1:]:
        value = static_word(word)
        if value is None:
            break
        args.append(value[0])
    rest = words[1:]
    if name == "git":
        # A configuration value cannot rename a built-in subcommand.
        while len(rest) >= 2 and rest[0] in {"-c", "-C"}:
            rest = rest[2:]
        subcommand = static_word(rest[0]) if rest else None
        return "sink" if subcommand and subcommand[0] in _GIT_READERS else None
    if name == "gh":
        return "sink" if args and args[0] in _GH_READERS else None
    if name == "env":
        while args:
            if _ASSIGNMENT.match(args[0]) or args[0] in {"-i", "--ignore-environment"}:
                skip = 1
            elif args[0] in {"-u", "--unset", "-C", "--chdir"}:
                skip = 2
            elif args[0].startswith(("--unset=", "--chdir=")):
                skip = 1
            else:
                break
            args, rest = args[skip:], rest[skip:]
        return _program_reader(rest) if args and not args[0].startswith("-") else None
    if name == "timeout":
        while args and args[0] in {"--foreground", "--preserve-status", "-v", "--verbose"}:
            args, rest = args[1:], rest[1:]
        return _program_reader(rest[1:]) if len(args) >= 2 and _DURATION.match(args[0]) else None
    return None


_GROUP_OPENERS = frozenset({"{", "do", "if"})
_GROUP_CLOSERS = frozenset({"}", "done", "fi"})
_COMPOUND_WORDS = frozenset({"{", "(", "while", "until", "for", "select", "if", "case", "function"})
_PIPES = frozenset({"|", "|&"})
_STAGE_ENDS = _PIPES | _PIPELINE_ENDS


def _reads_as_data(text: str, operators: list[int]) -> list[bool]:
    """Contract rule 2 for each `<<` at an offset in `text`, the command lines.

    A body's readers are the stage that owns its `<<`, every later stage of
    that pipeline, and, for each group around that pipeline, whatever reads
    the group's output: the stages a group or compound command pipes into, or
    the command a command substitution is a word of or that reads a `<(`
    file. An output process substitution `>(` on that path fails closed.
    """
    words = _line_words(text)
    if words is None or any(word in {"case", "esac", "coproc"} for word, _ in words):
        # Case patterns end in `)`, so groups cannot be matched safely.
        return [False] * len(operators)
    parents: list[int] = []
    closes: dict[int, int] = {}
    stack: list[int] = []
    command_start = True
    for index, (word, _) in enumerate(words):
        if word == ")" or (command_start and word in _GROUP_CLOSERS):
            if not stack:
                return [False] * len(operators)
            closes[stack.pop()] = index
        parents.append(stack[-1] if stack else -1)
        if word == "(" or (command_start and word in _GROUP_OPENERS):
            stack.append(index)
        command_start = word in _STAGE_ENDS or word in {"{", "do", "if", "then", "else", "elif", "!", "while", "until"}

    def kind(opener: int) -> str:
        word, offset = words[opener]
        if word != "(":
            return "group"
        if opener and words[opener - 1][0] in {"<", ">"} and words[opener - 1][1] + 1 == offset:
            return "input" if words[opener - 1][0] == "<" else "output"
        return "command" if offset and text[offset - 1] == "$" else "group"

    # `$(`, `<(` and `>(` are words of their stage, not stage boundaries.
    inline = {index for index in closes.keys() | set(stack) if kind(index) != "group"}
    inline |= {closes[index] for index in inline if index in closes}

    def boundary(position: int, level: int) -> bool:
        return parents[position] == level and words[position][0] in _STAGE_ENDS and position not in inline

    def within(position: int, group: int) -> bool:
        """Whether the word at `position` sits inside `group` (-1: the top)."""
        if group < 0:
            return True
        parent = parents[position]
        while parent >= 0:
            if parent == group:
                return True
            parent = parents[parent]
        return False

    def stage(index: int) -> tuple[int, int]:
        level = parents[index]
        start = index
        while start > 0 and within(start - 1, level) and not boundary(start - 1, level):
            start -= 1
        end = index
        while end < len(words) and within(end, level) and not boundary(end, level):
            end += 1
        return start, end

    def own_words(start: int, end: int) -> list[str]:
        return [
            words[position][0]
            for position in range(start, end)
            if parents[position] == parents[start] and position not in inline
        ]

    def stage_reader(start: int, end: int, *, owner: bool) -> str | None:
        if any(words[position][0] == "(" and kind(position) == "output" for position in range(start, end)):
            return None  # its output may go into the process substitution
        own = own_words(start, end)
        if not owner and own and own[0] in _COMPOUND_WORDS:
            return None  # a loop or group reading the pipe runs more than one command
        return _reader(own)

    def onward(end: int) -> bool | None:
        """Follow the body's text through pipes from a stage end.

        True when a sink takes it, False for any other reader, and None
        while it still flows when the pipeline ends.
        """
        while end < len(words) and words[end][0] in _PIPES:
            following = end + 1
            while following < len(words) and words[following][0] == "\n":
                following += 1
            if following >= len(words):
                return False  # no reader follows the pipe
            start, end = stage(following)
            reader = stage_reader(start, end, owner=False)
            if reader != "pass":
                return reader == "sink"
        return None

    heredocs = {offset: index for index, (word, offset) in enumerate(words) if word in {"<<", "<<-"}}
    flags: list[bool] = []
    for operator in operators:
        target = heredocs.get(operator)
        if target is None:
            flags.append(False)
            continue
        start, end = stage(target)
        reader = stage_reader(start, end, owner=True)
        flows = None if reader == "pass" else reader == "sink"
        if flows is None:
            flows = onward(end)
        group = parents[target]
        while flows is None and group >= 0:
            if kind(group) == "output" or group not in closes:
                flows = False
            elif kind(group) == "input":
                # The command around `<(` reads the body's text as a file.
                start, end = stage(group)
                reader = stage_reader(start, end, owner=True)
                flows = onward(end) if reader == "pass" else reader == "sink"
            elif kind(group) == "command":
                # The body's text becomes words of the command around the
                # substitution: echo and printf print it on, others take it.
                start, end = stage(group)
                reader = stage_reader(start, end, owner=True)
                own = own_words(start, end)
                passes = reader is not None and bool(own) and own[0] in {"echo", "printf"}
                flows = onward(end) if passes else reader is not None
            else:
                flows = onward(closes[group] + 1)
            group = parents[group]
        # Text still flowing at the top level is printed: it is data.
        flags.append(flows is not False)
    return flags


def _heredoc_script(body: str, quoted: bool) -> str:
    """The text a shell reader receives: an unquoted body loses its escapes."""
    if quoted:
        return body
    return re.sub(r"\\\n|\\([\\$`])", lambda match: match.group(1) or "", body)


def _script_closes(script: str) -> bool:
    """Contract rule 4: no quote, group or substitution is left open."""
    stack: list[str] = []
    word_start = True
    index = 0
    while index < len(script):
        char = script[index]
        top = stack[-1] if stack else ""
        if top == "'":
            if char == "'":
                stack.pop()
            index += 1
            continue
        if char == "\\":
            if index + 1 >= len(script):
                return False
            index += 2
            word_start = False
            continue
        if top == '"':
            if char == '"':
                stack.pop()
            elif char == "`" or script.startswith("$(", index) or script.startswith("${", index):
                pass
            else:
                index += 1
                continue
        if top == "#":
            if char == "\n":
                stack.pop()
                word_start = True
            index += 1
            continue
        if char == "#" and word_start and top != '"':
            stack.append("#")
            index += 1
            continue
        opening = next((op for op in ("$((", "$(", "${", "$[", "((") if script.startswith(op, index)), "")
        if opening:
            stack.append(opening)
            index += len(opening)
            word_start = True
            continue
        if char == "`":
            if top == "`":
                stack.pop()
            else:
                stack.append("`")
        elif char in "'\"" and top != '"':
            stack.append(char)
        elif char == "(":
            stack.append("(")
        elif char == ")" and top in {"$(", "("}:
            stack.pop()
        elif script.startswith("))", index) and top in {"$((", "(("}:
            stack.pop()
            index += 2
            word_start = False
            continue
        elif (char == "}" and top == "${") or (char == "]" and top == "$["):
            stack.pop()
        word_start = char in " \t\n;&|()"
        index += 1
    return not [entry for entry in stack if entry != "#"]


def strip_skippable_heredoc_bodies(
    command: str,
    *,
    opener_transform: Callable[[str], str] | None = None,
    body_substitutions: Callable[[str], list[str] | None] | None = None,
) -> str:
    """Apply the here-document contract above for every hook guard.

    Bash retains carriage returns in here-doc delimiters. A command containing
    one is left intact, so a mismatched closer can never conceal later commands.
    """
    return _heredoc_view(command, opener_transform, body_substitutions, 0)[0]


_Body = tuple[int, int, bool, bool, bool]  # start, closer line, strip_tabs, quoted, data


def _heredoc_plan(
    command: str, transform: Callable[[str], str]
) -> tuple[list[str], list[tuple[int, bool, list[_Body]]], bool]:
    """Find each body, and apply contract rule 2 to it.

    Return the lines, one entry per line Bash reads as a command (its index,
    whether a quote is open after it, its bodies), and whether every opener
    was unambiguous and every body closed.
    """
    lines = command.split("\n")
    plan: list[tuple[int, bool, list[list]]] = []
    commands: list[str] = []
    targets: list[tuple[int, int]] = []
    complete = True
    index = 0
    quote = ""
    while index < len(lines):
        opener = transform(lines[index])
        line_number = index
        index += 1
        openers = _heredoc_openers(opener, initial_quote=quote)
        quote = _quote_after(opener, quote)
        commands.append(opener)
        if openers is None:
            complete = False
            plan.append((line_number, bool(quote), []))
            break
        bodies: list[list] = []
        for closer, strip_tabs, quoted, operator in openers:
            start = index
            while index < len(lines) and (lines[index].lstrip("\t") if strip_tabs else lines[index]) != closer:
                index += 1
            if index == len(lines):
                complete = False
                break
            bodies.append([start, index, strip_tabs, quoted, not quote])
            targets.append((len(commands) - 1, operator))
            index += 1
        plan.append((line_number, bool(quote), bodies))
        if not complete:
            break
    if not complete:
        commands.extend(transform(line) for line in lines[index:])
    starts = [0]
    for line in commands:
        starts.append(starts[-1] + len(line) + 1)
    readers = iter(_reads_as_data("\n".join(commands), [starts[line] + operator for line, operator in targets]))
    for _, _, bodies in plan:
        for body in bodies:
            # A quote open after the opener hides where Bash starts the body.
            body[4] = next(readers) and body[4]
    return lines, [(line, quote_open, [tuple(body) for body in bodies]) for line, quote_open, bodies in plan], complete


def heredoc_readers_are_data(text: str, operators: list[int]) -> list[bool]:
    """Contract rule 2 for the `<<` operators at these offsets of `text`."""
    return _reads_as_data(strip_shell_comments(text), operators)


def heredoc_bodies_are_data(command: str) -> bool:
    """Whether contract rules 2 and 3 make every closed body in `command` inert.

    False when any closed body stays visible. A reader that drops bodies and
    cannot show a script must refuse then; ambiguous openers, carriage returns
    and unclosed bodies remain that reader's own decision.
    """
    if "<<" not in command:
        return True
    _, plan, _ = _heredoc_plan(command, strip_shell_comments)
    return all(body[4] for _, _, bodies in plan for body in bodies)


def _heredoc_view(
    command: str,
    opener_transform: Callable[[str], str] | None,
    body_substitutions: Callable[[str], list[str] | None] | None,
    depth: int,
) -> tuple[str, bool]:
    """The guard view of `command`, and whether every here-doc in it closed."""
    if "<<" not in command or "\r" in command:
        return command, "<<" not in command
    if depth >= _MAX_HEREDOC_DEPTH:
        raise ShellPreprocessLimit("nested here-document depth exceeded")
    lines, plan, complete = _heredoc_plan(command, opener_transform or (lambda text: text))
    kept: list[str] = []
    closed = complete
    for position, (line_number, quote_open, bodies) in enumerate(plan):
        kept.append(lines[line_number])
        if position == len(plan) - 1 and not complete:
            # An ambiguous opener or unclosed body keeps every later line.
            kept.extend(lines[line_number + 1 :])
            break
        for start, end, strip_tabs, quoted, data in bodies:
            body_lines = [line.lstrip("\t") for line in lines[start:end]] if strip_tabs else lines[start:end]
            body = "\n".join(body_lines)
            if data:
                if quoted or not body_substitutions:
                    continue
                extracted = body_substitutions(body)
                if extracted is None:
                    # Undecidable expansions keep the body and its closer.
                    kept.extend(lines[start : end + 1])
                    closed = False
                else:
                    kept.extend(extracted)
                continue
            if quote_open:
                # The opener's open quote would swallow the script in a lexer.
                raise ShellPreprocessLimit("here-document script follows an open quote")
            script, script_closed = _heredoc_view(
                _heredoc_script(body, quoted), opener_transform, body_substitutions, depth + 1
            )
            if not script_closed or not _script_closes(strip_shell_comments(script)):
                raise ShellPreprocessLimit("here-document script does not close before its delimiter")
            kept.extend(script.split("\n"))
    return "\n".join(kept), closed


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
