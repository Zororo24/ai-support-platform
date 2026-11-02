from fastapi import APIRouter

from app.api.routes import (
    agents,
    approvals,
    auth,
    chat,
    collections,
    connections,
    documents,
    health,
    insights,
    orders,
    quality,
    runs,
    scenarios,
    settings,
    tickets,
    users,
    v1,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(orders.router)
api_router.include_router(tickets.router)
api_router.include_router(chat.router)
api_router.include_router(documents.router)
api_router.include_router(users.router)
api_router.include_router(insights.router)
api_router.include_router(collections.router)
api_router.include_router(quality.router)
api_router.include_router(agents.router)
api_router.include_router(v1.router)
api_router.include_router(connections.router)
api_router.include_router(runs.router)
api_router.include_router(scenarios.router)
api_router.include_router(settings.router)
api_router.include_router(webhooks.router)
api_router.include_router(approvals.router)


from app.core.config import get_settings  # noqa: E402

if get_settings().enable_demo_company_api:
    from app.api.routes import demo_company  # noqa: E402

    api_router.include_router(demo_company.router)
