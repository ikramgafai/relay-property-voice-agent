import asyncio
from uuid import UUID

import pytest
from app.agent.schemas import AgentTurnRequest, AgentTurnResult
from app.api import voice
from app.core.config import Settings
from app.db.agent_models import AgentTurn
from app.db.models import ConversationSession, WorkflowRun
from app.db.operation_models import EmergencyIncident, Handoff, ToolCall
from app.realtime.media.protocol import AudioFrame
from app.realtime.schemas import AgentResponse, SpeechFinal
from app.realtime.speech import SpeechPipeline
from app.realtime.speech_context import speech_context
from app.tools.schemas import ToolResult
from app.workflows.schemas import ActionRequest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from tests.integration.test_agent import TextHarness
from tests.integration.test_agent import text_harness as text_harness  # noqa: F401
from tests.integration.test_realtime import realtime_app as realtime_app  # noqa: F401
from tests.integration.test_tools import harness as harness  # noqa: F401
from tests.support.realtime import Socket, live_relay_tasks
from tests.support.speech import FakeMedia, FakeSTT

pytestmark = pytest.mark.integration


async def test_spoken_flagship_and_media_reconnect(
    realtime_app: FastAPI, text_harness: TextHarness
) -> None:
    t = text_harness
    socket = Socket(realtime_app, t.source.session_id)
    await socket.start()
    session = await realtime_app.state.realtime_manager.get(t.source.session_id)
    assert session
    media, stt = FakeMedia(), FakeSTT()
    pipeline = SpeechPipeline(session, media, stt, speech_context(t.source.context))
    session.speech = pipeline
    pipeline.start()
    order = 0

    async def say(text: str, key: str | None = None) -> AgentResponse:
        nonlocal order
        if key:
            t.route(key)
        await media.frames.put(AudioFrame(data=b"\x00\x00" * 800, sequence=order))
        async with asyncio.timeout(3):
            await stt.audio.get()
        await stt.emit("FINAL", text, order)
        await stt.emit("FINAL", text, order)
        order += 1
        events = await socket.until_idle()
        result = next(e.payload for e in events if isinstance(e.payload, AgentResponse))
        assert isinstance(result, AgentResponse)
        async with asyncio.timeout(3):
            assert await stt.updates.get() == result.response_text
        return result

    try:
        await say("My AC isn't cooling.", "non_emergency_maintenance")
        await say("Answer: It blows warm air.")
        await say("Actually water is pouring out of it.", "emergency_maintenance")
        await say("Answer: Living room AC")
        await say("Answer: No known electrical contact")
        created = await say("Answer: Continuous leak")
        assert await t.source.count(EmergencyIncident) == 1
        # Media alone disconnects; the durable conversation/control plane survive.
        await media.frames.put(None)
        assert (await socket.receive()).event_type == "STT_FAILED"
        await pipeline.close()
        assert media.closed and stt.closed
        media, stt = FakeMedia(), FakeSTT()
        pipeline = SpeechPipeline(
            session,
            media,
            stt,
            speech_context(t.source.context, await t.store.last_response(t.source.session_id)),
        )
        session.speech = pipeline
        pipeline.start()
        await stt.opened.wait()
        assert stt.context and stt.context.agent_response == created.response_text
        t.route("emergency_maintenance", "electrical_hazard")
        await say("Wait, the water is getting close to an electrical outlet.")
        await say("I want to speak with someone.", "human_handoff")
        handoff = await say("Yes, please.")
        assert "No live transfer" in handoff.response_text
        assert await t.source.count(EmergencyIncident) == await t.source.count(Handoff) == 1
        async with t.source.database.session() as db:
            incident = (
                await db.scalars(
                    select(EmergencyIncident).where(
                        EmergencyIncident.session_id == t.source.session_id
                    )
                )
            ).one()
            assert incident.revision == 2 and str(incident.id) in created.response_text
            linked = (
                await db.scalars(select(Handoff).where(Handoff.session_id == t.source.session_id))
            ).one()
            assert linked.incident_id == incident.id
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(AgentTurn)
                    .where(AgentTurn.session_id == t.source.session_id)
                )
                == 9
            )
    finally:
        await socket.close()
    assert media.closed and stt.closed and not live_relay_tasks()


