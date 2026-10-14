class TurnError(Exception):
    """Only fixed, safe codes cross the internal HTTP boundary."""

    def __init__(self, code: str, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)
