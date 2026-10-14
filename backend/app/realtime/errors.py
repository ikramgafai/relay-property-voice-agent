from app.agent.errors import TurnError
from app.realtime.schemas import Code


class RealtimeError(Exception):
    def __init__(self, code: Code) -> None:
        self.code = code
        super().__init__(code.value)


class Backpressure(Exception):
    """Critical event delivery failed; close explicitly and require durable replay."""


def application_code(error: TurnError) -> Code:
    return {
        "SESSION_NOT_FOUND": Code.SESSION_NOT_FOUND,
        "SESSION_BUSY": Code.SESSION_BUSY,
        "SESSION_INACTIVE": Code.SESSION_UNAVAILABLE,
        "SESSION_CONTEXT_CHANGED": Code.SESSION_UNAVAILABLE,
        "TURN_IDENTITY_CONFLICT": Code.TURN_CONFLICT,
        "STALE_TURN": Code.TURN_CONFLICT,
        "PREVIOUS_TURN_INCOMPLETE": Code.TURN_CONFLICT,
    }.get(error.code, Code.INTERNAL_ERROR)
