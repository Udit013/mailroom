"""Crash injection for tests. Does nothing unless MAILROOM_FAILPOINT is set.

Format: MAILROOM_FAILPOINT=<name>:<action>, with MAILROOM_FAILPOINT_DIR for marker files.
"""

import os
import signal
import time
from pathlib import Path

from mailroom.stages import PermanentStageError, TransientStageError


def hit(name: str) -> None:
    spec = os.environ.get("MAILROOM_FAILPOINT")
    if spec is None:
        return
    target, action = spec.split(":")
    if target != name:
        return

    marker_dir = Path(os.environ["MAILROOM_FAILPOINT_DIR"])
    reached = marker_dir / "reached"
    first_time = not reached.exists()
    reached.write_text(str(os.getpid()))

    if action == "hang":
        time.sleep(3600)
    elif action == "sigkill" or (action == "sigkill_once" and first_time):
        os.kill(os.getpid(), signal.SIGKILL)
    elif action == "transient":
        raise TransientStageError(os.environ.get("MAILROOM_FAILPOINT_MESSAGE", name))
    elif action == "permanent":
        raise PermanentStageError("INJECTED")
