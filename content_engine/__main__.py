"""Preview, replay, or run Keenable research and Nebius generation."""

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .graph import build_graph
from .llm import NebiusLLM
from .models import EditorialBrief
from .prompts import WRITING_INSTRUCTIONS, build_writing_prompt
from .researcher import ResearchNode
from .search import KeenableSearch
from .state import ContentState
from .tracing import RunTrace
from .writer import WritingNode


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        from .web import main as serve
        serve(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] in {"start", "resume", "status", "retry", "refresh", "evaluate", "import-article"}:
        from .cli import main as workflow_main
        workflow_main(sys.argv[1:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brief", type=Path, help="Path to an editorial brief JSON file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show-prompt", action="store_true", help="Print the writing prompt")
    mode.add_argument("--replay", type=Path, help="Run the graph with a saved JSON response; no LLM call")
    mode.add_argument("--live", action="store_true", help="Search with Keenable, then write with Nebius")
    args = parser.parse_args()
    run_mode = "brief"
    trace = None

    try:
        brief = EditorialBrief.model_validate_json(args.brief.read_text(encoding="utf-8"))
        if args.show_prompt:
            print(f"SYSTEM:\n{WRITING_INSTRUCTIONS}\n\nUSER:\n{build_writing_prompt(brief)}")
            return

        settings = Settings.from_env() if args.live else Settings()
        state = ContentState(brief=brief, revision_count=0)
        if args.live or args.replay:
            run_mode = "live" if args.live else "replay (saved response; no LLM call)"
            trace = RunTrace(
                settings.data_dir / "runs",
                secrets=(settings.nebius_api_key, settings.keenable_api_key),
            )
            print(f"Trace: {trace.path}", file=sys.stderr, flush=True)
            with trace.span("content_workflow", "workflow", inputs={"mode": run_mode, **state}) as event:
                if args.live:
                    settings.require_live_credentials()
                    llm = NebiusLLM(settings, trace)
                    search = KeenableSearch(settings.keenable_api_key, trace)
                    state = build_graph(
                        WritingNode(llm.generate, trace), ResearchNode(search.search, trace)
                    ).invoke(state)
                else:
                    response = args.replay.read_text(encoding="utf-8")
                    writer = WritingNode(generate=lambda _prompt: response, trace=trace)
                    state = build_graph(writer).invoke(state)
                event["outputs"] = state
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"Could not run workflow: {error}\n")

    output = {
        "mode": run_mode,
        "brief": state["brief"].model_dump(),
        "revision_count": state["revision_count"],
        "storage": {
            "articles": str(settings.articles_db),
            "checkpoints": str(settings.checkpoints_db),
        },
    }
    if trace:
        output["run_id"] = trace.run_id
        output["trace_file"] = str(trace.path)
    if args.live:
        output["model"] = settings.nebius_model
        output["research"] = {
            "query": state["research_query"],
            "sources": [source.model_dump(mode="json") for source in state["sources"]],
        }
    if "drafts" in state:
        output["drafts"] = state["drafts"].model_dump()
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
