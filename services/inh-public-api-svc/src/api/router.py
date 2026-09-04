"""Main API router."""

from fastapi import APIRouter

from src.api.v1 import admin, chunks, documents, evals, search, verify, whoami

router = APIRouter(prefix="/v1")

router.include_router(search.router, tags=["Search"])
router.include_router(documents.router, tags=["Documents"])
router.include_router(chunks.router, tags=["Chunks"])
router.include_router(verify.router, tags=["Verify"])
router.include_router(evals.router, tags=["Evals"])
router.include_router(whoami.router, tags=["Identity"])
router.include_router(admin.router, tags=["Admin"])
