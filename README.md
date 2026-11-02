# Content Engine

A local, resumable editorial workflow for LinkedIn and Medium, using LangGraph,
Nebius Token Factory, Keenable, and SQLite. Phase 1 is implemented. It researches,
writes, evaluates, revises, prepares visuals, and waits for a human decision.
Approved content is exported locally; the application does not publish it.

## Browser interface

```sh
cd /Users/shreyavenkat/Projects/content-engine
uv sync
uv run python -m content_engine serve
```

Open **http://127.0.0.1:8765**. The interface reads the same saved sessions and
article history as the CLI; your existing draft appears automatically.

- **New article:** enter an idea in ordinary language. A broad request asks one
  focused question. You can select a direction, write an answer, or delegate.
- **History check:** when the angle overlaps, see the matching earlier brief,
  read its article, choose a suggested/new angle, allow the overlap, or stop.
- **Research review:** inspect the evidence, refine the query, or explicitly
  narrow the scope when enough sources exist.
- **Article:** switch between LinkedIn and Medium previews or edit the Markdown.
  Saving edits re-evaluates both versions. Picture choices survive text edits
  when their visual plans are unchanged. Changed plans require new choices.
- **Evidence:** read individual findings, checked claims, supplied excerpts, and
  original source links. Scores indicate model judgments, not factual guarantees.
- **Pictures:** inspect actual diagrams, captions, placement, and credits. Upload
  an image or omit a suggestion with a reason. Uploads stay in `data/uploads/`;
  review assets are copied into the session's review directory.
- **Activity:** follow real research, writing, evaluation, and human actions.
- **Approval:** ordinary approval stays disabled while picture choices or quality
  findings are unresolved. Explicitly accepting findings requires a reason.
  Review confirmation binds to the displayed version; the server revalidates it.
  After approval, download the two articles, pictures, and manifest as a ZIP.
- **Evaluations:** inspect automatic 0/1 article gates, record exact-version human
  reviews, track precision/recall/F1, and run or inspect frozen dataset cases.
- **Article history:** search approved/imported topics and angles, read earlier
  work, and import existing articles without preparing a JSON file.

The UI is served by FastAPI, with plain HTML/CSS/JavaScript and no frontend build
step. `web.py` defines HTTP routes; `studio.py` handles background work, trace
summaries, and safe Markdown previews; `ui/` contains the interface. A single
worker keeps graph actions ordered while the browser remains responsive. Graph
state is durable; the in-memory job queue is not. If a server stops mid-run,
restart it and use **Continue run** on the saved session. Keep one server process
for this workspace. `--port` selects another local port if needed.

The server binds to loopback for this local, single-user interface. It has no
multi-user login or hosted deployment. API keys remain on the server; previews
disable raw HTML and unreviewed remote images. Publishing remains a separate
phase-2 integration.

## Setup

```sh
cd /Users/shreyavenkat/Projects/content-engine
uv sync
cp -n .env.example .env
# Install diagram dependencies with Node.js and pnpm available:
pnpm install --frozen-lockfile --ignore-scripts
```

Set `NEBIUS_API_KEY` and the exact `NEBIUS_MODEL` in `.env`. The configured model
used for verification was `Qwen/Qwen3-235B-A22B-Instruct-2507`. `KEENABLE_API_KEY`
is optional for Keenable's public tier. `NODE_BINARY` can be `node` on PATH or an
absolute executable path. Credentials are never included in settings reprs;
`.env`, runtime data, and traces are ignored by Git. Shell variables take priority.

## Start and inspect a session

```sh
# A broad topic pauses for one focused clarification question.
uv run python -m content_engine start 'Write an article on meditation.' --id meditation-01

# A complete brief skips the intake LLM call.
uv run python -m content_engine start --brief examples/brief.json --id agent-evals-01

# Read persisted state without a search or model call.
uv run python -m content_engine status agent-evals-01
```

The CLI returns JSON with `thread_id`, `status`, `pending`, `state`, `errors`,
`next_nodes`, and `trace_file`. `pending.kind` tells you which human response is
needed. A paused session survives process restarts. Nothing resumes merely
because time passed. Use a new ID for a new article.

