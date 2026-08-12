"""FastAPI application factory."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.staticfiles import StaticFiles

from filearr import __version__
from filearr.api import v1_router
from filearr.config import get_settings
from filearr.profiles import seed_profiles_to_db
from filearr.search import ensure_index


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Console Logs panel: persist this process's log stream to app_logs.
    # In lifespan (not create_app) so unit tests on ASGITransport — which
    # skip lifespan — never spawn flusher threads against test databases.
    from filearr.logsink import install as install_logsink

    install_logsink("app")
    # BK-T1: compare the environment's FILEARR_SECRET_KEY (and the step-ca root
    # pin) against the fingerprints recorded IN this database, stamping them on
    # a fresh instance. Deliberately AFTER install_logsink so a mismatch ERROR
    # lands in the console Logs panel too, and deliberately non-fatal — see
    # filearr.keyguard for why a wrong key must not block boot.
    from filearr import keyguard

    await keyguard.run_startup_check()
    # P4-T1: register the code-shipped metadata profiles (idempotent upsert).
    await seed_profiles_to_db()
    await ensure_index()
    # Open the procrastinate pool ONCE for the app's lifetime. The defer
    # helpers used to open/close per call, which not only churned pools but
    # let concurrent requests close the pool under each other (same class as
    # the worker-side AppNotOpen incident, 2026-08-08). With the pool owned
    # here, every open_pool_if_needed() site becomes a no-op passthrough.
    from filearr.worker import proc_app

    await proc_app.open_async()
    yield
    await proc_app.close_async()


def create_app() -> FastAPI:
    settings = get_settings()
    # docs_url/redoc_url are None because /api/docs is registered manually
    # below (self-hosted Swagger assets), which also keeps FastAPI from
    # claiming /docs/oauth2-redirect — the /docs prefix belongs to the bundled
    # documentation site mount.
    app = FastAPI(
        title="Filearr",
        version=__version__,
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    # The frontend build vendors swagger-ui-dist into static/swagger-ui/ and
    # the SPA mount serves it; a dev checkout without a built frontend falls
    # back to FastAPI's default CDN URLs. Checked once — the image is
    # immutable.
    swagger_local = Path("static/swagger-ui/swagger-ui-bundle.js").is_file()

    @app.get("/api/docs", include_in_schema=False)
    async def api_docs():
        asset_kwargs = (
            {
                "swagger_js_url": "/swagger-ui/swagger-ui-bundle.js",
                "swagger_css_url": "/swagger-ui/swagger-ui.css",
                "swagger_favicon_url": "/favicon.ico",
            }
            if swagger_local
            else {}
        )
        return get_swagger_ui_html(
            openapi_url="/api/openapi.json",
            title=f"{app.title} - API docs",
            oauth2_redirect_url=None,
            **asset_kwargs,
        )
    if settings.environment == "development":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(v1_router, prefix="/api/v1")
    # LLM tool facade: its OWN sub-app so /api/llm/v1/openapi.json is a trimmed
    # spec containing only the role-gated tools (OpenWebUI/mcpo import it
    # directly). See docs/research/llm-rag-integration.md.
    from filearr.api.llm import llm_app

    app.mount("/api/llm/v1", llm_app)
    # Bundled documentation site (mkdocs build baked into the image at
    # /app/docs-site-html) — the console Help links here so the manual works
    # offline / on-LAN. Must be mounted before the "/" SPA catch-all.
    try:
        app.mount(
            "/docs", StaticFiles(directory="docs-site-html", html=True), name="docs"
        )
    except RuntimeError:
        pass  # dev mode: no bundled docs
    # Built SPA is copied to /app/static in the Docker image
    try:
        app.mount("/", StaticFiles(directory="static", html=True), name="spa")
    except RuntimeError:
        pass  # dev mode: frontend served by Vite
    return app


app = create_app()
