"""Spoken-only wording. The authoritative text response is never changed.

Only the renderer's exact terminal reference sentence is rewritten for speech; any other
text, including UUIDs written elsewhere, is spoken verbatim.
"""

import re

_REFERENCE = re.compile(
    r" Reference: [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.\Z"
)
SPOKEN_REFERENCE = " The reference is available in the chat."


def spoken_text(response_text: str) -> str:
    return _REFERENCE.sub(SPOKEN_REFERENCE, response_text, count=1)
