from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/config")
async def public_config():
    from app.core.config import settings
    return {
        "web_publishing_available": settings.web_publishing_available,
        "tmdb_available": bool(settings.tmdb_api_token.strip()),
    }


@router.get("/health")
async def health():
    return {"status": "ok"}