async def test_natural_spoken_flagship_without_answer_prefix(
    realtime_app: FastAPI, text_harness: TextHarness
) -> None:
    """ADR 008: natural replies by voice; every provider final is duplicated."""
    t = text_harness
    socket = Socket(realtime_app, t.source.session_id)
    await socket.start()
    session = await realtime_app.state.realtime_manager.get(t.source.session_id)
    assert session
    media, stt = FakeMedia(), FakeSTT()
    pipeline = SpeechPipeline(session, media, stt, speech_context(t.source.context))
    session.speech = pipeline
    pipeline.start()
    order = 0

    async def say(text: str) -> AgentResponse:
        nonlocal order
        await media.frames.put(AudioFrame(data=b"\x00\x00" * 800, sequence=order))
        async with asyncio.timeout(3):
            await stt.audio.get()
        await stt.emit("FINAL", text, order)
        await stt.emit("FINAL", text, order)
        order += 1
        events = await socket.until_idle()
        result = next(e.payload for e in events if isinstance(e.payload, AgentResponse))
        assert isinstance(result, AgentResponse)
        async with asyncio.timeout(3):
            assert await stt.updates.get() == result.response_text
        return result

    try:
        t.route("non_emergency_maintenance")
        assert (await say("My AC isn't cooling.")).response_text == (
            "What symptoms have you noticed?"
        )
        t.reply("WORKFLOW_ANSWER", "non_emergency_maintenance", "ac_not_cooling")
        assert "maintenance request" in (await say("It's only blowing warm air.")).response_text
        t.route("emergency_maintenance")
        assert (await say("Actually water is pouring out of it.")).response_text == (
            "Where is the issue happening?"
        )
        # The live misroute, now pinned to the emergency workflow's pending location field.
        t.reply("WORKFLOW_ANSWER", "non_emergency_maintenance", "ac_not_cooling")
        assert (await say("Living room AC.")).response_text == (
            "What hazards have you noticed nearby?"
        )
        t.reply("WORKFLOW_ANSWER")
        assert (await say("No, nothing electrical.")).response_text == (
            "How severe is the issue right now?"
        )
        t.reply("WORKFLOW_ANSWER", "emergency_maintenance", "severe_water_leak")
        created = await say("It's continuous.")
        assert created.response_text.startswith("Your emergency incident was created.")
        t.route("emergency_maintenance", "electrical_hazard")
        updated = await say("Wait, the water is getting close to an electrical outlet.")
        assert updated.response_text.startswith("Your emergency incident was updated")
        t.route("human_handoff")
        await say("I want to speak with someone.")
        handoff = await say("Yes, please.")
        assert "No live transfer" in handoff.response_text
        async with t.source.database.session() as db:
            incident = (
                await db.scalars(
                    select(EmergencyIncident).where(
                        EmergencyIncident.session_id == t.source.session_id
                    )
                )
            ).one()
            assert incident.revision == 2 and str(incident.id) in created.response_text
            assert incident.location_detail == "Living room AC."
            linked = (
                await db.scalars(select(Handoff).where(Handoff.session_id == t.source.session_id))
            ).one()
            assert linked.incident_id == incident.id
            turns = await db.scalar(
                select(func.count())
                .select_from(AgentTurn)
                .where(AgentTurn.session_id == t.source.session_id)
            )
            assert turns == 9
            calls = await db.scalar(
                select(func.count())
                .select_from(ToolCall)
                .where(ToolCall.session_id == t.source.session_id)
            )
            assert calls == 3  # Create incident, update the same incident, create handoff.
        assert pipeline.duplicates == 9
    finally:
        await socket.close()
    assert media.closed and stt.closed and not live_relay_tasks()


