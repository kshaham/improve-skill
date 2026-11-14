"""Shared switches for the test suite.

IMPROVE_TEST_SLOW=1 runs the soak, scale, and performance suites (minutes each); they are
skipped by default so the fast suite stays reliable on a loaded machine or CI runner.
IMPROVE_TEST_SPEED_FACTOR scales every wall-clock ceiling (2 doubles them).
Git is isolated from the developer's global and system configuration so signing, hooks,
and templates on the host never reach fixture repositories. The host's own IMPROVE_*
settings (an exported IMPROVE_FORMAT=json, a short IMPROVE_GRAPH_LOCK_WAIT) are removed for
the same reason, and the cross-project learning store points at a throwaway directory so no
test writes to the developer's real one.
"""
import atexit
import os
from pathlib import Path
import tempfile
import unittest

SLOW = os.environ.get("IMPROVE_TEST_SLOW") == "1"
slow = unittest.skipUnless(SLOW, "set IMPROVE_TEST_SLOW=1 to run soak, scale, and performance tests")
SPEED = float(os.environ.get("IMPROVE_TEST_SPEED_FACTOR", "1") or "1")


def budget(seconds):
    """A wall-clock ceiling scaled for the machine running the tests."""
    return seconds * SPEED


_GIT_HOME = None


def isolate_git():
    """Point git at an empty global config with a fixed identity for this process and its children."""
    global _GIT_HOME
    if _GIT_HOME is not None:
        return
    _GIT_HOME = tempfile.TemporaryDirectory(prefix="improve-test-git ")
    atexit.register(_GIT_HOME.cleanup)
    config = Path(_GIT_HOME.name) / "gitconfig"
    config.write_text("[user]\n\tname = Improve tests\n\temail = tests@example.invalid\n"
                      "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n[core]\n\thooksPath = /dev/null\n"
                      "[init]\n\tdefaultBranch = main\n")
    os.environ["GIT_CONFIG_GLOBAL"] = str(config)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ.pop("GIT_DIR", None)
    os.environ.pop("GIT_WORK_TREE", None)


def fts5_available():
    import sqlite3
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def isolate_environment():
    """Drop host IMPROVE_* settings (test knobs excepted) and isolate the learning store."""
    for key in [key for key in os.environ if key.startswith("IMPROVE_") and not key.startswith("IMPROVE_TEST_")]:
        del os.environ[key]
    os.environ["IMPROVE_LEARNING_HOME"] = str(Path(_GIT_HOME.name) / "learning-home")


isolate_git()
isolate_environment()
