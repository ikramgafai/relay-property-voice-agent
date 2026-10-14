from app.tools.schemas import ErrorCode


class ToolFailure(Exception):
    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class StorageFailure(Exception):
    """Safe error: transaction outcome may require reconciliation."""
