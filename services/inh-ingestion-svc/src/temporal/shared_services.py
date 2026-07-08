"""Process-level service registry for Temporal activity connections.

Instead of each activity creating and destroying its own DB engine +
connection pool, all activities share long-lived pools from this registry.
The worker initializes the registry at startup and tears it down on shutdown.

Thread safety: Temporal runs async activities in the event loop, so
concurrent initialization is not a concern. A threading lock is included
as a safety net for potential future sync activity executors.

Connection health: All SQLAlchemy engines use pool_pre_ping=True, which
automatically replaces stale connections before use.
"""

import asyncio
import threading

import structlog

from src.config.settings import Settings

logger = structlog.get_logger(__name__)

_lock = threading.Lock()
_settings: Settings | None = None


# Service instances (lazily connected on first access)
_db_service = None
_staging_service = None
_storage_service = None
_weaviate_service = None
# MQ service used by the publish_completion activity (#88). Either registered
# by main.py (worker mode reuses its already-connected service) or lazily
# created here. _mq_service_owned tracks whether this registry must
# disconnect it on shutdown (externally registered services are the caller's
# to close).
_mq_service = None
_mq_service_owned = False
_mq_connect_lock = asyncio.Lock()


def initialize(settings: Settings) -> None:
    """Store settings for lazy service creation. Called once at worker startup."""
    global _settings
    _settings = settings
    logger.info("Shared service registry initialized")


def shutdown() -> None:
    """Disconnect all shared services. Called on worker shutdown."""
    global _db_service, _staging_service
    global _storage_service, _weaviate_service, _settings
    global _mq_service, _mq_service_owned

    services = [
        ("db", _db_service),
        ("staging", _staging_service),
        ("storage", _storage_service),
        ("weaviate", _weaviate_service),
    ]

    for name, svc in services:
        if svc is not None:
            try:
                svc.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting shared {name} service", error=str(e))

    # MQ disconnect is async; this shutdown hook is sync. Best-effort schedule
    # it when a loop is running, otherwise let process exit close the socket.
    # Externally registered services (worker mode) are disconnected by main.py.
    if _mq_service is not None and _mq_service_owned:
        try:
            asyncio.get_running_loop().create_task(_mq_service.disconnect())
        except RuntimeError:
            logger.debug("No running loop; shared MQ connection closes at process exit")
    _mq_service = None
    _mq_service_owned = False

    _db_service = None
    _staging_service = None
    _storage_service = None
    _weaviate_service = None
    _settings = None
    logger.info("Shared service registry shutdown")


def _get_settings() -> Settings:
    if _settings is not None:
        return _settings
    from src.config.settings import get_settings

    return get_settings()


def get_db_service():
    """Get or create shared DatabaseService."""
    global _db_service
    if _db_service is None:
        with _lock:
            if _db_service is None:
                from src.services.database import DatabaseService

                _db_service = DatabaseService(_get_settings())
                _db_service.connect()
                logger.debug("Shared DatabaseService connected")
    return _db_service


def get_staging_service():
    """Get or create shared StagingService."""
    global _staging_service
    if _staging_service is None:
        with _lock:
            if _staging_service is None:
                from src.services.staging import StagingService

                _staging_service = StagingService(_get_settings())
                _staging_service.connect()
                logger.debug("Shared StagingService connected")
    return _staging_service


def get_storage_service():
    """Get or create shared StorageService."""
    global _storage_service
    if _storage_service is None:
        with _lock:
            if _storage_service is None:
                from src.services.storage import StorageService

                _storage_service = StorageService(_get_settings())
                _storage_service.connect()
                logger.debug("Shared StorageService connected")
    return _storage_service


def set_mq_service(mq_service) -> None:
    """Register an externally-owned, already-connected MQ service.

    Worker mode calls this so the publish_completion activity reuses the
    subscriber's connection instead of opening a second one. The caller keeps
    ownership: it disconnects the service itself.
    """
    global _mq_service, _mq_service_owned
    _mq_service = mq_service
    _mq_service_owned = False
    logger.debug("Shared MQ service registered (externally owned)")


async def get_mq_service():
    """Get the shared MQ service, lazily creating + connecting one if needed.

    Async (unlike the other getters) because MQ backends connect over the
    network with async clients.
    """
    global _mq_service, _mq_service_owned
    if _mq_service is None:
        async with _mq_connect_lock:
            if _mq_service is None:
                from src.services.mq import create_mq_service

                svc = create_mq_service(_get_settings())
                await svc.connect()
                _mq_service = svc
                _mq_service_owned = True
                logger.debug("Shared MQ service connected")
    return _mq_service


def get_weaviate_service():
    """Get or create shared WeaviateService.

    Returns None if Weaviate is not available (non-critical service).
    """
    global _weaviate_service
    if _weaviate_service is None:
        with _lock:
            if _weaviate_service is None:
                from src.services.weaviate import WeaviateService

                svc = WeaviateService(_get_settings())
                try:
                    svc.connect()
                    _weaviate_service = svc
                    logger.debug("Shared WeaviateService connected")
                except Exception as e:
                    logger.warning("Weaviate not available for shared service", error=str(e))
                    return None
    return _weaviate_service
