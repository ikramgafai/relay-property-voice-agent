"""Allowlist projection; no resident/contact/unit or arbitrary configuration content."""

from app.property.schemas import PropertyContext
from app.providers.stt.schemas import SpeechContext


def speech_context(context: PropertyContext, previous_response: str | None = None) -> SpeechContext:
    names = [context.property.name]
    for service in context.enabled_services:
        names.append(service.display_name)
        names.extend(scenario.name for scenario in service.scenarios)
    terms = list(dict.fromkeys(name.strip() for name in names if 0 < len(name.strip()) <= 50))
    return SpeechContext(keyterms=terms[:50], agent_response=previous_response)