```text
intake → [clarification if needed] → article-angle review → research → write
                                                           ↓             ↓
                                                     research review ← evaluate
                                                           ↑             ↓
                                                    deeper research / rewrite
                                                                         ↓
                                                         assets → human review
                                                                         ↓
                                                            approved local export
```

The actual conditional edges are in `content_engine/graph.py`. The graph uses
JSON-serializable state, Pydantic boundary validation, and SQLite checkpoints.
Intake, angle comparison, writing, and editorial evaluation are separate LLM
calls with defined prompts. Search, full-page fetching, local diagram rendering,
and export are ordinary tool calls controlled by the graph. The model does not
receive arbitrary filesystem or publishing tools.

## Respond to a human checkpoint

Save the selected response as JSON, then run:

```sh
uv run python -m content_engine resume meditation-01 --response response.json
```

Use the session ID whose checkpoint you are answering. Examples:

**Clarification** — answer in ordinary language or explicitly delegate:

```json
{"answer": "Busy parents with five minutes. Focus on making a habit stick."}
```

```json
{"answer": "You choose a practical angle and audience for me."}
```

**Repeated angle** — select a new thesis, allow the overlap, or cancel:

```json
{"action": "new_angle", "angle": "The maintenance cost of a daily practice"}
```

**Research review** — inspect the query, evidence, and evaluation in `pending`:

```json
{"action": "query", "query": "a more focused search with the desired scope"}
```

```json
{"action": "accept_scope", "feedback": "Limit the argument to the supplied short-term findings; remove claims about long-term outcomes."}
```

A qualified scope requires at least five sources. Fewer sources pause before
writing; refine the query or cancel. Accepting scope does not approve an article.

**Final review** — open `pending.review_directory` and inspect `linkedin.md`,
`medium.md`, `review.json`, the actual assets, and evaluation findings. Every
final-review response must include the full current `pending.version` value:

```json
{"action": "revise", "version": "COPY_CURRENT_VERSION", "feedback": "Explain the database check through one worked example."}
```

```json
{"action": "approve", "version": "COPY_CURRENT_VERSION"}
```

Unresolved quality findings block ordinary approval. A deliberate exception
requires `approve_with_warnings` with that version and a nonempty `reason`.
Unresolved image plans always need attaching or explicitly omitting first.

To submit manual text changes, use `action: "edit"`, the current version, and a
`drafts` object matching `DraftPackage` (`linkedin`, `medium`, `visuals`,
`research_gaps`). This triggers evaluation and a new review. Editing a review
Markdown file alone does not update the checkpoint and cannot be approved.

To resolve a visual plan, use its zero-based index:

```json
{
  "action": "assets",
  "version": "COPY_CURRENT_VERSION",
  "choices": {
    "0": {"action": "omit", "reason": "This illustration adds no useful detail."},
    "1": {"action": "attach", "path": "/absolute/path/image.png", "credit": "Created by the author"}
  }
}
```

Attachments support PNG, JPEG, GIF, and WebP up to 10 MiB. An optional `placement`
can name an actual heading. Assets, captions, and insertion location are included
in the review snapshot. A Mermaid plan renders locally to SVG and PNG; other
pictures require an attachment or omission. No image-generation service is
configured. Diagram syntax unsupported by the renderer remains a pending asset.

Visual choices and draft edits create a fresh version requiring another review.
Approval is bound to the text, evidence, evaluation, and asset hashes. A changed
file or stale version cannot export. `{"action":"cancel"}` works at earlier
gates; at final review include the current version as well.

```sh
# Rebuild review assets from saved drafts after a renderer fix, with no LLM call.
# This overwrites review Markdown from the checkpoint; submit edits first.
uv run python -m content_engine refresh agent-evals-01

# Retry a failed provider/node call from its saved checkpoint.
uv run python -m content_engine retry agent-evals-01
```

