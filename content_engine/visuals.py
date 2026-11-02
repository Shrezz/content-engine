"""Local diagrams, reviewed image attachments, and content-bound exports."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

from .config import PROJECT_DIR, Settings
from .models import DraftPackage
from .tracing import RunTrace


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class VisualService:
    """Produce local assets; unresolved illustration plans require attach or omit."""

    def __init__(self, settings: Settings, trace: RunTrace | None = None):
        self.settings = settings
        self.trace = trace or RunTrace()

    def prepare(self, thread_id: str, drafts: DraftPackage, sources: list, evaluation: dict, choices: dict) -> dict:
        root = self.settings.data_dir / "reviews" / thread_id
        asset_dir = root / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        assets = []
        articles = {"linkedin": drafts.linkedin, "medium": drafts.medium}
        for index, plan in enumerate(drafts.visuals):
            choice = choices.get(str(index), {})
            asset = {"index": index, "platform": plan.platform, "caption": plan.caption, "alt_text": plan.alt_text, "status": "pending"}
            if choice.get("action") == "omit":
                if not str(choice.get("reason", "")).strip():
                    raise ValueError("Omitting a visual requires a reason")
                asset.update(status="omitted", reason=choice["reason"])
            else:
                try:
                    if choice.get("action") == "attach":
                        image = self._attach(choice, asset_dir, index)
                        asset["credit"] = choice["credit"]
                    elif plan.mermaid:
                        image = self._diagram(plan.mermaid, asset_dir, index)
                    else:
                        raise ValueError("Attach a local image with credit, or explicitly omit this plan")
                    asset.update(status="ready", path=str(image), sha256=file_digest(image))
                    placement = choice.get("placement", plan.placement)
                    articles[plan.platform], location = self._insert(articles[plan.platform], placement, asset)
                    asset["placement_used"] = location
                except (ValueError, OSError, subprocess.SubprocessError) as error:
                    asset["error"] = str(error)
            assets.append(asset)
        snapshot = {"drafts": drafts.model_dump(), "sources": sources, "evaluation": evaluation, "assets": assets, "articles": articles}
        version = digest(snapshot)
        packet = {"version": version, "directory": str(root), "snapshot": snapshot}
        for platform, text in articles.items():
            (root / f"{platform}.md").write_text(text, encoding="utf-8")
        (root / "review.json").write_text(json.dumps(packet, indent=2, ensure_ascii=False), encoding="utf-8")
        return packet

    def _diagram(self, source: str, directory: Path, index: int) -> Path:
        renderer = PROJECT_DIR / "scripts/render-diagram.mjs"
        fingerprint = hashlib.sha256(source.encode() + renderer.read_bytes()).hexdigest()[:12]
        stem = directory / f"{index}-{fingerprint}"
        image = stem.with_suffix(".png")
        if image.exists():
            return image
        stem.with_suffix(".mmd").write_text(source, encoding="utf-8")
        with self.trace.span("render_diagram", "tool", inputs={"mermaid": source}) as event:
            try:
                result = subprocess.run(
                    [self.settings.node_binary, str(renderer), str(stem.with_suffix(".mmd")), str(stem)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
            except FileNotFoundError:
                raise ValueError("Node is unavailable. Set NODE_BINARY or attach/omit this visual.") from None
            if result.returncode or not image.exists():
                raise ValueError(f"Diagram could not render. Revise Mermaid or attach/omit it. {result.stderr[:400]}")
            event["outputs"] = {"png": str(image), "svg": str(stem.with_suffix('.svg'))}
        return image

    @staticmethod
    def _attach(choice: dict, directory: Path, index: int) -> Path:
        if not str(choice.get("credit", "")).strip():
            raise ValueError("An attached image requires credit / ownership information")
        path = Path(choice.get("path", "")).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Attach an existing image no larger than 10 MiB")
        content = path.read_bytes()
        extension = next((ext for prefix, ext in [(b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif")] if content.startswith(prefix)), None)
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            extension = ".webp"
        if not extension:
            raise ValueError("Only PNG, JPEG, GIF, and WebP attachments are supported")
        target = directory / f"{index}-{hashlib.sha256(content).hexdigest()[:12]}{extension}"
        if path != target:
            target.write_bytes(content)
        return target

    @staticmethod
    def _insert(text: str, placement: str, asset: dict) -> tuple[str, str]:
        alt = asset["alt_text"].replace("[", "").replace("]", "").replace("\n", " ")
        block = f"\n\n![{alt}](assets/{Path(asset['path']).name})\n\n{asset['caption']}"
        if asset.get("credit"):
            block += f"\n\nImage credit: {asset['credit']}"
        candidates = [placement] + re.findall(r"['\"]([^'\"]+)['\"]", placement)
        for candidate in candidates:
            for match in re.finditer(r"(?m)^#{1,6}\s+(.+)$", text):
                if match.group(1).strip().casefold() == candidate.strip().lstrip("# ").casefold():
                    following = re.search(r"(?m)^#{1,6}\s+", text[match.end():])
                    point = match.end() + following.start() if following else len(text)
                    return text[:point].rstrip() + block + "\n\n" + text[point:], "after section: " + match.group(1)
            if candidate in text:
                point = text.index(candidate) + len(candidate)
                paragraph_end = re.search(r"\n\s*\n", text[point:])
                point = point + paragraph_end.start() if paragraph_end else len(text)
                return text[:point] + block + text[point:], "after matched paragraph"
        return text.rstrip() + "\n\n## Illustrations" + block + "\n", "end of article (placement not matched)"

    @staticmethod
    def verify(packet: dict):
        if digest(packet["snapshot"]) != packet["version"]:
            raise ValueError("Review content changed; prepare and review a new version")
        for platform, text in packet["snapshot"]["articles"].items():
            if (Path(packet["directory"]) / f"{platform}.md").read_text(encoding="utf-8") != text:
                raise ValueError("A review article file changed; submit edited drafts for re-evaluation")
        for asset in packet["snapshot"]["assets"]:
            if asset["status"] == "pending":
                raise ValueError("Resolve every visual by rendering, attaching, or explicitly omitting it")
            if asset["status"] == "ready" and file_digest(Path(asset["path"])) != asset["sha256"]:
                raise ValueError("An image changed after review; prepare and approve a new version")

    def export(self, thread_id: str, packet: dict, approval: dict) -> Path:
        self.verify(packet)
        if approval.get("version") != packet["version"] or approval.get("action") not in {"approve", "approve_with_warnings"}:
            raise ValueError("Explicit approval for this exact version is required")
        target = self.settings.data_dir / "exports" / thread_id / packet["version"][:12]
        (target / "assets").mkdir(parents=True, exist_ok=True)
        for asset in packet["snapshot"]["assets"]:
            if asset["status"] == "ready":
                shutil.copy2(asset["path"], target / "assets" / Path(asset["path"]).name)
                if file_digest(target / "assets" / Path(asset["path"]).name) != asset["sha256"]:
                    raise ValueError("An image changed during export; approve a fresh review version")
        for platform, article in packet["snapshot"]["articles"].items():
            (target / f"{platform}.md").write_text(article, encoding="utf-8")
        manifest = {"approved_version": packet["version"], "approval": approval, "published": False, **packet["snapshot"]}
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        return target
