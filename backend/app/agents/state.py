from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    # Identity comes from the authenticated principal, never from the request body.
    tenant_id: str
    user_id: str
    messages: list[dict[str, str]]  # [{"role": "user"|"assistant", "content": ...}], newest last
    intent: str  # knowledge | order | ticket | other
    confidence: float
    order_number: str | None
    retrieved_chunks: list[dict[str, Any]]
    cache_hit: bool
    citations: list[dict[str, Any]]
    tool_result: dict[str, Any] | None  # {"order": Order} or {"ticket": Ticket}
    # answered | refused | order_found | order_not_found | order_needs_number
    # | ticket_created | clarify | other
    outcome: str
    final_response: str
