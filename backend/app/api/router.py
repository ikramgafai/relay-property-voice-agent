"""Root API router; intentionally no domain routes."""

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.turns import router as turns_router
from app.api.voice import router as voice_router
from app.realtime.websocket import router as realtime_router

router = APIRouter()
router.include_router(health_router)
router.include_router(turns_router)
router.include_router(realtime_router)
router.include_router(voice_router)
