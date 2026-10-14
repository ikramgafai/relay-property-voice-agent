"""One-shot, cancellable routing interpretation; no tool execution or streaming."""

from typing import Protocol

from app.routing.model import ModelRoutingRequest, ModelRoutingResponse


class ModelUnavailable(Exception):
    """Transport, refusal or incomplete provider response; never contains raw provider data."""


class ModelContractError(Exception):
    """Malformed structured response; never contains raw provider data."""


class RoutingModelProvider(Protocol):
    async def interpret(self, request: ModelRoutingRequest) -> ModelRoutingResponse: ...
