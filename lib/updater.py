"""Validate a fetched Brigade revision before advancing its linked installation."""

from __future__ import annotations

import tempfile
from pathlib import Path

from worker_control import WorkerError, require_ok, run


def git(repository: Path, *arguments: str) -> str:
    return require_ok(run(["git", *arguments], cwd=str(repository)), "Git " + arguments[0]).strip()


def installed_head(repository: Path) -> str:
    root = Path(git(repository, "rev-parse", "--show-toplevel")).resolve()
    if root != repository.resolve():
        raise WorkerError("The Brigade installation must be the root of a Git checkout.")
    branch = git(repository, "branch", "--show-current")
    if branch != "master":
        raise WorkerError(f"Brigade update requires master; this checkout is on {branch or 'a detached revision'}.")
    changes = git(repository, "status", "--porcelain", "--untracked-files=all")
    if changes:
        raise WorkerError("Brigade has local edits. Save or commit them before updating:\n" + changes)
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply"):
        path = Path(git(repository, "rev-parse", "--git-path", marker))
        if not path.is_absolute():
            path = repository / path
        if path.exists():
            raise WorkerError("Finish the current Git operation before updating Brigade.")
    return git(repository, "rev-parse", "HEAD")


def update(repository: Path) -> None:
    repository = repository.resolve()
    previous = installed_head(repository)
    print("Fetching Brigade master from origin...", flush=True)
    git(repository, "fetch", "--no-tags", "origin", "+refs/heads/master:refs/remotes/origin/master")
    target = git(repository, "rev-parse", "refs/remotes/origin/master^{commit}")
    if target == previous:
        print(f"Brigade is already up to date at {previous[:12]}.")
        return
    ancestry = run(["git", "merge-base", "--is-ancestor", previous, target], cwd=str(repository))
    if ancestry.returncode == 1:
        raise WorkerError("Local master has commits outside origin/master. Resolve the history before updating.")
    require_ok(ancestry, "Check update history")

    print(f"Checking candidate {target[:12]} in a temporary checkout...", flush=True)
    with tempfile.TemporaryDirectory(prefix="brigade-update-") as directory:
        candidate = Path(directory) / "repo"
        require_ok(run(["git", "clone", "--quiet", "--no-local", "--no-checkout", str(repository), str(candidate)]), "Create validation checkout")
        # The new revision may exist only on a remote-tracking ref in the installation.
        git(candidate, "fetch", "--quiet", "--no-tags", "origin", target)
        git(candidate, "checkout", "--quiet", "--detach", target)
        if not (candidate / "scripts/check.sh").is_file():
            raise WorkerError("The candidate has no scripts/check.sh. The installed checkout is unchanged.")
        checked = run(["bash", "scripts/check.sh"], cwd=str(candidate))
        if checked.returncode:
            detail = (checked.stderr or checked.stdout).strip()
            raise WorkerError("Candidate checks failed. The installed checkout is unchanged.\n" + detail)

    # Validation can take time. Refuse to proceed if someone edited or moved the checkout.
    if installed_head(repository) != previous:
        raise WorkerError("The installed revision changed during validation. Run brigade update again.")
    print(f"Updating Brigade from {previous[:12]} to {target[:12]}...", flush=True)
    git(repository, "merge", "--ff-only", "--no-autostash", "--no-overwrite-ignore", target)
    print(f"Brigade updated to {target[:12]}. Start a fresh agent session to load the updated instructions.")
