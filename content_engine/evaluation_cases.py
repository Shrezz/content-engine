"""Execute frozen benchmark fixtures through real application components.

Only search responses and starting application state are fixtures. Model decisions,
graph routing, persistence, validation, and rendering use the application code.
"""

from dataclasses import replace
from types import SimpleNamespace

from pydantic import ValidationError

from .archive import ArticleArchive
from .decisions import INTAKE_INSTRUCTIONS, with_delegation_choice
from .metrics import ReviewMetrics
from .models import DraftPackage, EditorialBrief, EvaluationReport, IntakeDecision, Source
from .runtime import WorkflowRuntime
from .visuals import VisualService, digest
from .workflow import WorkflowNodes


class FrozenEvidence:
    def __init__(self, sources, trace):
        self.sources, self.trace, self.calls = sources, trace, 0

    def search(self, query):
        self.calls += 1
        with self.trace.span("frozen_search_fixture", "tool", inputs={"query": query}) as event:
            event["outputs"] = {"sources": self.sources, "live_web_search": False}
        return self.sources

    def fetch(self, source):
        with self.trace.span("frozen_fetch_fixture", "tool", inputs={"url": str(source.url)}) as event:
            event["outputs"] = source
        return source


class ForbiddenGeneration:
    def __init__(self):
        self.writer_calls = self.critic_calls = 0

    def __call__(self, state):
        self.writer_calls += 1
        raise RuntimeError("Writer called before the required input/research boundary")

    def evaluate(self, *args):
        self.critic_calls += 1
        raise RuntimeError("Critic called before the required input/research boundary")


def validate_fixtures(cases, pool):
    """Fail the whole selection before paid calls if any fixture is incomplete."""
    allowed = {"critic", "intake", "clarify", "validation", "research_boundary",
               "research_exhausted", "history", "writer", "workflow", "revision"}
    for case in cases:
        execution = case.get("execution", {})
        if execution.get("kind") not in allowed or not case.get("criteria") or not case.get("definition_approved"):
            raise ValueError(f"Unprepared execution or reference: {case['id']}")
        if digest({"inputs": case["inputs"], "execution": execution}) != case.get("fixture_hash"):
            raise ValueError(f"Fixture hash mismatch: {case['id']}")
        ids = execution.get("source_ids", [])
        sources = [Source.model_validate(pool[key]["source"]) for key in ids]
        for key in ids:
            if digest(pool[key]["source"]) != pool[key].get("sha256"):
                raise ValueError(f"Source hash mismatch: {key}")
        if execution["kind"] in {"writer", "workflow", "revision"} and len({str(s.url) for s in sources}) < 5:
            raise ValueError(f"At least five unique frozen sources required: {case['id']}")
        if execution["kind"] == "critic":
            DraftPackage.model_validate(case["inputs"]["drafts"])
        if execution["kind"] == "revision":
            DraftPackage.model_validate(execution["starting_drafts"])


