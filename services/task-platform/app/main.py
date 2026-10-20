import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.gateway_routes import gateway_error_handler
from app.api.gateway_routes import router as gateway_router
from app.api.library_routes import library_error_handler
from app.api.library_routes import router as library_router
from app.api.maintenance_routes import router as maintenance_router
from app.api.repository_routes import router as repository_router
from app.api.routes import router
from app.api.worker_routes import router as worker_router
from app.api.workspace_routes import router as workspace_router
from app.assistant.orchestrator import get_orchestrator, set_orchestrator
from app.bootstrap import bootstrap_categories, bootstrap_employees, bootstrap_knowledge_spaces
from app.config.settings import get_settings
from app.database import SessionLocal, init_database
from app.gateway.security import CredentialError
from app.gateway.service import GatewayError
from app.library_service import LibraryError
from app.runtime.maintenance import health_observer, maintenance_executor
from app.runtime.recovery import supervise_sessions
from app.seed import seed_demo_data
from app.worker_gateway.routes import router as worker_gateway_router

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_database()
    async with SessionLocal() as session:
        await bootstrap_categories(session)
        await bootstrap_knowledge_spaces(session)
    if settings.seed_demo:
        async with SessionLocal() as session:
            await seed_demo_data(session)
    else:
        async with SessionLocal() as session:
            await bootstrap_employees(session, settings.resolved_config_dir)
    from app.worker_gateway.admin_key import warn_if_admin_key_missing
    from app.worker_gateway.service import sweep_worker_gateway

    warn_if_admin_key_missing(settings.worker_gateway_admin_key)
    supervisor = asyncio.create_task(supervise_sessions())
    worker_sweep = asyncio.create_task(sweep_worker_gateway())
    maintenance = asyncio.create_task(maintenance_executor())
    health = asyncio.create_task(health_observer())
    try:
        yield
    finally:
        supervisor.cancel()
        worker_sweep.cancel()
        maintenance.cancel()
        health.cancel()
        await asyncio.gather(
            supervisor, worker_sweep, maintenance, health, return_exceptions=True
        )
        orchestrator = get_orchestrator()
        if orchestrator:
            await orchestrator.stop()
            set_orchestrator(None)
        from app.worker_gateway.service import get_gateway

        await get_gateway().aclose()


app = FastAPI(
    title="Nexus API",
    version="0.4.0",
    description="长期 Agent Worker 任务分配、工作观察、人工介入和结果管理 API",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(repository_router)
app.include_router(worker_router)
app.include_router(worker_gateway_router)
app.include_router(maintenance_router)
app.include_router(workspace_router)
app.include_router(library_router)
app.include_router(gateway_router)
app.add_exception_handler(LibraryError, library_error_handler)
app.add_exception_handler(GatewayError, gateway_error_handler)
app.add_exception_handler(CredentialError, gateway_error_handler)
