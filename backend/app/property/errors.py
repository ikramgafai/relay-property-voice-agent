from app.core.errors import RelayError


class PropertyContextError(RelayError):
    """Missing, inactive, or inconsistent property context; never guess a replacement."""
