"""Git refs and disposable local run trees; commands never interpolate refs into a shell."""
import fnmatch
import shlex
import signal
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


class GitError(ValueError):
    pass


def git(repo, *args) -> str:
    p = subprocess.run(["git", "-C", str(Path(repo).expanduser()), *args], capture_output=True, text=True)
    if p.returncode:
        raise GitError(p.stderr.strip() or f"git {' '.join(args)} failed")
    return p.stdout


def resolve(repo, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()


def metadata(repo, ref: str, base: str = "HEAD") -> dict:
    sha, base_sha = resolve(repo, ref), resolve(repo, base)
    return {"ref": sha, "base_ref": base_sha,
            "change": git(repo, "diff", "--stat", "--no-color", "--no-ext-diff", f"{base_sha}...{sha}").rstrip()}


def denied(repo, base: str, ref: str, patterns: list[str]) -> str | None:
    """The first path the diff base...ref touches that matches a glob; no rename detection, so a move checks both names."""
    paths = git(repo, "diff", "--name-only", "-z", "--no-renames", f"{base}...{ref}").split("\0")
    return next((p for p in paths if p and any(fnmatch.fnmatchcase(p, g) for g in patterns)), None)


def command(cmd: str, tree) -> str:
    return cmd.replace("{tree}", shlex.quote(str(tree)))


def environment(tree, ref: str) -> dict:
    return {"PIT_TREE": str(tree), "PIT_REF": ref}


@contextmanager
def repository(repo):
    """A local repo, or a temporary mirror for validating refs of a remote lane."""
    path = Path(repo).expanduser()
    if path.is_dir():
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="pit-refs-") as tmp:
        path = Path(tmp) / "repo.git"
        p = subprocess.run(["git", "clone", "--mirror", "--", repo, str(path)], capture_output=True, text=True)
        if p.returncode:
            raise GitError(p.stderr.strip())
        yield path


@contextmanager
def worktree(repo, ref: str):
    """A detached worktree at ref in a temp dir; the dir is deleted and git's record of it pruned on any exit but SIGKILL."""
    repo = Path(repo).expanduser().resolve()
    old = {}
    def interrupted(sig, frame):
        raise SystemExit(128 + sig)
    if threading.current_thread() is threading.main_thread():
        old = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        with tempfile.TemporaryDirectory(prefix="pit-run-", ignore_cleanup_errors=True) as tmp:
            tree = Path(tmp).resolve() / "tree"
            git(repo, "worktree", "add", "--detach", str(tree), ref)
            yield tree
    finally:
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True)
        for sig, handler in old.items():
            signal.signal(sig, handler)
