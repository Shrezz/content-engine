"""Keenable web search, normalized to a small evidence contract."""

from keenable import Keenable, KeenableError
from pydantic import ValidationError

from .config import MAX_RESEARCH_SOURCES
from .models import Source
from .tracing import RunTrace


class EmptySearchError(RuntimeError):
    """A usable research checkpoint, unlike an authentication/provider failure."""


class KeenableSearch:
    def __init__(self, api_key: str = "", trace: RunTrace | None = None) -> None:
        self.api_key = api_key
        self.trace = trace or RunTrace()

    def search(self, query: str) -> list[Source]:
        with self.trace.span(
            "keenable.search", "tool",
            inputs={"query": query, "mode": "pro", "snippet_max_length": 4000},
        ) as event:
            sources = self._search(query)
            event["outputs"] = sources
            event["metadata"] = {
                "source_count": len(sources),
                "retained_source_limit": MAX_RESEARCH_SOURCES,
            }
            return sources

    def _search(self, query: str) -> list[Source]:
        try:
            with Keenable(api_key=self.api_key, timeout=30, client_source="Content Engine") as client:
                response = client.search(query=query, mode="pro", snippet_max_length=4000)
        except KeenableError as error:
            raise RuntimeError(
                f"Keenable search failed ({type(error).__name__}). Check key, rate limits, or connectivity."
            ) from None

        sources = []
        seen = set()
        for result in response:
            try:
                source = Source(
                    title=result.title,
                    url=result.url,
                    content=result.snippet[:4000],
                )
            except (ValidationError, TypeError):
                continue
            if str(source.url) not in seen:
                sources.append(source)
                seen.add(str(source.url))
            if len(sources) == MAX_RESEARCH_SOURCES:
                break
        if not sources:
            raise EmptySearchError("Keenable found no usable sources. Refine the topic or angle and retry.")
        return sources

    def fetch(self, source: Source) -> Source:
        """Fetch a known source through Keenable; keep a bounded page excerpt."""
        with self.trace.span("keenable.fetch", "tool", inputs={"url": str(source.url)}) as event:
            try:
                with Keenable(api_key=self.api_key, timeout=30, client_source="Content Engine") as client:
                    page = client.fetch(str(source.url))
            except KeenableError as error:
                raise RuntimeError(f"Keenable fetch failed ({type(error).__name__}).") from None
            if not page.content or not page.content.strip():
                raise RuntimeError("Keenable returned no readable page content.")
            result = Source(title=page.title or source.title, url=source.url, content=page.content[:16000])
            event["outputs"] = result
            event["metadata"] = {"truncated": len(page.content) > 16000}
            return result
