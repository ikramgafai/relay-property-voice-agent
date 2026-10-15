"""Internal media admission. Identity comes only from Relay's persisted session."""

from typing import cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from app.api.turns import internal_only
from app.core.config import Settings
from app.db.agent import SQLAlchemyTurnStore
from app.db.session import get_database
from app.property.schemas import DomainModel
from app.providers.stt.assemblyai import AssemblyAIStreaming
from app.realtime.manager import RealtimeSessionManager
from app.realtime.media.livekit import (
    LiveKitAudioSink,
    LiveKitAudioSource,
    LiveKitMediaSession,
    participant_name,
    room_name,
    token,
)
from app.realtime.output import SpeechOutput
from app.realtime.schemas import State
from app.realtime.speech import SpeechPipeline
from app.realtime.speech_context import speech_context

router = APIRouter(prefix="/api/voice")


class VoiceInput(DomainModel):
    pass  # Explicit empty body rejects caller-supplied identity/room/property fields.


class VoiceToken(DomainModel):
    livekit_url: str
    room_name: str
    participant_identity: str
    participant_token: str
    conversation_session_id: UUID
    expires_in_seconds: int = 300


@router.post("/{session_id}/token", response_model=VoiceToken)
async def voice_token(session_id: UUID, body: VoiceInput, request: Request) -> VoiceToken:
    internal_only(request)
    settings = cast(Settings, request.app.state.settings)
    if not settings.voice_enabled:
        raise HTTPException(503, "VOICE_DISABLED")
    manager = cast(RealtimeSessionManager, request.app.state.realtime_manager)
    realtime = await manager.get(session_id)
    if realtime is None or realtime.state != State.IDLE or realtime.stopped.is_set():
        raise HTTPException(409, "START_REALTIME_SESSION_FIRST")
    # The same gate prevents simultaneous token requests from starting competing pipelines.
    async with realtime._input_lock:
        if realtime.closed or realtime.stopped.is_set():
            raise HTTPException(409, "SESSION_UNAVAILABLE")
        if realtime.speech is not None:
            old = realtime.speech
            if isinstance(old, SpeechPipeline) and not old.closed:
                raise HTTPException(409, "VOICE_ALREADY_ACTIVE")
            await old.close()
        if realtime.output is not None:
            # The previous attachment's Room is gone; its output can never publish again.
            await realtime.output.close()
            realtime.output = None
        try:
            store = SQLAlchemyTurnStore(get_database(request))
            _, context = await store.load(session_id)
            previous = realtime.last_response or await store.last_response(session_id)
            assert settings.livekit_url
            result = VoiceToken(
                livekit_url=settings.livekit_url,
                room_name=room_name(session_id),
                participant_identity=participant_name(session_id),
                participant_token=token(settings, session_id),
                conversation_session_id=session_id,
            )
            # One backend participant: input and (optional) output share this media session.
            media = LiveKitMediaSession(settings, session_id)
            pipeline = SpeechPipeline(
                realtime,
                LiveKitAudioSource(settings, session_id, media),
                AssemblyAIStreaming(settings),
                speech_context(context, previous),
                admission_grace_ms=settings.stt_final_admission_grace_ms,
            )
            provider = getattr(request.app.state, "tts_provider", None)
            output = None
            if settings.tts_enabled and provider is not None:
                output = SpeechOutput(
                    provider,
                    LiveKitAudioSink(media),
                    provider_name=getattr(provider, "provider_name", None),
                    model=getattr(provider, "model", None),
                    log=realtime.log,
                    observer=realtime.presentation_event,
                )
            realtime.speech = pipeline
            pipeline.start()
            if output is not None:
                realtime.attach_output(output)
            return result
        except Exception:
            raise HTTPException(503, "VOICE_SESSION_UNAVAILABLE") from None
