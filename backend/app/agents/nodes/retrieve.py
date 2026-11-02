from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig

from app.agents.state import AgentState
from app.rag.retrieve import retrieve as hybrid_retrieve


def retrieve(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    db = config["configurable"]["db"]
    chunks, cache_hit = hybrid_retrieve(
        db, tenant_id=UUID(state["tenant_id"]), query=state["messages"][-1]["content"]
    )
    return {"retrieved_chunks": chunks, "cache_hit": cache_hit}
