"""Verify provider requests and the research-to-writing flow without real keys."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

import httpx
from keenable import Keenable, KeenableAuthError, KeenableRateLimitError, SearchResponse, SearchResult

from content_engine.config import Settings
from content_engine.graph import build_graph
from content_engine.llm import NebiusLLM
from content_engine.models import EditorialBrief
from content_engine.prompts import WRITING_INSTRUCTIONS
from content_engine.researcher import ResearchNode
from content_engine.search import KeenableSearch
from content_engine.state import ContentState
from content_engine.writer import WritingNode


PROJECT = Path(__file__).resolve().parent.parent
SETTINGS = Settings(nebius_api_key="test-nebius-key", keenable_api_key="test-search-key", nebius_model="test-model")
RESULT = SearchResult(title="Agent evaluation", url="https://example.org/evals", snippet="Check final state.")


class IntegrationTests(unittest.TestCase):
    def test_known_page_fetch_is_bounded_and_keeps_original_source_identity(self):
        source = KeenableSearch()
        from content_engine.models import Source
        evidence = Source(title="Known source", url="https://example.org/article", content="An excerpt")
        with patch("content_engine.search.Keenable") as sdk:
            client = sdk.return_value.__enter__.return_value
            client.fetch.return_value = SimpleNamespace(title="Full article", content="x" * 17000)
            fetched = source.fetch(evidence)
        client.fetch.assert_called_once_with(str(evidence.url))
        self.assertEqual(fetched.url, evidence.url)
        self.assertEqual(len(fetched.content), 16000)

    def test_empty_full_page_is_reported_as_research_warning_not_valid_evidence(self):
        from content_engine.models import Source
        evidence = Source(title="Known source", url="https://example.org/article", content="An excerpt")
        with patch("content_engine.search.Keenable") as sdk:
            sdk.return_value.__enter__.return_value.fetch.return_value = SimpleNamespace(title="Empty", content=" ")
            with self.assertRaisesRegex(RuntimeError, "no readable page"):
                KeenableSearch().fetch(evidence)
    def test_research_evidence_reaches_nebius_before_drafts_are_validated(self) -> None:
        brief = EditorialBrief.model_validate_json((PROJECT / "examples/brief.json").read_text())
        drafts = (PROJECT / "examples/drafts.json").read_text()
        events = []

        def search(**kwargs):
            events.append("search")
            self.assertIn(brief.angle, kwargs["query"])
            self.assertEqual(kwargs["mode"], "pro")
            self.assertEqual(kwargs["snippet_max_length"], 4000)
            results = [RESULT] + [
                SearchResult(title="Evidence", url=f"https://example.org/{i}", snippet="Check outcomes.")
                for i in range(4)
            ]
            return SearchResponse(query=kwargs["query"], results=results)

        def complete(request: httpx.Request) -> httpx.Response:
            events.append("generate")
            self.assertEqual(str(request.url), "https://api.tokenfactory.nebius.com/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-nebius-key")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], "test-model")
            self.assertEqual(payload["messages"][0], {"role": "system", "content": WRITING_INSTRUCTIONS})
            self.assertIn(RESULT.url, payload["messages"][1]["content"])
            self.assertIn(RESULT.snippet, payload["messages"][1]["content"])
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertGreater(payload["presence_penalty"], 0)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": drafts}}]})

        tool = KeenableSearch(SETTINGS.keenable_api_key)
        with httpx.Client(transport=httpx.MockTransport(complete)) as client:
            with patch("content_engine.search.Keenable") as sdk:
                sdk.return_value.__enter__.return_value.search.side_effect = search
                with patch("content_engine.llm.httpx.post", side_effect=client.post):
                    result = build_graph(
                        WritingNode(NebiusLLM(SETTINGS).generate), ResearchNode(tool.search)
                    ).invoke(ContentState(brief=brief, revision_count=0))
        self.assertEqual(events, ["search", "generate"])
        self.assertEqual(str(result["sources"][0].url), RESULT.url)
        self.assertEqual(result["drafts"].model_dump(), json.loads(drafts))

    def test_nebius_http_errors_do_not_echo_response_body(self) -> None:
        response = httpx.Response(401, text="sensitive-test-detail", request=httpx.Request("POST", "https://example.org"))
        with patch("content_engine.llm.httpx.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as failure:
                NebiusLLM(SETTINGS).generate("prompt")
        self.assertNotIn("sensitive-test-detail", str(failure.exception))

    def test_nebius_rejects_truncation_refusal_empty_and_malformed_responses(self) -> None:
        payloads = [
            {},
            {"choices": []},
            {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "refusal": "refused"}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": " "}}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                response = httpx.Response(200, json=payload, request=httpx.Request("POST", "https://example.org"))
                with patch("content_engine.llm.httpx.post", return_value=response):
                    with self.assertRaises(RuntimeError):
                        NebiusLLM(SETTINGS).generate("prompt")

    def test_nebius_timeout_uses_configuration_and_has_a_clear_error(self) -> None:
        settings = Settings(nebius_timeout_seconds=180)
        with patch("content_engine.llm.httpx.post", side_effect=httpx.ReadTimeout("provider-detail")) as post:
            with self.assertRaisesRegex(RuntimeError, "timed out after 180s"):
                NebiusLLM(settings).generate("prompt")
        self.assertEqual(post.call_args.kwargs["timeout"], 180)

    def test_nebius_reports_token_exhaustion_separately_from_refusal(self) -> None:
        payload = {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        response = httpx.Response(200, json=payload, request=httpx.Request("POST", "https://example.org"))
        with patch("content_engine.llm.httpx.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "6000-token output limit"):
                NebiusLLM(SETTINGS).generate("prompt")

    def test_keenable_deduplicates_and_bounds_valid_evidence(self) -> None:
        tool = KeenableSearch("test-key")
        results = [RESULT, RESULT, SearchResult(title="Invalid URL", url="javascript:alert(1)", snippet="Text")]
        results += [SearchResult(title="Evidence", url=f"https://example.org/{i}", snippet="x" * 5000) for i in range(8)]
        with patch("content_engine.search.Keenable") as sdk:
            sdk.return_value.__enter__.return_value.search.return_value = SearchResponse(query="query", results=results)
            sources = tool.search("query")
        self.assertEqual(len(sources), 8)
        self.assertEqual(len({str(source.url) for source in sources}), 8)
        self.assertEqual(str(sources[0].url), RESULT.url)
        self.assertTrue(all(len(source.content) <= 4000 for source in sources))

    def test_empty_search_stops_before_generation(self) -> None:
        brief = EditorialBrief(topic="Topic", audience="Reader", angle="Angle", goal="Goal")
        generate = Mock()
        tool = KeenableSearch("test-key")
        with patch("content_engine.search.Keenable") as sdk:
            sdk.return_value.__enter__.return_value.search.return_value = SearchResponse(query="query", results=[])
            with self.assertRaisesRegex(RuntimeError, "no usable sources"):
                build_graph(WritingNode(generate), ResearchNode(tool.search)).invoke(
                    ContentState(brief=brief, revision_count=0)
                )
        generate.assert_not_called()

    def test_fewer_than_five_sources_stop_before_generation(self) -> None:
        brief = EditorialBrief(topic="Topic", audience="Reader", angle="Angle", goal="Goal")
        generate = Mock()
        results = [
            SearchResult(title="Evidence", url=f"https://example.org/{i}", snippet="Text")
            for i in range(4)
        ]
        with patch("content_engine.search.Keenable") as sdk:
            sdk.return_value.__enter__.return_value.search.return_value = SearchResponse("query", results)
            with self.assertRaisesRegex(RuntimeError, "4 usable sources; at least 5"):
                build_graph(WritingNode(generate), ResearchNode(KeenableSearch().search)).invoke(
                    ContentState(brief=brief, revision_count=0)
                )
        generate.assert_not_called()

    def test_keenable_errors_do_not_echo_provider_detail(self) -> None:
        for error_type, status in [(KeenableAuthError, 401), (KeenableRateLimitError, 429)]:
            with self.subTest(status=status):
                with patch("content_engine.search.Keenable") as sdk:
                    sdk.return_value.__enter__.return_value.search.side_effect = error_type("sensitive-test-detail", status)
                    with self.assertRaisesRegex(RuntimeError, error_type.__name__) as failure:
                        KeenableSearch("test-key").search("query")
                self.assertNotIn("sensitive-test-detail", str(failure.exception))

    def test_keenable_sdk_uses_public_or_authenticated_endpoint(self) -> None:
        for key, suffix in [("", "/public"), ("test-key", "")]:
            with self.subTest(keyless=not key):
                def respond(request: httpx.Request) -> httpx.Response:
                    self.assertEqual(str(request.url), f"https://api.keenable.ai/v1/search{suffix}")
                    self.assertEqual(request.headers.get("X-API-Key"), key or None)
                    payload = json.loads(request.content)
                    self.assertEqual(payload["query"], "query")
                    self.assertEqual(payload["snippet_max_length"], 4000)
                    return httpx.Response(200, json={"results": [{"title": RESULT.title, "url": RESULT.url, "snippet": RESULT.snippet}]})

                with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
                    client = Keenable(api_key=key, base_url="https://api.keenable.ai", client=transport)
                    with patch("content_engine.search.Keenable", return_value=client):
                        sources = KeenableSearch(key).search("query")
                self.assertEqual(sources[0].content, RESULT.snippet)

    def test_project_env_loading_preserves_shell_values_and_hides_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".env").write_text(
                "NEBIUS_API_KEY=file-key\nKEENABLE_API_KEY=file-search-key\nNEBIUS_MODEL=file-model\n"
            )
            with patch.dict(os.environ, {"NEBIUS_API_KEY": "shell-key"}, clear=True):
                with patch("content_engine.config.PROJECT_DIR", project):
                    settings = Settings.from_env()
                    settings.require_live_credentials()
        self.assertEqual(settings.nebius_api_key, "shell-key")
        self.assertEqual(settings.keenable_api_key, "file-search-key")
        self.assertEqual(settings.nebius_model, "file-model")
        self.assertNotIn("shell-key", repr(settings))
        self.assertNotIn("file-search-key", repr(settings))

    def test_live_preflight_allows_keyless_search(self) -> None:
        Settings(nebius_api_key="test-key", nebius_model="test-model").require_live_credentials()


if __name__ == "__main__":
    unittest.main()
