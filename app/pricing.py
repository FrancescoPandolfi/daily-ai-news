"""Stima dei costi in dollari.

ATTENZIONE: sono prezzi di listino copiati a mano e cambiano nel tempo. I costi mostrati nella
pagina di monitoraggio sono stime da confrontare con le fatture. Un modello assente da queste
tabelle costa 0 e la pagina lo segnala.
"""

from __future__ import annotations

from typing import Literal, assert_never, get_args

Operation = Literal[
    "agent_step",  # un giro dell'agente (OpenAI, chat)
    "index_embed",  # embedding degli articoli salvati (OpenAI)
    "rag_embed",  # embedding della domanda (OpenAI)
    "rag_answer",  # risposta generata alle domande (OpenAI, chat)
    "search",  # ricerca web (Tavily)
    "extract",  # lettura di una pagina (Tavily)
    "send_email",  # invio del digest (Resend)
]
OPERATIONS: tuple[str, ...] = get_args(Operation)

# dollari per 1 milione di token: (input, output)
LLM_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}
# dollari per 1 milione di token
EMBEDDING_PRICES: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
    "text-embedding-ada-002": 0.10,
}
# Tavily conta in crediti: una ricerca o una lettura di pagina valgono circa 1 credito.
TAVILY_USD_PER_CREDIT = 0.008
TAVILY_FREE_CREDITS_PER_MONTH = 1000


def _lookup[T](table: dict[str, T], model: str | None) -> T | None:
    """Trova il prezzo anche per nomi con data (es. gpt-4.1-mini-2025-04-14): vince il prefisso più lungo."""
    if not model:
        return None
    matches = [name for name in table if model == name or model.startswith(f"{name}-")]
    return table[max(matches, key=len)] if matches else None


def estimate_cost(
    operation: Operation,
    model: str | None,
    tokens_in: int,
    tokens_out: int,
    units: int,
    free_units_left: int = 0,
) -> tuple[float, bool]:
    """Restituisce (costo stimato in USD, prezzo noto?).

    `free_units_left` sono i crediti gratuiti ancora disponibili nel mese (conta solo per Tavily).
    """
    match operation:
        case "agent_step" | "rag_answer":
            price = _lookup(LLM_PRICES, model)
            if price is None:
                return 0.0, False
            return (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000, True
        case "index_embed" | "rag_embed":
            per_million = _lookup(EMBEDDING_PRICES, model)
            if per_million is None:
                return 0.0, False
            return tokens_in * per_million / 1_000_000, True
        case "search" | "extract":
            billable = max(0, units - free_units_left)
            return billable * TAVILY_USD_PER_CREDIT, True
        case "send_email":
            return 0.0, True
        case _:
            assert_never(operation)
