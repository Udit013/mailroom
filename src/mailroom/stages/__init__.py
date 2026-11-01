from mailroom.models import Stage

PIPELINE = (Stage.EXTRACT, Stage.CLASSIFY, Stage.MATCH, Stage.DECIDE)
NEXT_STAGE = dict(zip(PIPELINE, PIPELINE[1:], strict=False))
QUEUED_STAGES = (*PIPELINE, Stage.FILE)


class PermanentStageError(Exception):
    """Retrying cannot help, for example an invalid document."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TransientStageError(Exception):
    """Retrying may help."""
