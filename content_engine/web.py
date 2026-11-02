"""Local Content Engine web application. Run: python -m content_engine serve."""

import argparse
import base64
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl, StrictInt
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .archive import ArticleArchive
from .config import PROJECT_DIR, Settings
from .models import EditorialBrief
from .metrics import ReviewMetrics, assess
from .evaluation_runs import EvaluationRuns
from .evals import GoldenEvaluator
from .runtime import WorkflowRuntime
from .studio import WorkflowJobs, activity, render_article, summary
from .tracing import RunTrace
from .visuals import VisualService, digest, file_digest


class UIInput(BaseModel):
    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class NewArticle(UIInput):
    request: str = Field(min_length=1, max_length=12000)


class ImageUpload(UIInput):
    version: str
    content: str = Field(max_length=14_000_000)
    credit: str = Field(min_length=1, max_length=1000)


class HistoryImport(UIInput):
    brief: EditorialBrief
    content: str = Field(min_length=1, max_length=500_000)
    source_url: HttpUrl | None = None


class QualityReview(UIInput):
    version: str
    result: StrictInt = Field(ge=0, le=1)
    notes: str = Field(default="", max_length=6000)


class BenchmarkRequest(UIInput):
    split: Literal["dev", "test", "all"] = "dev"
    compare_baseline: bool = False
    case_ids: list[str] | None = None


