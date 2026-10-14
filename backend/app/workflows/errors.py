"""Safe application errors; no user text or SQL is included."""


class WorkflowError(ValueError):
    pass


class StaleRevision(WorkflowError):
    pass
