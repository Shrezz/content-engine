"""Durable LangGraph sessions and validation of human responses before resume."""

import re
import sqlite3
from contextlib import contextmanager

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .archive import ArticleArchive
from .config import Settings
from .decisions import DecisionClient
from .evaluator import DraftEvaluator
from .graph import build_editorial_graph
from .llm import NebiusLLM
from .metrics import ReviewMetrics
from .models import DraftPackage, EditorialBrief, EvaluationReport
from .search import KeenableSearch
from .tracing import RunTrace
from .visuals import VisualService
from .workflow import WorkflowNodes
from .writer import WritingNode


class WorkflowRuntime:
    def __init__(self, settings: Settings, trace: RunTrace | None = None, nodes: WorkflowNodes | None = None):
        self.settings = settings
        self.trace = trace or RunTrace()
        if nodes is None:
            llm = NebiusLLM(settings, self.trace)
            decisions = DecisionClient(llm.complete, self.trace)
            nodes = WorkflowNodes(settings, decisions, KeenableSearch(settings.keenable_api_key, self.trace),
                                  WritingNode(llm.generate, self.trace), DraftEvaluator(decisions),
                                  ArticleArchive(settings.articles_db), VisualService(settings, self.trace), self.trace)
        self.nodes = nodes
        self.metrics = ReviewMetrics(settings.metrics_db)

    @contextmanager
    def session(self, thread_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", thread_id):
            raise ValueError("Session ID must contain 1-64 letters, numbers, underscores, or hyphens")
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(str(self.settings.checkpoints_db)) as saver:
            graph = build_editorial_graph(self.nodes, saver)
            yield graph, {"configurable": {"thread_id": thread_id}, "recursion_limit": 80}

    def start(self, thread_id: str, *, request: str | None = None, brief: dict | None = None):
        if bool(request) == bool(brief):
            raise ValueError("Supply exactly one raw request or structured brief")
        if brief:
            brief = EditorialBrief.model_validate(brief).model_dump()
        with self.session(thread_id) as (graph, config):
            if graph.get_state(config).values:
                raise ValueError("Session already exists; resume or use a new ID")
            state = {"thread_id": thread_id, "revision_count": 0, "research_rounds": 0, "human_revisions": 0,
                     "conversation": [{"role": "user", "content": request}] if request else [], "status": "running"}
            if brief:
                state["brief"] = brief
            graph.invoke(state, config)
            return self.finish(graph.get_state(config))

    def status(self, thread_id):
        with self.session(thread_id) as (graph, config):
            snapshot = graph.get_state(config)
            if not snapshot.values:
                raise ValueError("Unknown session ID")
            return self.describe(snapshot)

    def sessions(self):
        """List recent threads using the same checkpoints as the CLI."""
        if not self.settings.checkpoints_db.exists():
            return []
        with sqlite3.connect(self.settings.checkpoints_db) as db:
            ids = db.execute("SELECT thread_id, MAX(checkpoint_id) AS latest FROM checkpoints WHERE checkpoint_ns = '' GROUP BY thread_id ORDER BY latest DESC LIMIT 100").fetchall()
        with self.session("index") as (graph, config):
            return [self.describe(graph.get_state({**config, "configurable": {"thread_id": key}})) for key, _ in ids]

    def resume(self, thread_id, response):
        with self.session(thread_id) as (graph, config):
            snapshot = graph.get_state(config)
            pending = self.pending(snapshot)
            if not pending:
                raise ValueError("Session has no pending human decision")
            self.validate_response(pending, response, snapshot.values)
            if pending["kind"] == "final_review" and response.get("action") in {"revise", "edit"}:
                item = self.metrics.record(snapshot.values, self.settings.nebius_model)
                self.metrics.review(item["id"], item["version"], 0,
                                    response.get("feedback") or "The human edited this draft before acceptance.", "revision")
            with self.trace.span("human_decision", "human", inputs={"thread_id": thread_id, "kind": pending["kind"], "response": response}):
                graph.invoke(Command(resume=response), config)
            return self.finish(graph.get_state(config))

    def retry(self, thread_id):
        with self.session(thread_id) as (graph, config):
            snapshot = graph.get_state(config)
            if not snapshot.values or not snapshot.next or self.pending(snapshot):
                raise ValueError("Retry is only for a failed node, not a pending human decision")
            graph.invoke(None, config)
            return self.finish(graph.get_state(config))

    def refresh(self, thread_id):
        """Rebuild assets from the saved evaluated drafts without granting approval."""
        with self.session(thread_id) as (graph, config):
            snapshot = graph.get_state(config)
            pending = self.pending(snapshot)
            if not pending or pending["kind"] != "final_review":
                raise ValueError("Only a session awaiting final review can refresh its assets")
            update = self.nodes.assets(snapshot.values)
            graph.update_state(config, {**update, "approval": {}}, as_node="assets")
            graph.invoke(None, config)
            return self.finish(graph.get_state(config))

    def finish(self, snapshot):
        pending = self.pending(snapshot) or {}
        if pending.get("kind") in {"final_review", "research_review"} or snapshot.values.get("status") == "approved":
            self.metrics.capture(snapshot.values, self.settings.nebius_model,
                                 approved=snapshot.values.get("status") == "approved", replace_label=True)
        return self.describe(snapshot)

    @staticmethod
    def pending(snapshot):
        return next((item.value for task in snapshot.tasks for item in task.interrupts), None)

    def describe(self, snapshot):
        state = snapshot.values
        errors = [str(task.error) for task in snapshot.tasks if task.error]
        return {"thread_id": state["thread_id"], "status": "failed" if errors else "awaiting_input" if self.pending(snapshot) else state.get("status", "running"),
                "pending": self.pending(snapshot), "errors": errors, "next_nodes": list(snapshot.next),
                "state": state, "trace_file": str(self.trace.path) if self.trace.path else None}

    def validate_response(self, pending, response, state):
        if not isinstance(response, dict):
            raise ValueError("A response must be a JSON object")
        kind, action = pending["kind"], response.get("action")
        if kind == "clarification":
            if action != "cancel" and (not isinstance(response.get("answer"), str) or not response["answer"].strip()):
                raise ValueError("Supply answer text, including explicit delegation if desired")
            return
        if action not in pending["actions"]:
            raise ValueError("Choose an action offered by the pending review")
        def has_text(key):
            return isinstance(response.get(key), str) and bool(response[key].strip())
        if kind == "angle_review" and action == "new_angle" and not has_text("angle"):
            raise ValueError("Supply the new angle")
        if kind == "research_review":
            if action == "query" and (not has_text("query") or state["research_rounds"] >= 6):
                raise ValueError("Supply a query; at most six explicit research passes are allowed per session")
            if action == "accept_scope":
                if len(state["sources"]) < 5 or not has_text("feedback"):
                    raise ValueError("A qualified scope requires five sources and explicit narrowing feedback")
                self._check_human_budget(state)
        if kind == "final_review":
            if response.get("version") != pending["version"]:
                raise ValueError("The response must identify the exact version currently awaiting review")
            if action in {"approve", "approve_with_warnings"}:
                self.nodes.visuals.verify(state["packet"])
                report = EvaluationReport.model_validate(state["evaluation"])
                if action == "approve" and (report.needs_revision or report.needs_research):
                    raise ValueError("Unresolved evaluation issues require revision or explicit approve_with_warnings and a reason")
                if action == "approve_with_warnings" and not has_text("reason"):
                    raise ValueError("Explain why the qualified draft is accepted despite the visible evaluation findings")
            if action == "revise":
                self._check_human_budget(state)
                if not has_text("feedback"):
                    raise ValueError("A revision requires specific feedback")
            if action == "edit":
                self._check_human_budget(state)
                DraftPackage.model_validate(response.get("drafts"))
            if action == "assets":
                choices = response.get("choices")
                if not isinstance(choices, dict) or not choices:
                    raise ValueError("Supply visual choices keyed by plan index")
                for index, choice in choices.items():
                    if not isinstance(index, str) or not index.isdigit() or int(index) >= len(state["drafts"]["visuals"]) or not isinstance(choice, dict):
                        raise ValueError("Unknown visual plan index")
                    if choice.get("action") not in {"attach", "omit"}:
                        raise ValueError("A visual choice must attach or omit")
                    required = ["reason"] if choice["action"] == "omit" else ["path", "credit"]
                    if not all(isinstance(choice.get(key), str) and choice[key].strip() for key in required):
                        raise ValueError("An omission needs a reason; an attachment needs a path and credit")

    def _check_human_budget(self, state):
        if state.get("human_revisions", 0) >= self.settings.max_human_revisions:
            raise ValueError("Human revision budget exhausted; approve a reviewed version or cancel")
