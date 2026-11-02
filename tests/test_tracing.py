"""Check nested traces, failures, secret redaction, and clean CLI output."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from keenable import SearchResponse, SearchResult

from content_engine.__main__ import main
from content_engine.config import Settings
from content_engine.graph import build_graph
from content_engine.llm import NebiusLLM
from content_engine.models import EditorialBrief
from content_engine.researcher import ResearchNode
from content_engine.search import KeenableSearch
from content_engine.state import ContentState
from content_engine.tracing import RunTrace
from content_engine.writer import WritingNode


PROJECT = Path(__file__).resolve().parent.parent


def read_events(trace: RunTrace) -> list[dict]:
    return [json.loads(line) for line in trace.path.read_text().splitlines()]


class TracingTests(unittest.TestCase):
    def test_nested_graph_trace_includes_prompts_evidence_usage_and_parents(self) -> None:
        settings = Settings(nebius_api_key="private-key", nebius_model="test-model")
        brief = EditorialBrief.model_validate_json((PROJECT / "examples/brief.json").read_text())
        draft_data = json.loads((PROJECT / "examples/drafts.json").read_text())
        draft_data["visuals"] = [{
            "platform": "medium",
            "placement": "After A hypothetical chore-planning task",
            "description": "Show the request, agent actions, and resulting task records.",
            "caption": "Evaluate the saved records against the requested outcome.",
            "alt_text": "A request flows through an agent to task records and an outcome check.",
            "mermaid": "flowchart LR\n  Request --> Agent --> Records --> Check",
        }]
        draft_data["research_gaps"] = ["Find an original source for limitations of final-state evaluation."]
        drafts = json.dumps(draft_data)
        source = SearchResult(title="Evidence", url="https://example.org/evidence", snippet="Check final state.")
        sources = [source] + [
            SearchResult(title="Evidence", url=f"https://example.org/{i}", snippet="Check outcomes.")
            for i in range(4)
        ]
        payload = {
            "model": "test-model",
            "choices": [{"finish_reason": "stop", "message": {"content": drafts}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        }
        response = httpx.Response(200, json=payload, request=httpx.Request("POST", "https://example.org"))
        with tempfile.TemporaryDirectory() as directory:
            trace = RunTrace(Path(directory), secrets=(settings.nebius_api_key,), console=False)
            with patch("content_engine.search.Keenable") as sdk:
                sdk.return_value.__enter__.return_value.search.return_value = SearchResponse("query", sources)
                with patch("content_engine.llm.httpx.post", return_value=response):
                    with trace.span("content_workflow", "workflow"):
                        result = build_graph(
                            WritingNode(NebiusLLM(settings, trace).generate, trace),
                            ResearchNode(KeenableSearch(trace=trace).search, trace),
                        ).invoke(ContentState(brief=brief, revision_count=0))
            events = read_events(trace)
            raw = trace.path.read_text()
        self.assertEqual(len(events), 14)
        starts = {event["name"]: event for event in events if event["event"] == "start"}
        ends = {event["name"]: event for event in events if event["event"] == "end"}
        self.assertEqual(starts["research"]["parent_id"], starts["content_workflow"]["span_id"])
        self.assertEqual(starts["keenable.search"]["parent_id"], starts["research"]["span_id"])
        self.assertEqual(starts["write_drafts"]["parent_id"], starts["content_workflow"]["span_id"])
        self.assertEqual(starts["nebius.chat_completions"]["parent_id"], starts["write_drafts"]["span_id"])
        self.assertEqual(ends["nebius.chat_completions"]["metadata"]["usage"]["total_tokens"], 300)
        self.assertEqual(ends["keenable.search"]["outputs"][0]["url"], source.url)
        self.assertIn(source.snippet, starts["nebius.chat_completions"]["inputs"]["messages"][1]["content"])
        self.assertEqual(ends["validate_drafts"]["outputs"], result["drafts"].model_dump())
        validation_metadata = ends["validate_drafts"]["metadata"]
        self.assertEqual(validation_metadata["visual_count"], 1)
        self.assertTrue(validation_metadata["research_review_needed"])
        self.assertEqual(set(validation_metadata["word_counts"]), {"linkedin", "medium"})
        self.assertNotIn("private-key", raw)
        self.assertNotIn("Authorization", raw)
        self.assertTrue(all(event["duration_ms"] >= 0 for event in ends.values()))

    def test_failed_completion_retains_diagnostics_and_reraises(self) -> None:
        response = httpx.Response(200, json={
            "choices": [{"finish_reason": "length", "message": {"content": "unfinished"}}],
            "usage": {"completion_tokens": 6000},
        }, request=httpx.Request("POST", "https://example.org"))
        with tempfile.TemporaryDirectory() as directory:
            trace = RunTrace(Path(directory), console=False)
            with patch("content_engine.llm.httpx.post", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "output limit"):
                    with trace.span("content_workflow", "workflow"):
                        NebiusLLM(Settings(), trace).generate("prompt")
            events = read_events(trace)
        errors = [event for event in events if event["event"] == "error"]
        self.assertEqual([event["name"] for event in errors], ["nebius.chat_completions", "content_workflow"])
        self.assertEqual(errors[0]["metadata"]["finish_reason"], "length")
        self.assertEqual(errors[0]["metadata"]["usage"]["completion_tokens"], 6000)
        self.assertEqual(errors[0]["outputs"]["content"], "unfinished")

    def test_redaction_private_permissions_and_context_reset_after_failure(self) -> None:
        secret = 'private-"key\\value'
        with tempfile.TemporaryDirectory() as directory:
            trace = RunTrace(Path(directory), secrets=(secret,), console=False)
            with self.assertRaises(ValueError):
                with trace.span("failed", "validation", inputs={"text": secret}) as event:
                    event["outputs"] = secret
                    raise ValueError(secret)
            with trace.span("next", "node"):
                pass
            events = read_events(trace)
            self.assertEqual(trace.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(events[0]["inputs"]["text"], "[redacted]")
        self.assertEqual(events[1]["outputs"], "[redacted]")
        self.assertEqual(events[1]["error"]["message"], "[redacted]")
        self.assertIsNone(events[2]["parent_id"])

    def test_cli_replay_has_progress_and_trace_but_no_fake_external_calls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            args = ["content_engine", str(PROJECT / "examples/brief.json"), "--replay", str(PROJECT / "examples/drafts.json")]
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("sys.argv", args), patch("content_engine.__main__.Settings", return_value=settings):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    main()
            output = json.loads(stdout.getvalue())
            events = [json.loads(line) for line in Path(output["trace_file"]).read_text().splitlines()]
        self.assertTrue(output["mode"].startswith("replay"))
        self.assertIn("start", stderr.getvalue())
        self.assertIn("Trace:", stderr.getvalue())
        self.assertTrue(all(event["kind"] not in {"tool", "llm"} for event in events))
        self.assertTrue(all(event["run_id"] == output["run_id"] for event in events))


if __name__ == "__main__":
    unittest.main()
