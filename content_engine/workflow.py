"""Node behavior for the complete editorial workflow. Routing lives in graph.py."""

from langgraph.types import interrupt

from .archive import ArticleArchive
from .config import MIN_RESEARCH_SOURCES, Settings
from .decisions import DecisionClient, INTAKE_INSTRUCTIONS, NOVELTY_INSTRUCTIONS, with_delegation_choice
from .evaluator import DraftEvaluator
from .models import DraftPackage, EditorialBrief, EvaluationReport, IntakeDecision, NoveltyDecision, Source
from .search import EmptySearchError, KeenableSearch
from .tracing import RunTrace
from .visuals import VisualService
from .writer import WritingNode


class WorkflowNodes:
    def __init__(self, settings: Settings, decisions: DecisionClient, search: KeenableSearch,
                 writer: WritingNode, evaluator: DraftEvaluator, archive: ArticleArchive,
                 visuals: VisualService, trace: RunTrace):
        self.settings, self.decisions, self.search = settings, decisions, search
        self.writer, self.evaluator, self.archive = writer, evaluator, archive
        self.visuals, self.trace = visuals, trace

    def wait(self, payload):
        with self.trace.span(payload["kind"], "human", inputs=payload) as event:
            response = interrupt(payload)
            event["outputs"] = response
            return response

    def intake(self, state):
        with self.trace.span("intake", "node", inputs=state.get("conversation")) as event:
            if state.get("brief"):
                brief = EditorialBrief.model_validate(state["brief"])
                decision = IntakeDecision(status="ready", brief=brief)
            else:
                decision = self.decisions.ask("resolve_brief", INTAKE_INSTRUCTIONS, IntakeDecision,
                                              {"conversation": state["conversation"]})
            decision = with_delegation_choice(decision)
            update = {"intake": decision.model_dump(mode="json"), "assumptions": decision.assumptions}
            if decision.brief:
                update["brief"] = decision.brief.model_dump()
            event["outputs"] = update
            return update

    def clarify(self, state):
        decision = state["intake"]
        response = self.wait({"kind": "clarification", "question": decision["question"], "choices": decision["choices"]})
        if response.get("action") == "cancel":
            return {"status": "cancelled"}
        return {"conversation": state["conversation"] + [
            {"role": "assistant", "content": decision["question"]},
            {"role": "user", "content": response["answer"]},
        ]}

    def novelty(self, state):
        if state.get("repeat_allowed"):
            return {"novelty": {"duplicate": False, "reason": "Human explicitly allowed the repeated angle.", "matching_ids": [], "alternative_angles": []}}
        history = self.archive.briefs(state["thread_id"])
        with self.trace.span("review_article_history", "node", inputs={"brief": state["brief"], "archive_count": len(history)}) as event:
            if len(history) > 100:
                decision = NoveltyDecision(duplicate=True, reason="Archive exceeds the 100-article automatic review budget. Human direction is required.")
            elif history:
                decision = self.decisions.ask("compare_angles", NOVELTY_INSTRUCTIONS, NoveltyDecision,
                                              {"brief": state["brief"], "archive_metadata": history})
                if set(decision.matching_ids) - {a["id"] for a in history}:
                    raise ValueError("Novelty review returned an unknown archive ID")
            else:
                decision = NoveltyDecision(duplicate=False, reason="The approved article archive is empty.")
            event["outputs"] = decision
            return {"novelty": decision.model_dump()}

    def angle_review(self, state):
        matching_ids = set(state["novelty"]["matching_ids"])
        response = self.wait({"kind": "angle_review", "brief": state["brief"], "finding": state["novelty"],
                              "matching_articles": [a for a in self.archive.briefs(state["thread_id"]) if a["id"] in matching_ids],
                              "actions": ["new_angle", "allow_repeat", "cancel"]})
        if response["action"] == "cancel":
            return {"status": "cancelled"}
        if response["action"] == "allow_repeat":
            return {"repeat_allowed": True}
        return {"brief": {**state["brief"], "angle": response["angle"]}, "repeat_allowed": False}

    def research(self, state):
        brief = state["brief"]
        query = state.get("search_query") or f"{brief['topic']}: {brief['angle']}. Audience: {brief['audience']}"
        with self.trace.span("research", "node", inputs={"query": query}) as event:
            try:
                sources = self.search.search(query)
            except EmptySearchError:
                sources = []
            update = {"search_query": query, "sources": [s.model_dump(mode="json") for s in sources],
                      "research_rounds": state.get("research_rounds", 0) + 1, "scope_accepted": False}
            event["outputs"] = update
            return update

    def deepen(self, state):
        report = EvaluationReport.model_validate(state["evaluation"])
        by_url = {s["url"]: Source.model_validate(s) for s in state["sources"]}
        warnings = []
        with self.trace.span("deepen_research", "node", inputs={"queries": report.followup_queries, "fetch_urls": report.fetch_urls}) as event:
            for query in report.followup_queries[:2] or [state["search_query"]]:
                try:
                    for source in self.search.search(query):
                        by_url[str(source.url)] = source
                except EmptySearchError:
                    warnings.append(f"No new evidence for: {query}")
            for url in report.fetch_urls:
                if url not in by_url:
                    raise ValueError("Refusing to fetch an unknown source URL")
                try:
                    by_url[url] = self.search.fetch(by_url[url])
                except RuntimeError as error:
                    warnings.append(str(error))
            update = {"sources": [s.model_dump(mode="json") for s in by_url.values()],
                      "research_rounds": state["research_rounds"] + 1, "research_warnings": warnings,
                      "feedback": "Use the added evidence to resolve material gaps; qualify or remove claims that remain unsupported."}
            event["outputs"] = update
            return update

    def write(self, state):
        writer_state = {"brief": EditorialBrief.model_validate(state["brief"]),
                        "sources": [Source.model_validate(s) for s in state["sources"]]}
        if state.get("drafts"):
            writer_state.update(previous_drafts=state["drafts"], evaluation=state.get("evaluation"), feedback=state.get("feedback", ""))
        drafts = self.writer(writer_state)["drafts"]
        return {"drafts": drafts.model_dump(), "revision_count": state.get("revision_count", 0) + int(bool(state.get("drafts"))),
                "visual_choices": {}, "approval": {}, "packet": {}, "status": "drafted"}

    def evaluate(self, state):
        with self.trace.span("evaluate_drafts", "node") as event:
            report = self.evaluator.evaluate(EditorialBrief.model_validate(state["brief"]),
                                            DraftPackage.model_validate(state["drafts"]),
                                            [Source.model_validate(s) for s in state["sources"]])
            event["outputs"] = report
            return {"evaluation": report.model_dump(), "feedback": "Address the evaluation issues in the next revision."}

    def research_review(self, state):
        response = self.wait({"kind": "research_review", "brief": state["brief"], "query": state.get("search_query"),
                              "sources": state["sources"], "evaluation": state.get("evaluation"),
                              "warnings": state.get("research_warnings", []),
                              "actions": ["query", "accept_scope", "cancel"]})
        if response["action"] == "cancel":
            return {"status": "cancelled", "review_action": "cancel"}
        if response["action"] == "query":
            return {"search_query": response["query"], "review_action": "research", "scope_accepted": False}
        return {"scope_accepted": True, "review_action": "write", "human_revisions": state.get("human_revisions", 0) + 1,
                "feedback": "Human selected a narrower, qualified scope: " + response["feedback"] + ". Remove or qualify unsupported claims; do not repeat an unverified assertion."}

    def assets(self, state):
        with self.trace.span("prepare_review", "node") as event:
            packet = self.visuals.prepare(state["thread_id"], DraftPackage.model_validate(state["drafts"]),
                                          state["sources"], state["evaluation"], state.get("visual_choices", {}))
            event["outputs"] = {"version": packet["version"], "directory": packet["directory"], "assets": packet["snapshot"]["assets"]}
            return {"packet": packet, "status": "awaiting_review"}

    def review(self, state):
        response = self.wait({"kind": "final_review", "version": state["packet"]["version"],
                              "review_directory": state["packet"]["directory"], "brief": state["brief"],
                              "evaluation": state["evaluation"], "assets": state["packet"]["snapshot"]["assets"],
                              "actions": ["approve", "approve_with_warnings", "revise", "edit", "assets", "cancel"]})
        action = response["action"]
        if action == "cancel":
            return {"status": "cancelled", "review_action": "cancel"}
        if action == "assets":
            return {"visual_choices": {**state.get("visual_choices", {}), **response["choices"]}, "review_action": "assets"}
        if action == "edit":
            drafts = DraftPackage.model_validate(response["drafts"]).model_dump()
            old_plans, new_plans = state["drafts"]["visuals"], drafts["visuals"]
            choices = {key: choice for key, choice in state.get("visual_choices", {}).items()
                       if int(key) < len(new_plans) and int(key) < len(old_plans) and new_plans[int(key)] == old_plans[int(key)]}
            return {"drafts": drafts, "visual_choices": choices,
                    "approval": {}, "packet": {}, "review_action": "evaluate", "scope_accepted": False,
                    "human_revisions": state.get("human_revisions", 0) + 1}
        if action == "revise":
            return {"feedback": response["feedback"], "human_revisions": state.get("human_revisions", 0) + 1,
                    "review_action": "write", "scope_accepted": False}
        return {"approval": response, "review_action": "export"}

    def export(self, state):
        with self.trace.span("approved_export", "tool", inputs={"version": state["packet"]["version"], "approval": state["approval"]}) as event:
            target = self.visuals.export(state["thread_id"], state["packet"], state["approval"])
            self.archive.save(state["thread_id"], state["packet"]["version"], state["brief"], state["drafts"], str(target))
            event["outputs"] = {"directory": str(target), "published": False}
            return {"export_path": str(target), "status": "approved"}

    def after_evaluation(self, state):
        report = EvaluationReport.model_validate(state["evaluation"])
        can_revise = state["revision_count"] < self.settings.max_revisions
        if report.needs_research and not state.get("scope_accepted"):
            return "deepen" if can_revise and state["research_rounds"] < self.settings.max_research_passes else "research_review"
        return "write" if report.needs_revision and can_revise else "assets"
