import json

import httpx
import pytest
from app.core.config import Settings
from app.providers.llm.openai_compatible import OpenAICompatibleRoutingProvider
from app.providers.llm.protocol import ModelContractError, ModelUnavailable
from app.routing.model import ModelRoutingRequest, ModelRoutingResponse
from app.routing.router import ServiceRouter
from app.routing.schemas import RoutingRequest
from pydantic import SecretStr, ValidationError

from evaluations.catalogs import catalogs

from .support.routing import response


def settings() -> Settings:
    return Settings(
        routing_model_base_url="https://model.example/v1",
        routing_model_name="test-model",
        routing_model_api_key=SecretStr("test-secret"),
    )


def request() -> ModelRoutingRequest:
    return ModelRoutingRequest.from_request(
        RoutingRequest(utterance="I need someone", property_context=catalogs()["palm"])
    )


async def test_adapter_request_and_strict_output() -> None:
    incoming = request()
    selected = incoming.services[0]
    answer = response("ROUTE", selected.property_service_id, selected.scenarios[0].id)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        body = json.loads(req.content)
        assert str(req.url) == "https://model.example/v1/chat/completions"
        assert req.headers["authorization"] == "Bearer test-secret"
        assert body["response_format"]["json_schema"]["strict"] is True
        schema = body["response_format"]["json_schema"]["schema"]
        assert set(schema["required"]) == set(ModelRoutingResponse.model_fields)
        assert schema["additionalProperties"] is False
        assert "emergency_maintenance" not in body["messages"][1]["content"]
        assert body["model"] == "test-model" and body["stream"] is False
        assert "tools" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": answer.model_dump_json()}}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAICompatibleRoutingProvider(settings(), client).interpret(incoming)
    assert result == answer and len(calls) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "invalid_json",
        "missing_choices",
        "empty_choices",
        "prose",
        "extra_field",
        "refusal",
        "truncated",
        "tool_call",
        "http_error",
        "timeout",
        "redirect",
    ],
)
async def test_adapter_errors_are_safe_and_not_retried(kind: str) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        message: dict[str, object] = {"content": response().model_dump_json()}
        finish = "stop"
        if kind == "invalid_json":
            return httpx.Response(200, text="not json secret")
        if kind == "missing_choices":
            return httpx.Response(200, json={})
        if kind == "empty_choices":
            return httpx.Response(200, json={"choices": []})
        if kind == "prose":
            message["content"] = "I would pick maintenance, secret"
        if kind == "extra_field":
            message["content"] = json.dumps(
                {
                    **response().model_dump(mode="json"),
                    "clarification_question": "Ask about an unavailable service",
                }
            )
        if kind == "refusal":
            message["refusal"] = "secret refusal"
        if kind == "truncated":
            finish = "length"
        if kind == "tool_call":
            message["tool_calls"] = [{"name": "execute"}]
        if kind == "http_error":
            return httpx.Response(429, text="secret throttle")
        if kind == "redirect":
            return httpx.Response(307, headers={"location": "https://untrusted.example"})
        if kind == "timeout":
            raise httpx.ReadTimeout("secret timeout", request=req)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": finish, "message": message}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((ModelContractError, ModelUnavailable)) as caught:
            await OpenAICompatibleRoutingProvider(settings(), client).interpret(request())
    assert "secret" not in str(caught.value) and len(calls) == 1


async def test_real_adapter_malformed_response_abstains_through_router() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    ) as client:
        result = await ServiceRouter(OpenAICompatibleRoutingProvider(settings(), client)).route(
            RoutingRequest(utterance="unmatched", property_context=catalogs()["palm"])
        )
    assert result.decision == "ABSTAINED" and result.reason_code == "MODEL_CONTRACT_VIOLATION"


@pytest.mark.parametrize(
    "config",
    [
        {"routing_model_name": "model"},
        {"routing_model_base_url": "https://model.example/v1"},
        {"routing_model_api_key": "secret"},
        {"routing_model_timeout_seconds": 0},
        {"routing_model_timeout_seconds": 61},
        {"routing_model_max_completion_tokens": 1},
        {"routing_model_name": " "},
        {"routing_model_base_url": "http://remote.example/v1", "routing_model_name": "m"},
        {
            "routing_model_base_url": "https://user:secret@remote.example/v1",
            "routing_model_name": "m",
        },
        {
            "routing_model_base_url": "https://remote.example/v1?key=secret",
            "routing_model_name": "m",
        },
    ],
)
def test_routing_settings_validation(config: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(config)


def test_default_model_unconfigured_and_local_endpoint_supported() -> None:
    assert Settings().routing_model_base_url is None
    local = Settings(routing_model_base_url="http://127.0.0.1:8080/v1", routing_model_name="local")
    assert local.routing_model_api_key is None
    assert "test-secret" not in repr(settings())