def create_app(settings=None, runtime_factory=WorkflowRuntime, evaluator_factory=GoldenEvaluator):
    settings = settings or Settings.from_env()
    jobs = WorkflowJobs(settings, runtime_factory)
    archive = ArticleArchive(settings.articles_db)
    metrics = ReviewMetrics(settings.metrics_db)
    benchmarks = EvaluationRuns(settings, evaluator_factory, legacy_paths=[] if settings.data_dir != PROJECT_DIR / "data" else None)
    # Recover assessments for saved drafts without rerunning research or an LLM.
    for saved in jobs.runtime().sessions():
        if (saved.get("pending") or {}).get("kind") in {"final_review", "research_review"} or saved["state"].get("status") == "approved":
            metrics.capture(saved["state"], "", approved=saved["state"].get("status") == "approved")

    @asynccontextmanager
    async def lifespan(app):
        yield
        jobs.close()
        benchmarks.close()

    app = FastAPI(title="Content Engine", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.jobs = jobs
    app.state.metrics = metrics
    app.state.benchmarks = benchmarks
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_requests(request, call_next):
        if request.method == "POST":
            if request.headers.get("x-content-engine") != "studio":
                return JSONResponse({"detail": "Use the Content Engine interface for this action."}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Cross-site actions are not allowed."}, status_code=403)
            if int(request.headers.get("content-length", "0")) > 15_000_000:
                return JSONResponse({"detail": "The upload is too large."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    def get(key):
        try:
            return jobs.runtime().status(key)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None

    def ensure_idle(key):
        if jobs.job(key).get("status") in {"queued", "running"}:
            raise HTTPException(409, "This draft already has an action in progress.")

    def checked_response(key, response):
        ensure_idle(key)
        snapshot = get(key)
        if not snapshot.get("pending"):
            raise HTTPException(409, "This draft has no pending decision.")
        jobs.runtime().validate_response(snapshot["pending"], response, snapshot["state"])
        return snapshot

    @app.get("/api/sessions")
    def sessions():
        rows = [summary(s, jobs.job(s["thread_id"])) for s in jobs.runtime().sessions()]
        known = {s["id"] for s in rows}
        with jobs.lock:
            waiting = [key for key, job in jobs.jobs.items() if key not in known and job["status"] in {"queued", "running"}]
        rows = [{"id": key, "title": "Starting your article", "status": jobs.job(key)["status"], "pending_kind": None, "has_drafts": False} for key in waiting] + rows
        return {"sessions": rows, "history_count": len(archive.articles()), "configured": bool(settings.nebius_api_key and settings.nebius_model)}

    @app.post("/api/sessions", status_code=202)
    def start(body: NewArticle):
        settings.require_live_credentials()
        key = uuid4().hex
        return {"id": key, "job": jobs.launch(key, "start", request=body.request)}

    @app.get("/api/sessions/{key}")
    def detail(key: str):
        job = jobs.job(key)
        try:
            snapshot = get(key)
        except HTTPException:
            if job:
                return {"thread_id": key, "status": job["status"], "job": job, "state": {}, "pending": None, "articles": {}, "activity": activity(settings, key)}
            raise
        packet = snapshot["state"].get("packet", {})
        assets = packet.get("snapshot", {}).get("assets", [])
        texts = packet.get("snapshot", {}).get("articles") or {k: v for k, v in snapshot["state"].get("drafts", {}).items() if k in {"linkedin", "medium"}}
        assessment = assess(snapshot["state"])
        try:
            assessment = metrics.summary(metrics.get(assessment["id"])) if assessment else None
        except ValueError:
            assessment = None
        return {**snapshot, "assessment": assessment, "status": summary(snapshot, job)["status"], "job": job,
                "articles": {k: render_article(v, key, assets) for k, v in texts.items()}, "activity": activity(settings, key)}

    @app.post("/api/sessions/{key}/respond", status_code=202)
    def respond(key: str, response: dict):
        checked_response(key, response)
        return {"job": jobs.launch(key, "resume", response=response)}

    @app.post("/api/sessions/{key}/retry", status_code=202)
    def retry(key: str):
        ensure_idle(key)
        snapshot = get(key)
        if snapshot.get("pending") or not snapshot.get("next_nodes"):
            raise HTTPException(409, "There is no interrupted action to continue.")
        return {"job": jobs.launch(key, "retry")}

    @app.post("/api/sessions/{key}/refresh", status_code=202)
    def refresh(key: str):
        ensure_idle(key)
        if (get(key).get("pending") or {}).get("kind") != "final_review":
            raise HTTPException(409, "Only a draft awaiting final review can refresh its pictures.")
        return {"job": jobs.launch(key, "refresh")}

    @app.post("/api/sessions/{key}/images/{index}", status_code=202)
    def upload(key: str, index: int, body: ImageUpload):
        ensure_idle(key)
        snapshot = get(key)
        pending = snapshot.get("pending") or {}
        if pending.get("kind") != "final_review" or body.version != pending.get("version"):
            raise HTTPException(409, "Review version changed; refresh this page before attaching a picture.")
        if not 0 <= index < len(snapshot["state"]["drafts"]["visuals"]):
            raise HTTPException(422, "Unknown picture plan")
        try:
            content = base64.b64decode(body.content, validate=True)
        except ValueError:
            raise HTTPException(422, "Invalid image upload") from None
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "Images must be no larger than 10 MiB")
        directory = settings.data_dir / "uploads" / key
        directory.mkdir(parents=True, exist_ok=True)
        raw = directory / f"{uuid4().hex}.upload"
        raw.write_bytes(content)
        try:
            target = VisualService._attach({"path": str(raw), "credit": body.credit}, directory, index)
        finally:
            raw.unlink(missing_ok=True)
        response = {"action": "assets", "version": body.version, "choices": {str(index): {"action": "attach", "path": str(target), "credit": body.credit}}}
        checked_response(key, response)
        return {"job": jobs.launch(key, "resume", response=response)}

    @app.get("/api/sessions/{key}/assets/{filename}")
    def asset(key: str, filename: str):
        snapshot = get(key)
        items = snapshot["state"].get("packet", {}).get("snapshot", {}).get("assets", [])
        item = next((a for a in items if a.get("status") == "ready" and Path(a["path"]).name == filename), None)
        if not item:
            raise HTTPException(404, "Image not found")
        path = Path(item["path"]).resolve()
        if not path.is_relative_to((settings.data_dir / "reviews" / key).resolve()) or not path.is_file():
            raise HTTPException(404, "Image not found")
        if file_digest(path) != item["sha256"]:
            raise HTTPException(409, "This image changed. Refresh the review before approving.")
        return FileResponse(path)

    @app.get("/api/sessions/{key}/download")
    def download(key: str):
        state = get(key)["state"]
        if state.get("status") != "approved":
            raise HTTPException(409, "Approve this version before downloading its export")
        directory = Path(state["export_path"]).resolve()
        if not directory.is_relative_to((settings.data_dir / "exports").resolve()):
            raise HTTPException(404, "Export not found")
        manifest = json.loads((directory / "manifest.json").read_text())
        fields = ["drafts", "sources", "evaluation", "assets", "articles"]
        if manifest.get("approved_version") != state["packet"]["version"] or digest({k: manifest[k] for k in fields}) != state["packet"]["version"]:
            raise HTTPException(409, "Export content changed after approval")
        files = [directory / "manifest.json"]
        for platform, text in manifest["articles"].items():
            if platform not in {"linkedin", "medium"} or (directory / f"{platform}.md").read_text() != text:
                raise HTTPException(409, "Export text changed after approval")
            files.append(directory / f"{platform}.md")
        for item in manifest["assets"]:
            if item["status"] == "ready":
                path = directory / "assets" / Path(item["path"]).name
                if file_digest(path) != item["sha256"]:
                    raise HTTPException(409, "Export image changed after approval")
                files.append(path)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in files:
                bundle.write(path, path.relative_to(directory))
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="article-{key[:8]}.zip"'})

    @app.get("/api/history")
    def history():
        return {"articles": archive.articles()}

    @app.get("/api/history/{key}")
    def previous(key: str):
        article = archive.article(key)
        package = article["package"]
        return {**article, "articles": {k: render_article(package[k]) for k in ["linkedin", "medium", "content"] if k in package}}

    @app.post("/api/history", status_code=201)
    def import_history(body: HistoryImport):
        brief = body.brief.model_dump()
        package = {"content": body.content, "source_url": str(body.source_url) if body.source_url else None}
        version = digest({"brief": brief, "package": package})
        key = "import-" + version[:12]
        trace = RunTrace(settings.data_dir / "runs", console=False)
        with trace.span("import_article", "tool", inputs={"brief": brief}):
            archive.save(key, version, brief, package, "Imported from interface", status="imported")
        return {"id": f"{key}-{version[:12]}", "status": "imported"}

    @app.get("/api/evals")
    def evals_overview():
        return {**metrics.overview(), "runs": benchmarks.rows(), "dataset": benchmarks.catalog()}

    @app.get("/api/evals/assessments/{key}")
    def assessment_detail(key: str):
        item = metrics.get(key)
        snapshot = item["snapshot"]
        articles = {platform: render_article(text, item["thread_id"], snapshot["assets"]).replace(
            f'/api/sessions/{item["thread_id"]}/assets/', f'/api/evals/assessments/{key}/assets/')
            for platform, text in snapshot["articles"].items()}
        return {**item, "articles": articles}

    @app.post("/api/evals/assessments/{key}/review")
    def quality_review(key: str, body: QualityReview):
        return metrics.summary(metrics.review(key, body.version, body.result, body.notes))

    @app.get("/api/evals/assessments/{key}/assets/{filename}")
    def assessment_asset(key: str, filename: str):
        item = metrics.get(key)
        asset = next((a for a in item["snapshot"]["assets"] if a.get("status") == "ready" and Path(a["path"]).name == filename), None)
        if not asset:
            raise HTTPException(404, "Image not found")
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to((settings.data_dir / "reviews" / item["thread_id"]).resolve()) or not path.is_file():
            raise HTTPException(404, "Image not found")
        if file_digest(path) != asset["sha256"]:
            raise HTTPException(409, "This historical image has changed and cannot be shown as reviewed")
        return FileResponse(path)

    @app.get("/api/evals/runs/{key}")
    def benchmark_detail(key: str):
        return benchmarks.get(key)

    @app.post("/api/evals/runs", status_code=202)
    def run_benchmark(body: BenchmarkRequest):
        settings.require_live_credentials()
        return benchmarks.launch(**body.model_dump())

    @app.get("/")
    def home():
        return FileResponse(PROJECT_DIR / "content_engine/ui/index.html")

    app.mount("/static", StaticFiles(directory=PROJECT_DIR / "content_engine/ui"), name="static")
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port)