async def test_media_disconnect_after_commit_preserves_turn_and_control_replay(
    realtime_app: FastAPI,
    text_harness: TextHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = text_harness
    ready = await t.emergency_ready()
    committed, release = asyncio.Event(), asyncio.Event()
    execute = t.source.executor.execute

    async def held(session_id: UUID, action: ActionRequest) -> ToolResult:
        result = await execute(session_id, action)
        committed.set()
        await release.wait()
        return result

    monkeypatch.setattr(t.source.executor, "execute", held)
    socket = Socket(realtime_app, t.source.session_id)
    await socket.start()
    session = await realtime_app.state.realtime_manager.get(t.source.session_id)
    assert session
    media, stt = FakeMedia(), FakeSTT()
    pipeline = SpeechPipeline(session, media, stt, speech_context(t.source.context))
    session.speech = pipeline
    pipeline.start()
    final: SpeechFinal | None = None
    try:
        await stt.emit("FINAL", ready.utterance)
        async with asyncio.timeout(5):
            await committed.wait()
        await media.frames.put(None)
        while True:
            event = await socket.receive()
            if isinstance(event.payload, SpeechFinal):
                final = event.payload
            if event.event_type == "STT_FAILED":
                break
        assert await t.source.count(EmergencyIncident) == 1
        assert session.active_task and not session.active_task.done()
        release.set()
        await socket.until_idle()
    finally:
        release.set()
        await socket.close()
    assert final and final.turn_number and final.expected_revision
    replay_socket = Socket(realtime_app, t.source.session_id)
    await replay_socket.start()
    try:
        replay = await replay_socket.turn(
            AgentTurnRequest(
                session_id=t.source.session_id,
                turn_id=final.turn_id,
                turn_number=final.turn_number,
                expected_revision=final.expected_revision,
                utterance=final.transcript,
            )
        )
        reply = next(e.payload for e in replay if isinstance(e.payload, AgentResponse))
        assert isinstance(reply, AgentResponse) and reply.replayed
        assert await t.source.count(EmergencyIncident) == 1
    finally:
        await replay_socket.close()


async def test_property_context_isolation_and_two_speech_sessions(
    realtime_app: FastAPI,
    text_harness: TextHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = text_harness
    other = await t.store.create_demo("Alex")
    entered, resume = asyncio.Event(), asyncio.Event()
    process = t.agent.process

    async def slow(request: AgentTurnRequest) -> AgentTurnResult:
        if request.session_id == t.source.session_id:
            entered.set()
            await resume.wait()
        return await process(request)

    monkeypatch.setattr(t.agent, "process", slow)
    first, second = (
        Socket(realtime_app, t.source.session_id),
        Socket(realtime_app, other.session_id),
    )
    await first.start()
    await second.start()
    _, palm = await t.store.load(other.session_id)
    atlas_terms, palm_terms = speech_context(t.source.context), speech_context(palm)
    assert "Atlas Residence" in atlas_terms.keyterms and "Palm Heights" not in atlas_terms.keyterms
    assert "Palm Heights" in palm_terms.keyterms and "Atlas Residence" not in palm_terms.keyterms
    assert "Emergency Maintenance" not in palm_terms.keyterms
    assert t.source.context.resident
    assert t.source.context.resident.first_name not in atlas_terms.keyterms
    pipes = []
    try:
        for socket, context in ((first, atlas_terms), (second, palm_terms)):
            session = await realtime_app.state.realtime_manager.get(socket.session_id)
            assert session
            media, stt = FakeMedia(), FakeSTT()
            pipeline = SpeechPipeline(session, media, stt, context)
            session.speech = pipeline
            pipeline.start()
            pipes.append((pipeline, stt))
        await pipes[0][1].emit("FINAL", "Unusual request A")
        async with asyncio.timeout(3):
            await entered.wait()
        await pipes[1][1].emit("FINAL", "Unusual request B")
        result = await second.until_idle()
        assert any(isinstance(e.payload, AgentResponse) for e in result)
        assert not resume.is_set()
        resume.set()
        await first.until_idle()
    finally:
        await first.close()
        await second.close()
        async with t.source.database.session() as db:
            for model in (AgentTurn, ToolCall, WorkflowRun):
                await db.execute(delete(model).where(model.session_id == other.session_id))
            await db.execute(
                delete(ConversationSession).where(ConversationSession.id == other.session_id)
            )
    assert all(stt.closed for _, stt in pipes) and not live_relay_tasks()


async def test_voice_token_admission_and_text_context_update(
    realtime_app: FastAPI,
    text_harness: TextHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t, app = text_harness, realtime_app
    app.state.database = t.source.database
    app.state.settings = Settings(
        voice_enabled=True,
        livekit_url="ws://livekit:7880",
        livekit_api_key=SecretStr("devkey"),
        livekit_api_secret=SecretStr("secret"),
        assemblyai_api_key=SecretStr("not-used"),
    )
    media, stt = FakeMedia(), FakeSTT()

    def source(settings: Settings, session_id: UUID) -> FakeMedia:
        assert session_id == t.source.session_id
        return media

    monkeypatch.setattr(voice, "LiveKitAudioSource", source)
    monkeypatch.setattr(voice, "AssemblyAIStreaming", lambda settings: stt)
    socket = Socket(app, t.source.session_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/voice/{t.source.session_id}/token"
        assert (await client.post(path, json={})).status_code == 409
        await socket.start()
        try:
            assert (await client.post(path, json={"property_id": "Atlas"})).status_code == 422
            response = await client.post(path, json={})
            assert response.status_code == 200
            assert response.json()["conversation_session_id"] == str(t.source.session_id)
            assert "secret" not in response.text and "not-used" not in response.text
            assert (await client.post(path, json={})).status_code == 409
            t.route("non_emergency_maintenance")
            result = await socket.turn(await t.request("My AC isn't cooling."))
            reply = next(e.payload for e in result if isinstance(e.payload, AgentResponse))
            assert isinstance(reply, AgentResponse)
            async with asyncio.timeout(3):
                assert await stt.updates.get() == reply.response_text
            app.state.settings.app_env = "production"
            assert (await client.post(path, json={})).status_code == 403
        finally:
            await socket.close()
    assert media.closed and stt.closed and not live_relay_tasks()
