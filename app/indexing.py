from __future__ import annotations

CHUNK_WORDS = 250
OVERLAP_WORDS = 40


def chunk_text(title: str, text: str) -> list[str]:
    """Divide il testo in blocchi di ~250 parole con sovrapposizione.

    Il titolo è anteposto a ogni blocco, così anche i blocchi centrali
    "ricordano" di quale articolo fanno parte.
    """
    words = text.split()
    if not words:
        return [title]
    step = CHUNK_WORDS - OVERLAP_WORDS
    chunks = []
    for start in range(0, len(words), step):
        piece = " ".join(words[start : start + CHUNK_WORDS])
        chunks.append(f"{title}\n\n{piece}")
        if start + CHUNK_WORDS >= len(words):
            break
    return chunks
