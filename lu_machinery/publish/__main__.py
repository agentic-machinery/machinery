import argparse
import contextlib
import os
import sys

from lu_machinery.publish import github


def _repo_admin_argv(argv):
    """Recognize the repo-admin subcommands even when argparse cannot probe stdout."""
    try:
        with (
            open(os.devnull, "w", encoding="utf-8") as devnull,
            contextlib.redirect_stdout(devnull),
            contextlib.redirect_stderr(devnull),
        ):
            probe = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
            probe.add_argument("-h", "--help", action="store_true")
            probe.add_argument("rest", nargs=argparse.PARSER)
            known, _ = probe.parse_known_args(argv)
    except Exception:
        return False
    return not known.help and bool(known.rest) and known.rest[0] in github.REPO_ADMIN_VERBS


def _repo_admin_cli(argv):
    try:
        try:
            args = github._parse_args(argv)
        except Exception:
            # Argparse probes and writes to the streams. Only retry parsing, before any publication.
            with (
                open(os.devnull, "w", encoding="utf-8") as devnull,
                contextlib.redirect_stdout(devnull),
                contextlib.redirect_stderr(devnull),
            ):
                args = github._parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):
                stream.flush()
    else:
        try:
            code = github._run_args(args)
        except Exception:
            # The invocation handled its own errors. Its refusal message could not be written.
            code = 2

    # The streams have already been flushed. Prevent interpreter shutdown from replacing the result with 120.
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives until exit
    return code


if __name__ == "__main__":
    argv = sys.argv[1:]
    raise SystemExit(_repo_admin_cli(argv) if _repo_admin_argv(argv) else github.main(argv))
