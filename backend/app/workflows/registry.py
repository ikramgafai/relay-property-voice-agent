"""Capability bindings, NOT the set of valid routable services."""

from app.property.schemas import EnabledServiceContext
from app.workflows.definitions import DEFINITIONS, Definition
from app.workflows.schemas import WorkflowKey


def resolve_workflow(service: EnabledServiceContext) -> Definition | None:
    # Explicit property configuration overrides conventional demo bindings. Unknown values
    # deliberately disable support; they never cause a fallback to an unrelated capability.
    key = service.configuration.get("workflow_key", service.key)
    try:
        capability = WorkflowKey(key) if isinstance(key, str) else None
    except ValueError:
        return None
    return DEFINITIONS.get(capability) if capability else None