class DatasetCaseRunner:
    def __init__(self, evaluator):
        self.runner = evaluator
        self.settings, self.trace = evaluator.settings, evaluator.trace

    def runtime(self, case, sources, *, guarded=False, suffix=""):
        local = replace(self.settings, data_dir=self.settings.data_dir / "evaluation-sessions" / self.trace.run_id / (case["id"] + suffix))
        evidence, guard = FrozenEvidence(sources, self.trace), ForbiddenGeneration()
        nodes = WorkflowNodes(local, self.runner.decisions, evidence,
                              guard if guarded else self.runner.writer,
                              guard if guarded else self.runner.evaluator,
                              ArticleArchive(local.articles_db), VisualService(local, self.trace), self.trace)
        return WorkflowRuntime(local, self.trace, nodes), evidence, guard

    def grade(self, case, output, context):
        return self.runner._grade(case["criteria"], output, context, evaluation_target=case["target"])

    @staticmethod
    def check(criterion, passed, reason):
        return {"criterion": criterion, "passed": bool(passed), "reason": reason}

    def execute(self, case, pool):
        data, execution = case["inputs"], case["execution"]
        kind = execution["kind"]
        sources = [Source.model_validate(pool[key]["source"]) for key in execution.get("source_ids", [])]
        if execution.get("source_attack"):
            sources[0] = sources[0].model_copy(update={"content": sources[0].content + "\n\nSynthetic untrusted source text: " + execution["source_attack"]})
        context = {**data, "sources": [s.model_dump(mode="json") for s in sources]}
        quality, checks = True, []
        if kind == "critic":
            evaluation = self.runner.evaluator.evaluate(EditorialBrief.model_validate(data["brief"]), DraftPackage.model_validate(data["drafts"]), sources)
            decision = "research_review" if evaluation.needs_research else "revision" if evaluation.needs_revision else "human_review"
            output = {"decision": decision, "evaluation": evaluation.model_dump()}
            graded = {"decision": decision, "claims": [c.model_dump() for c in evaluation.claims],
                      "issues": [i.model_dump() for i in evaluation.issues], "needs_research": evaluation.needs_research}
            checks = self.grade(case, graded, context)
            quality = evaluation.faithfulness <= 3 and any(c.verdict in {"unsupported", "conflicting"} for c in evaluation.claims)
        elif kind == "validation":
            try:
                EditorialBrief.model_validate(data["brief"])
                decision, output = "accepted", {}
            except ValidationError as error:
                decision, output = "validation_error", {"errors": error.errors()}
                checks = [self.check("Missing angle is rejected before any service calls", any(e["loc"] == ("angle",) and e["type"] == "missing" for e in error.errors()), "Pydantic validation of the actual structured input")]
        elif kind == "intake":
            output = with_delegation_choice(self.runner.decisions.ask("golden_intake", INTAKE_INSTRUCTIONS, IntakeDecision, data)).model_dump(mode="json")
            decision = output["status"]
            checks = self.grade(case, output, context)
        elif kind == "writer":
            drafts = self.runner.writer({"brief": EditorialBrief.model_validate(data["brief"]), "sources": sources})["drafts"]
            output = {"drafts": drafts.model_dump(), "decision": "writer_output"}
            decision = "writer_output"
            checks = self.grade(case, output, context)
        elif kind == "revision":
            return self.revision(case, sources)
        else:
            runtime, evidence, guard = self.runtime(case, sources, guarded=kind != "workflow")
            thread = "eval-" + case["id"].lower()
            if kind == "history":
                for index, brief in enumerate(execution["archive_briefs"]):
                    runtime.nodes.archive.save(f"fixture-{index}", "synthetic-import", brief,
                                               {"linkedin": "Synthetic archive fixture", "medium": "Synthetic archive fixture", "visuals": [], "research_gaps": []}, "fixture-only")
                # Execute real intake, novelty and research, stopping before generation.
                with runtime.session(thread) as (graph, config):
                    graph.invoke({"thread_id": thread, "brief": data["brief"], "submitted_brief": data["brief"],
                                  "conversation": [], "revision_count": 0, "research_rounds": 0, "human_revisions": 0,
                                  "status": "running"}, config, interrupt_before=["write", "research_review"])
                    result = runtime.finish(graph.get_state(config))
                state = result["state"]
                decision = "angle_review" if state["novelty"]["duplicate"] else "research"
                output = {"decision": decision, "novelty": state["novelty"], "pending": result.get("pending"), "search_calls": evidence.calls}
                ids = {b["id"] for b in runtime.nodes.archive.briefs()}
                checks = [self.check("Archive decision uses actual stored thesis metadata", set(state["novelty"]["matching_ids"]) <= ids and evidence.calls == int(decision == "research"), f"Loaded {len(ids)} archive records; search calls={evidence.calls}"),
                          self.check("No writer or critic runs before this history boundary", guard.writer_calls == guard.critic_calls == 0, "Guarded generation boundary")]
                if decision == "angle_review":
                    checks.append(self.check("Same-angle fixture is the match", state["novelty"]["matching_ids"] == [runtime.nodes.archive.briefs()[0]["id"]] if len(ids) == 1 else execution["duplicate_angle"] in [b["angle"] for b in runtime.nodes.archive.briefs() if b["id"] in state["novelty"]["matching_ids"]], "Different-thesis articles must not be treated as repeats"))
            else:
                if kind == "research_exhausted":
                    state = {**data, "thread_id": thread, "conversation": [], "sources": context["sources"], "status": "drafted", "human_revisions": 0, "search_query": data["brief"]["topic"]}
                    with runtime.session(thread) as (graph, config):
                        graph.update_state(config, state, as_node="evaluate")
                        graph.invoke(None, config)
                        result = runtime.finish(graph.get_state(config))
                elif data.get("request") or data.get("conversation"):
                    result = runtime.start(thread, request=data.get("request") or data["conversation"][0]["content"])
                else:
                    result = runtime.start(thread, brief=data["brief"])
                state = result["state"]
                pending = (result.get("pending") or {}).get("kind")
                decision = "human_review" if pending == "final_review" else pending or result["status"]
                output = {"decision": decision, "drafts": state.get("drafts"), "evaluation": state.get("evaluation"),
                          "intake": state.get("intake"), "revisions": state.get("revision_count", 0),
                          "research_rounds": state.get("research_rounds", 0), "pending": result.get("pending"),
                          "checkpoint_database": str(runtime.settings.checkpoints_db),
                          "assets": state.get("packet", {}).get("snapshot", {}).get("assets", [])}
                if kind == "workflow":
                    report = EvaluationReport.model_validate(state["evaluation"]) if state.get("evaluation") else None
                    quality = bool(report) and not report.needs_revision and not report.needs_research if execution.get("require_quality", True) else True
                    checks = self.grade(case, {k: v for k, v in output.items() if k != "evaluation"}, context)
                    checks.append(self.check("No automatic approval or export", not state.get("approval") and not state.get("export_path") and not runtime.nodes.archive.briefs(), "This benchmark supplies no human approval"))
                    if state.get("drafts") and report:
                        # Assets remain under the main review root so dashboard routes
                        # can verify and display them without widening path access.
                        review_thread = f"eval-{self.trace.run_id[:12]}-{case['id'].lower()}"
                        review_state = {**state, "thread_id": review_thread}
                        review_state["packet"] = VisualService(self.settings, self.trace).prepare(review_thread,
                            DraftPackage.model_validate(state["drafts"]), state["sources"], state["evaluation"], state.get("visual_choices", {}))
                        assessment = ReviewMetrics(self.settings.metrics_db).record(review_state, self.settings.nebius_model)
                        output.update(assessment_id=assessment["id"], automatic_result=assessment["automatic_result"], human_result=None)
                else:
                    refreshed = runtime.status(thread)
                    checks = [self.check("The human pause persists on refresh", pending == (refreshed.get("pending") or {}).get("kind"), f"Pending checkpoint={pending}"),
                              self.check("No premature generation and correct search boundary", guard.writer_calls == guard.critic_calls == 0 and evidence.calls == (1 if kind == "research_boundary" else 0), f"Search={evidence.calls}, writer={guard.writer_calls}, critic={guard.critic_calls}")]
                    if kind == "clarify":
                        checks += self.grade(case, {"intake": state["intake"], "decision": decision}, context)
        status = decision in case["expected_decisions"]
        return {"id": case["id"], "passed": bool(status and quality and all(c["passed"] for c in checks)),
                "status_matches": status, "runtime_quality_passed": quality, "checks": checks,
                "output": output, "baseline": None}

    def revision(self, case, sources):
        execution = case["execution"]
        checks, outputs = [], []
        for action in ("revise", "edit"):
            runtime, _, _ = self.runtime(case, sources, suffix="-" + action)
            writer, critic = runtime.nodes.writer, runtime.nodes.evaluator
            write_inputs, critic_calls = [], []
            def observed_write(value):
                write_inputs.append(value)
                return writer(value)
            def observed_critic(*args):
                critic_calls.append(args)
                return critic.evaluate(*args)
            runtime.nodes.writer = observed_write
            runtime.nodes.evaluator = SimpleNamespace(evaluate=observed_critic)
            thread = "eval-" + case["id"].lower() + "-" + action
            state = {"thread_id": thread, "brief": case["inputs"]["brief"], "drafts": execution["starting_drafts"],
                     "sources": [s.model_dump(mode="json") for s in sources], "evaluation": execution["starting_evaluation"],
                     "conversation": [], "revision_count": self.settings.max_revisions, "research_rounds": 1,
                     "human_revisions": 0, "status": "drafted", "search_query": case["inputs"]["brief"]["topic"]}
            with runtime.session(thread) as (graph, config):
                graph.update_state(config, state, as_node="evaluate")
                graph.invoke(None, config)
                before = runtime.finish(graph.get_state(config))
            old = before["pending"]["version"]
            response = {"action": action, "version": old}
            if action == "revise":
                response["feedback"] = execution["feedback"]
            else:
                response["drafts"] = {**execution["starting_drafts"], "medium": execution["starting_drafts"]["medium"] + "\n\n" + execution["manual_edit"]}
            after = runtime.resume(thread, response)
            new_state = after["state"]
            labels = runtime.metrics.rows()
            checks.append(self.check(f"{action}: old label is zero; the new reviewed version is unlabelled", any(r["version"] == old and r["human_result"] == 0 for r in labels) and all(r["human_result"] is None for r in labels if r["version"] != old), "Synthetic feedback labels exist only in this isolated fixture database"))
            pending = after.get("pending") or {}
            checks.append(self.check(f"{action}: critic ran again and stale approval was cleared", bool(critic_calls) and not new_state.get("approval") and pending.get("kind") == "final_review" and bool(pending.get("version")) and pending["version"] != old and not new_state.get("export_path"), "New text is evaluated before another human decision"))
            proper_input = (not write_inputs if action == "edit" else bool(write_inputs) and write_inputs[0].get("previous_drafts") == execution["starting_drafts"] and write_inputs[0].get("feedback") == execution["feedback"])
            checks.append(self.check(f"{action}: correct writer boundary and actual feedback", proper_input, f"Writer calls={len(write_inputs)}, critic calls={len(critic_calls)}"))
            checks.append(self.check(f"{action}: changed text matches the requested operation", new_state["drafts"] != execution["starting_drafts"] and (action != "edit" or execution["manual_edit"] in new_state["drafts"]["medium"]), "Inspect the actual saved draft"))
            outputs.append({"action": action, "old_version": old, "new_version": (after.get("pending") or {}).get("version"),
                            "decision": (after.get("pending") or {}).get("kind"), "drafts": new_state["drafts"], "evaluation": new_state["evaluation"]})
        return {"id": case["id"], "passed": all(c["passed"] for c in checks), "status_matches": True,
                "runtime_quality_passed": True, "checks": checks, "output": {"variants": outputs}, "baseline": None}
