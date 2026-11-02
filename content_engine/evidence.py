"""Stable passage IDs let code copy evidence instead of asking an LLM to transcribe it."""

import re

from .models import Source


def evidence_passages(sources: list[Source]) -> dict[str, dict]:
    passages = {}
    for source_number, source in enumerate(sources, 1):
        paragraphs = re.split(r"\n\s*\n", source.content)
        part = 0
        for paragraph in paragraphs:
            remaining = paragraph.strip()
            while remaining:
                end = len(remaining) if len(remaining) <= 1200 else remaining.rfind(" ", 0, 1200)
                if end <= 0:
                    end = min(1200, len(remaining))
                text = remaining[:end].strip()
                remaining = remaining[end:].lstrip()
                if text:
                    part += 1
                    key = f"S{source_number}P{part}"
                    passages[key] = {"id": key, "source_url": str(source.url), "text": text}
    return passages
