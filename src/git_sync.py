"""
Commits the trade ledger and report, and pushes them to the remote.

Deliberately narrow: it stages only the paths it is given, so an unattended
run can never commit a half-finished code change that happens to be sitting in
the working tree on the box.
"""
import logging
import subprocess
from typing import List, Tuple

logger = logging.getLogger(__name__)

# The box runs unattended with no git identity configured, and setting a global
# one would also apply to any manual commits made there. Passing it per
# invocation keeps the attribution clear and the config untouched.
BOT_NAME = "strategy-bot"
BOT_EMAIL = "strategy-bot@localhost"

TIMEOUT = 120


def _run(args: List[str]) -> Tuple[int, str]:
    result = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT)
    return result.returncode, (result.stdout + result.stderr).strip()


def commit_and_push(paths: List[str], message: str, branch: str = "main") -> str:
    """
    Stage, commit and push `paths`. Returns a short human-readable status for
    the Telegram notification.

    Never force-pushes and never resets. If the remote has moved on, the rebase
    is attempted once and the whole thing is abandoned on conflict - losing a
    day of automated report regeneration is recoverable, and the next run will
    pick it up, whereas a forced push over someone's commit is not.
    """
    code, out = _run(["git", "add", "--"] + paths)
    if code != 0:
        return f"git add failed: {out[:200]}"

    # Nothing staged means nothing changed - a weekend, or a day with no trades.
    code, _ = _run(["git", "diff", "--cached", "--quiet", "--"] + paths)
    if code == 0:
        return "no changes to commit"

    code, out = _run([
        "git", "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
        "commit", "-m", message, "--"] + paths)
    if code != 0:
        return f"git commit failed: {out[:200]}"
    logger.info("Committed: %s", message)

    # The box and the laptop both push here, so the remote may be ahead.
    code, out = _run(["git", "pull", "--rebase", "--autostash", "origin", branch])
    if code != 0:
        _run(["git", "rebase", "--abort"])
        return (f"committed locally but rebase onto origin/{branch} failed, "
                f"not pushed: {out[:200]}")

    code, out = _run(["git", "push", "origin", branch])
    if code != 0:
        return f"committed locally but push failed: {out[:200]}"

    code, head = _run(["git", "rev-parse", "--short", "HEAD"])
    return f"pushed {head}"