Retry may repeat an external request whose response was lost. It never bypasses
a pending human decision. Review actions validate before consuming the interrupt.

## Evidence, limits, and local storage

- Initial search retains 5–8 distinct usable source URLs, with at most 4,000
  characters per excerpt. Rank and count do not establish source quality.
- The independent evaluator checks central claims, quote anchors, citations,
  substance, voice, structure, and visual plans. Code checks length and source
  URLs; a supported quote must exist exactly in the supplied source content.
- One automatic deeper pass can run up to two focused searches and fetch up to
  two known URLs. A fetched excerpt is capped at 16,000 characters. With defaults,
  the accumulated pool is at most 24 sources. Search/fetch cannot supply arbitrary
  URLs proposed outside the evidence set.
- Up to two automatic rewrites are shared across editorial and research work.
  Persistent gaps pause for human research direction. Exhausting a budget never
  means that the draft passed. Explicit human requests allow up to five further
  revisions and six total initial/manual research passes per session.
- Quote existence is not proof of entailment or source truth. The LLM judge can
  miss claims, overstate support, or escalate unnecessarily. Inspect findings and
  sources during human review; do not treat scores as certification.

| Location under `data/` | Purpose |
| --- | --- |
| `checkpoints.sqlite3` | LangGraph sessions, pauses, and resumable state |
| `articles.sqlite3` | Approved/imported article content and brief metadata |
| `reviews/<id>/` | Current articles, actual images, and versioned snapshot |
| `exports/<id>/<version>/` | Human-approved Markdown, assets, and manifest |
| `runs/<run-id>.jsonl` | Parent/child action spans, inputs, outputs, errors, latency, token usage |
| `evaluations/` | Saved dataset evaluation reports |
| `evaluations.sqlite3` | Versioned article assessments, human-label audit, benchmark jobs |

Approved exports have `published: false`. Retries save article history
idempotently. History comparison uses topic/audience/angle/goal metadata, not a
full-text embedding index. The first 100 articles fit the automatic comparison
budget; a larger archive requests human direction. Traces are local, include
full prompts and content, redact configured API keys, and use private file
permissions. They are not a hosted tracing dashboard or a complete PII scrubber.

To include writing that predates this app, prepare JSON with `brief`, nonempty
`content`, and optional `source_url`, then import it:

```sh
uv run python -m content_engine import-article existing-article.json
```

The brief uses the same four fields as `examples/brief.json`. Importing the
same content and brief is idempotent and records status `imported`; it neither
publishes the article nor retroactively approves it through this workflow.

## Evaluation dashboard

Open `http://127.0.0.1:8765/?view=evals` or choose **Evaluations** in the sidebar.
The sequence is **draft → separate LLM editorial review → rewrite and re-review
when needed → human review**. The critic gets the brief, both drafts and supplied
sources in its own model call. It scores factual support, substance, voice and
structure, checks claims and supplies revision or research findings. Code then
validates quotes, citations, length and visual limits. The UI shows the critic
scores separately from the combined quality gate and your final human label.
Each evaluated draft saves an immutable automatic result before human review:
**1** means all quality-gate conditions passed; **0** means changes or review are
needed. The `quality-v1` policy requires all four critic scores >=4/5, no error
findings or further research request, five unique sources, a current review
packet, and resolved picture choices. These are fallible model judgments and
rules, not proof of factual correctness.

Use **Read & review** to inspect the saved LinkedIn/Medium articles and evidence,
then record **1 = acceptable as-is** or **0 = needs changes**. A 0 requires notes.
This rating is independent of approval/export. Approve & export records human 1;
Request changes or a text edit records 0 for the version being replaced. Stopping
an article records no quality label. New versions never inherit old labels.
Labels can be corrected and their changes are retained in an audit table.

The positive class is acceptable as-is. Precision = TP/(TP+FP), recall =
TP/(TP+FN), F1 = 2TP/(2TP+FP+FN). Only labelled versions enter these calculations;
undefined denominators display a dash. The confusion matrix and review coverage
make the sample visible. These are descriptive metrics over reviewed versions,
not an unbiased estimate of performance on new topics.

