import sys

from lu_machinery.publish.github import main, repo_admin_detach

if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        repo_admin_detach(sys.argv[1:])
        raise
    # After repo-create or repo-transfer only: interpreter shutdown cannot turn the settled code into 120.
    # An uncaught exception keeps both streams, so its traceback stays visible.
    repo_admin_detach(sys.argv[1:])
    raise SystemExit(code)
