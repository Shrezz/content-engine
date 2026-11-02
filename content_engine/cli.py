"""Commands for complete, durable editorial sessions."""

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from .config import Settings
from .runtime import WorkflowRuntime
from .tracing import RunTrace


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Start from a raw request or structured brief")
    start.add_argument("request", nargs="?")
    start.add_argument("--brief", type=Path)
    start.add_argument("--id", default=None)
    for name in ["resume", "status", "retry", "refresh"]:
        command = commands.add_parser(name)
        command.add_argument("id")
        if name == "resume":
            command.add_argument("--response", type=Path, required=True, help="JSON human response")
    archive = commands.add_parser("import-article", help="Import an existing article and its angle into local history")
    archive.add_argument("article", type=Path, help="JSON with a brief object and nonempty content string")
    evaluate = commands.add_parser("evaluate", help="Evaluate a frozen golden dataset")
    evaluate.add_argument("dataset", type=Path)
    evaluate.add_argument("--split", choices=["dev", "test", "all"], default="test")
    evaluate.add_argument("--baseline", action="store_true", help="Compare with one-call writing without research/revision loops")
    evaluate.add_argument("--case", action="append", dest="case_ids", help="Limit to specified case IDs")
    evaluate.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    trace = None
    thread_id = getattr(args, "id", None) or uuid4().hex
    try:
        settings = Settings.from_env()
        if args.command != "status":
            if args.command not in {"refresh", "import-article"}:
                settings.require_live_credentials()
            trace = RunTrace(settings.data_dir / "runs", secrets=(settings.nebius_api_key, settings.keenable_api_key))
            print(f"Trace: {trace.path}", file=sys.stderr, flush=True)
            if args.command in {"start", "resume", "retry", "refresh"}:
                print(f"Session: {thread_id}", file=sys.stderr, flush=True)
        if args.command == "import-article":
            from .archive import ArticleArchive
            from .models import EditorialBrief
            from .visuals import digest
            article = json.loads(args.article.read_text())
            if not isinstance(article, dict):
                raise ValueError("An imported article must be a JSON object")
            brief = EditorialBrief.model_validate(article.get("brief")).model_dump()
            content = article.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("An imported article requires nonempty content")
            package = {"content": content, "source_url": article.get("source_url")}
            version = digest({"brief": brief, "package": package})
            key = "import-" + version[:12]
            with trace.span("import_article", "tool", inputs={"path": args.article, "brief": brief}):
                ArticleArchive(settings.articles_db).save(key, version, brief, package, str(args.article.resolve()), status="imported")
            result = {"article_id": key + "-" + version[:12], "status": "imported", "database": str(settings.articles_db)}
        elif args.command == "evaluate":
            from .evals import GoldenEvaluator
            result = GoldenEvaluator(settings, trace).run(args.dataset, split=args.split, compare_baseline=args.baseline, case_ids=args.case_ids)
            target = args.output or settings.data_dir / "evaluations" / f"{trace.run_id}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            result["report_file"] = str(target.resolve())
        else:
            runtime = WorkflowRuntime(settings, trace)
            if args.command in {"status", "refresh"}:
                if args.command == "refresh":
                    with trace.span("refresh_review", "workflow", inputs={"thread_id": thread_id}):
                        result = runtime.refresh(thread_id)
                else:
                    result = runtime.status(thread_id)
            else:
                with trace.span("editorial_workflow", "workflow", inputs={"thread_id": thread_id, "command": args.command}) as event:
                    if args.command == "start":
                        brief = json.loads(args.brief.read_text()) if args.brief else None
                        result = runtime.start(thread_id, request=args.request, brief=brief)
                    elif args.command == "resume":
                        result = runtime.resume(thread_id, json.loads(args.response.read_text()))
                    else:
                        result = runtime.retry(thread_id)
                    event["outputs"] = result
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f"Could not run {args.command} for {thread_id}: {error}\n")
