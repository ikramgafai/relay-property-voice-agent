import pytest
from app.property.schemas import PropertyData
from pydantic import ValidationError


def test_timezone_validation_uses_iana_database() -> None:
    for timezone in ("America/New_York", "America/Los_Angeles", "UTC"):
        assert PropertyData(slug="demo", name="Demo", timezone=timezone).timezone == timezone
    with pytest.raises(ValidationError, match="Unknown IANA timezone"):
        PropertyData(slug="demo", name="Demo", timezone="Mars/Olympus_Mons")
