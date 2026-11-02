"""Wire research and writing; keep the earlier offline replay available."""

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from .state import ContentState, WorkflowState
from .researcher import ResearchNode
from .writer import WritingNode
from .config import MIN_RESEARCH_SOURCES


def build_graph(writer: WritingNode, researcher: ResearchNode | None = None) -> CompiledStateGraph:
    builder = StateGraph(ContentState)
    builder.add_node("write_drafts", writer)
    if researcher is None:
        builder.add_edge(START, "write_drafts")
    else:
        builder.add_node("research", researcher)
        builder.add_edge(START, "research")
        builder.add_edge("research", "write_drafts")
    builder.add_edge("write_drafts", END)
    return builder.compile()


def build_editorial_graph(nodes, checkpointer):
    """Explicit, bounded routing. Every publishing-adjacent export requires review."""
    graph = StateGraph(WorkflowState)
    for name in ["intake", "clarify", "novelty", "angle_review", "research", "deepen", "write", "evaluate", "research_review", "assets", "review", "export"]:
        graph.add_node(name, getattr(nodes, name))
    graph.add_edge(START, "intake")
    graph.add_conditional_edges("intake", lambda s: "clarify" if s["intake"]["status"] == "clarify" else "novelty")
    graph.add_conditional_edges("clarify", lambda s: END if s.get("status") == "cancelled" else "intake")
    graph.add_conditional_edges("novelty", lambda s: "angle_review" if s["novelty"]["duplicate"] else "research")
    graph.add_conditional_edges("angle_review", lambda s: END if s.get("status") == "cancelled" else "novelty")
    graph.add_conditional_edges("research", lambda s: "write" if len(s["sources"]) >= MIN_RESEARCH_SOURCES else "research_review")
    graph.add_edge("write", "evaluate")
    graph.add_conditional_edges("evaluate", nodes.after_evaluation)
    graph.add_edge("deepen", "write")
    graph.add_conditional_edges("research_review", lambda s: END if s.get("status") == "cancelled" else s["review_action"])
    graph.add_edge("assets", "review")
    graph.add_conditional_edges("review", lambda s: END if s.get("status") == "cancelled" else s["review_action"])
    graph.add_edge("export", END)
    return graph.compile(checkpointer=checkpointer)
