"""Research node: derive a query from the brief and call the search tool."""

from collections.abc import Callable

from .config import MIN_RESEARCH_SOURCES
from .models import Source
from .state import ContentState
from .tracing import RunTrace


class ResearchNode:
    def __init__(self, search: Callable[[str], list[Source]], trace: RunTrace | None = None) -> None:
        self.search = search
        self.trace = trace or RunTrace()

    def __call__(self, state: ContentState) -> dict[str, str | list[Source]]:
        with self.trace.span("research", "node", inputs={"brief": state["brief"]}) as event:
            brief = state["brief"]
            query = f"{brief.topic}: {brief.angle}"
            sources = self.search(query)
            update = {"research_query": query, "sources": sources}
            event["outputs"] = update
            if len(sources) < MIN_RESEARCH_SOURCES:
                raise RuntimeError(
                    f"Research returned {len(sources)} usable sources; at least "
                    f"{MIN_RESEARCH_SOURCES} are required for the first draft. "
                    "Review the evidence and refine the topic or search criteria; drafting stopped."
                )
            return update
