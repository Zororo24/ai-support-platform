import math

from app.core import llm
from app.core.config import get_settings
from app.db.models import EMBEDDING_DIM

BATCH_SIZE = 50


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed via the configured model. Raises LLMUnavailable; never returns placeholder vectors."""
    if get_settings().embedding_dimensions != EMBEDDING_DIM:
        raise ValueError(
            f"EMBEDDING_DIMENSIONS must equal the chunks.embedding column size ({EMBEDDING_DIM})"
        )
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH_SIZE):
        vectors.extend(_normalize(v) for v in llm.embed(texts[start : start + BATCH_SIZE]))
    if len(vectors) != len(texts):
        raise llm.LLMUnavailable("Embedding count does not match input count")
    return vectors


def _normalize(vec: list[float]) -> list[float]:
    # Truncated (reduced-dimension) embeddings are not unit length; cosine search wants them to be.
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