**Dataset runs** contains saved CLI reports and new benchmarks. Select a split
and cases; optionally compare the initial writing draft against its revisions.
Running uses model credits, frozen evidence, and no live search. Each case gets
a binary result, expected/actual route, rubric explanations, and actual output.
Suite pass rate is separate from the human-labelled article metrics. Background
benchmark jobs and reports persist in SQLite; jobs interrupted by a server
restart are marked interrupted and require an explicit new run. Reports save
under `data/evaluations/`; new reports include prompt/dataset fingerprints and
revision configuration. Old reports retain an unknown prompt-version label.

`metrics.py` owns gates, labels, and metric formulas. `evaluation_runs.py` owns
benchmark jobs and saved reports. `ui/evals.js` owns dashboard and review views.

## Evaluation dataset and baseline

```sh
uv run python -m content_engine evaluate evals/golden.json --split dev --baseline
uv run python -m content_engine evaluate evals/golden.json --split test --case INT-002
```

`evals/golden.json` is a runnable **candidate seed**, with five intake scenarios,
two technical writing scenarios, frozen evidence, and evaluator-only criteria.
The existing 36-scenario Excel workbook is the broader coverage plan, not 36
fully curated executable fixtures. Source content in the seed is explicitly
marked as curated summaries of primary sources, not verbatim page excerpts.
References require human review before promotion to gold.

Writing cases use the frozen sources rather than live search, enabling a
repeatable component comparison. Reference criteria never enter the writer
prompt. A separate reference judge sees the actual output and frozen evidence,
without the runtime evaluator's scores. Writing passes require the expected
route, reference checks, and applicable runtime quality checks. The paired
baseline is the first one-call draft using the same evidence, before any
revisions. Evaluating the baseline still uses grading calls. Reports contain
actual checks, failures, baseline results, model ID, duration, and trace path.
Do not tune prompts on the held-out test split. The controlled graph tests
cover orchestration behaviors that this component dataset does not execute.

## Read the code by responsibility

| Section | Files / classes |
| --- | --- |
| Contracts and prompts | `models.py`, `state.py`, `prompts.py`, decision prompts in `decisions.py` |
| Provider/tool adapters | `llm.py` (`NebiusLLM`), `search.py` (`KeenableSearch`) |
| Writing and evaluation | `writer.py` (`WritingNode`), `evaluator.py` (`DraftEvaluator`) |
| Node behavior and routing | `workflow.py` (`WorkflowNodes`), `graph.py` |
| Persistence and human responses | `runtime.py` (`WorkflowRuntime`), `archive.py` (`ArticleArchive`) |
| Assets and export | `visuals.py` (`VisualService`), `scripts/render-diagram.mjs` |
| Version quality and benchmark jobs | `metrics.py` (`ReviewMetrics`), `evaluation_runs.py` (`EvaluationRuns`), `ui/evals.js` |
| Offline evaluation and traces | `evals.py` (`GoldenEvaluator`), `tracing.py` (`RunTrace`) |
| Entry points and settings | `cli.py`, `__main__.py`, `config.py` |

```sh
uv run python -m unittest discover -s tests -v
node --test tests/test_visual_render.mjs
```

Tests use controlled providers and temporary SQLite databases, without network
calls. Native rendering tests check actual visible pixels in flow and sequence
diagrams. Live research and model smoke runs are separate from unit tests.

The earlier commands remain for inspecting a prompt, replaying saved JSON, or
running just research and generation:

```sh
uv run python -m content_engine examples/brief.json --show-prompt
uv run python -m content_engine examples/brief.json --replay examples/drafts.json
uv run python -m content_engine examples/brief.json --live
```

These legacy commands do not run the complete editorial workflow. Replay tests
the saved-output contract and makes no LLM call.

## Phase 2

Platform suitability, LinkedIn-only output, Medium articles with LinkedIn
announcements, Jev integration, and actual publishing remain deferred, as agreed.
Phase 1 always produces both article formats. See `Requirements.MD` for scope.
