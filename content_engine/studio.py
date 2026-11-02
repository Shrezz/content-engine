"""UI services: background workflow jobs, safe previews, and trace summaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from urllib.parse import quote

from markdown_it import MarkdownIt

from .runtime import WorkflowRuntime
from .tracing import RunTrace


LABELS = {"intake": "Understanding your brief", "review_article_history": "Checking earlier articles",
          "research": "Researching your topic", "keenable.search": "Searching the web", "keenable.fetch": "Reading a source",
          "deepen_research": "Looking for stronger evidence", "write_drafts": "Writing your articles",
          "evaluate_drafts": "Reviewing the writing", "prepare_review": "Preparing your review",
          "render_diagram": "Rendering a diagram", "approved_export": "Exporting your approved articles",
          "clarification": "Waiting for your direction", "angle_review": "Waiting for an angle decision",
          "research_review": "Waiting for research direction", "final_review": "Waiting for your review",
          "nebius.chat_completions": "Model call", "human_decision": "Your decision"}


class WorkflowJobs:
    """One worker keeps local graph mutations ordered; HTTP stays responsive."""

    def __init__(self, settings, runtime_factory=WorkflowRuntime):
        self.settings, self.factory = settings, runtime_factory
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="editorial")
        self.lock, self.jobs = Lock(), {}

    def runtime(self, trace=None):
        return self.factory(self.settings, trace)

    def job(self, key):
        with self.lock:
            return dict(self.jobs.get(key, {}))

    def launch(self, key, command, **kwargs):
        with self.lock:
            if self.jobs.get(key, {}).get("status") in {"queued", "running"}:
                raise ValueError("This draft already has an action in progress")
            trace = RunTrace(self.settings.data_dir / "runs", secrets=(self.settings.nebius_api_key, self.settings.keenable_api_key), console=False)
            self.jobs[key] = {"status": "queued", "trace_file": str(trace.path), "command": command}
        self.executor.submit(self._run, key, command, kwargs, trace)
        return self.job(key)

    def _run(self, key, command, kwargs, trace):
        with self.lock:
            self.jobs[key]["status"] = "running"
        try:
            with trace.span("editorial_workflow", "workflow", inputs={"thread_id": key, "command": command}):
                getattr(self.runtime(trace), command)(key, **kwargs)
            with self.lock:
                self.jobs[key]["status"] = "done"
        except Exception as error:
            message = str(error)
            for secret in (self.settings.nebius_api_key, self.settings.keenable_api_key):
                if secret:
                    message = message.replace(secret, "[redacted]")
            with self.lock:
                self.jobs[key].update(status="failed", error=message)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


def activity(settings, key):
    """Read the latest traces for this session, without sending prompts to the UI."""
    rows = []
    files = sorted((settings.data_dir / "runs").glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:150]:
        with path.open() as handle:
            try:
                first = json.loads(next(handle))
            except (StopIteration, ValueError):
                continue
            inputs = first.get("inputs")
            if not isinstance(inputs, dict) or inputs.get("thread_id") != key:
                continue
            events = [first]
            for line in handle:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # A currently-written final line can be incomplete.
        for event in events:
            if event["kind"] not in {"node", "tool", "human", "llm"}:
                continue
            rows.append({"time": event["timestamp"], "name": event["name"], "label": LABELS.get(event["name"], event["name"].replace("_", " ")),
                         "event": event["event"], "kind": event["kind"], "span_id": event["span_id"],
                         "duration_ms": event.get("duration_ms"), "metadata": event.get("metadata", {}),
                         "error": event.get("error", {}).get("message")})
        if len(rows) >= 100:
            break
    return sorted(rows, key=lambda r: r["time"])[-120:]


def render_article(text, key=None, assets=()):
    """Disable raw HTML and remote images; serve only reviewed local assets."""
    parser = MarkdownIt("js-default", {"html": False, "linkify": False})
    known = {Path(a["path"]).name for a in assets if a.get("status") == "ready"}
    tokens = parser.parse(text)

    def walk(items):
        for token in items:
            if token.type == "image":
                src = token.attrGet("src") or ""
                filename = src.removeprefix("assets/")
                if key and src.startswith("assets/") and filename in known:
                    token.attrSet("src", f"/api/sessions/{quote(key)}/assets/{quote(filename)}")
                    token.attrSet("loading", "lazy")
                else:
                    token.type, token.tag, token.nesting = "text", "", 0
                    token.content = "[Image awaiting review]"
                    token.children = None
            if token.type == "link_open":
                token.attrSet("target", "_blank")
                token.attrSet("rel", "noopener noreferrer")
            if token.children:
                walk(token.children)
    walk(tokens)
    return parser.renderer.render(tokens, parser.options, {})


def summary(snapshot, job=None):
    state, pending = snapshot["state"], snapshot.get("pending") or {}
    brief = state.get("brief") or state.get("submitted_brief") or {}
    first = next((c["content"] for c in state.get("conversation", []) if c["role"] == "user"), "Untitled article")
    status = snapshot["status"]
    if status not in {"approved", "cancelled"} and job and job.get("status") in {"queued", "running", "failed"}:
        status = job["status"]
    elif not pending and snapshot.get("next_nodes") and not snapshot.get("errors"):
        status = "interrupted"
    return {"id": snapshot["thread_id"], "title": brief.get("topic", first), "angle": brief.get("angle", ""),
            "status": status, "pending_kind": pending.get("kind"), "has_drafts": bool(state.get("drafts"))}
