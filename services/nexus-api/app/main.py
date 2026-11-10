import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update

from app.api.gateway_routes import gateway_error_handler
from app.api.gateway_routes import router as gateway_router
from app.api.library_routes import library_error_handler
from app.api.library_routes import router as library_router
from app.api.maintenance_routes import router as maintenance_router
from app.api.routes import router
from app.api.speech_routes import router as speech_router
from app.api.task_tool_routes import router as task_tool_router
from app.api.worker_routes import router as worker_router
from app.api.workspace_routes import router as workspace_router
from app.bootstrap import bootstrap_categories, bootstrap_knowledge_spaces, bootstrap_workers
from app.config.settings import get_settings
from app.database import SessionLocal, validate_database
from app.gateway.security import CredentialError
from app.gateway.service import GatewayError
from app.library_service import LibraryError
from app.models import AgentWorker
from app.runtime.maintenance import maintenance_executor
from app.runtime.recovery import supervise_sessions
from app.seed import seed_demo_data

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await validate_database()
    async with SessionLocal() as session:
        await session.execute(
            update(AgentWorker).where(
                AgentWorker.mode == "LINUX_REMOTE", AgentWorker.status != "PAUSED",
            ).values(status="OFFLINE")
        )
        await session.commit()
        await bootstrap_categories(session)
        await bootstrap_knowledge_spaces(session)
    if settings.seed_demo:
        async with SessionLocal() as session:
            await seed_demo_data(session)
    else:
        async with SessionLocal() as session:
            await bootstrap_workers(session, settings.resolved_config_dir)
    from app.runtime.worker_connection import supervise_workers

    supervisor = asyncio.create_task(supervise_sessions())
    worker_sweep = asyncio.create_task(supervise_workers())
    maintenance = asyncio.create_task(maintenance_executor())
    try:
        yield
    finally:
        supervisor.cancel()
        worker_sweep.cancel()
        maintenance.cancel()
        await asyncio.gather(supervisor, worker_sweep, maintenance, return_exceptions=True)



app = FastAPI(
    title="Nexus API",
    version="1.5.0",
    description="长期 Agent Worker 任务分配、工作观察、人工介入和结果管理 API",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    expose_headers=["X-Total-Count"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(worker_router)
app.include_router(maintenance_router)
app.include_router(workspace_router)
app.include_router(library_router)
app.include_router(gateway_router)
app.include_router(speech_router)
app.include_router(task_tool_router)
app.add_exception_handler(LibraryError, library_error_handler)
app.add_exception_handler(GatewayError, gateway_error_handler)
app.add_exception_handler(CredentialError, gateway_error_handler)
