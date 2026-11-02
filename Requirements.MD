# Content Engine requirements and implementation status

Phase 1 uses defined prompts, LangGraph, Nebius Token Factory, Keenable search,
SQLite, evaluation, bounded rewriting, and human review. The remaining phase-1
workflow is implemented. Keep code separated by responsibility and review it
in the order shown in README.md. Publication integrations remain phase 2.

## Author voice and structure — implemented

- Ground claims in supplied evidence and relevant theories; distinguish evidence,
  interpretation, and uncertainty. Do not invent the author's biography.
- Warmth, heart, analogy, and naturally hypothetical scenes are appropriate when
  useful. Documented stories need attribution. Use descriptive language moderately.
- Technical articles need a concrete use case, design explanation, and tradeoffs.
- Structure: honest hook, one or two premise paragraphs, organized detail, and a
  conclusion with an optional relevant call to action.
- LinkedIn: a professional, concise article, normally 300–500 words; direct prose
  with restrained imagery. Medium: a fuller 800–1,200-word article with warmth,
  storytelling, explanation, and practical depth. Avoid padding and unsupported
  assertions to satisfy a length target.
- Broad topic-only inputs ask one focused question with useful choices and a
  delegation option. Explicit delegation or sufficient context produces a brief
  without requesting permission again. No answer means the session remains paused.

## Research and angle review — implemented

- Compare proposed topic/argument metadata with approved or imported articles
  before research. Repeated angles pause for human direction; a shared topic
  alone is not a duplicate. This is metadata comparison, not embedding retrieval.
- SQLite stores both history and checkpoints in separate databases. Existing
  articles can be imported. An archive over the 100-article comparison budget
  requests human direction rather than silently ignoring older work.
- Initial Keenable search keeps up to eight distinct valid URLs and 4,000
  characters per excerpt. Fewer than five usable sources pauses before drafting.
  Search order is preserved; ranking and source counts do not verify credibility.
- Independently assess central claim support and argument substance after writing.
  Material gaps allow one automatic deeper pass by default: up to two focused
  searches and two full-page fetches from already-known source URLs. Fetched
  content is bounded at 16,000 characters. Unsupported peripheral claims can be
  removed by revision instead of expanding the research scope.
- Unresolved material gaps pause with the brief, query, sources, findings, and
  follow-up suggestions. Humans can change the query, explicitly narrow the
  scope with at least five sources, or cancel. These actions are traced.
- Distinguish an empty/insufficient search from a provider failure. Provider
  failures preserve a checkpoint for an explicit retry.

## Editorial evaluation and rewriting — implemented

- An independent LLM call evaluates central claims across both drafts, substance,
  voice, structure, and visual consistency. Model judgment is fallible.
- Code validates supported quote anchors against supplied evidence and checks
  citation URLs, article lengths, embedded-image links, and visual-plan counts.
  Finding a quote does not establish its entailment, credibility, or completeness.
- Rewrites receive prior drafts, evaluation findings, and any human feedback.
  Limit automatic rewrites to two by default; exhausting the limit never implies
  a quality pass. Material gaps can instead request human research direction.
- Explicit human-directed revisions are limited to five additional attempts;
  initial/manual research passes are limited to six per session. Cancellation
  remains available. Settings keep these budgets in one place.

## Pictures and review assets — implemented within phase-1 scope

- Return useful visual plans with platform, actual placement, description, caption,
  alt text, and optional Mermaid. At most one LinkedIn visual and three Medium
  visuals. Avoid decorative filler and fabricated image URLs.
- Technical Mermaid diagrams render locally to SVG and PNG. Rendering errors
  remain visible as unresolved assets. Nontechnical image generation/search is
  not configured; the human can attach a local image with ownership/credit or
  explicitly omit a plan with a reason.
- Insert resolved images and show actual placement in review Markdown. If a
  suggested placement cannot be matched, append an illustrations section and
  disclose that fallback in the asset manifest.
- A refresh rebuilds assets from saved evaluated drafts without granting approval.
  Direct review-file edits must be submitted as a draft edit to enter state and
  receive evaluation; refreshing replaces external unsaved review-file edits.

## Human approval and export — implemented

- Persist the human interruption across process restarts using the same session ID.
- Human review shows both articles, evidence, evaluation findings, actual assets,
  and a content version covering the complete snapshot and image hashes.
- Validate responses before consuming the pending interrupt. A changed draft,
  changed asset, or changed evaluation requires review of its current version.
- Ordinary approval requires no unresolved material quality findings. Explicit
  approval with warnings requires a reason. Pending assets must be resolved or
  deliberately omitted regardless of approval type.
- Approval exports Markdown, assets, and a manifest to local storage, then saves
  the approved content to history idempotently. Approval and publication are
  separate states. Every export records `published: false`.
- No network publication, social-media account write, or automated approval is
  part of phase 1.

## Dataset evaluation and traces — implemented, reference curation pending

- Trace prompt construction, model calls, search/fetch, evaluation, human actions,
  asset rendering, export, errors, latency, and provider token usage locally as
  JSON Lines with parent/child span identifiers. Redact configured API keys.
- Keep evaluator-only references out of application inputs. Support development
  and held-out splits, per-case checks, saved JSON reports, and a paired baseline
  using the initial single-call draft with the same frozen evidence.
- The runnable seed contains five intake cases and two technical writing cases.
  Its frozen source summaries and reference criteria are explicitly **candidate**
  data. Human review must promote them to gold. Do not claim that the broader
  36-case Excel scenario plan is already fully curated or evaluated.
- Component evaluations use frozen evidence; controlled graph tests exercise
  interruptions, retries, duplicate-angle review, research/revision budgets,
  asset/version binding, and approved exports. Live smoke runs verify providers
  separately. A model pass rate is not a claim of universal factual accuracy.

## Phase 2 — deliberately deferred

- Route by suitability to LinkedIn, Medium, or both rather than always requiring
  both article versions.
- Support a Medium article plus a concise LinkedIn announcement pointing to its
  real URL after publication. Do not fabricate a published link.
- Support LinkedIn-only articles when suitable.
- Explore **Jev**, as requested, after confirming the intended product and API.
- Add actual publication integrations only after human approval of the exact
  content and assets. RAG/embedding infrastructure and Pinecone are not required
  by the current SQLite metadata archive.

## Browser interface — implemented

- A local interface uses the existing LangGraph runtime and SQLite stores.
- New ideas, clarification answers, history-overlap decisions, and research
  direction are entered through forms instead of JSON files.
- Article review includes LinkedIn/Medium previews, manual text edits, actual
  images and their credits, evidence findings, and a real activity timeline.
- Image attachment and omission require explicit decisions. Text edits preserve
  choices for unchanged visual plans, then re-evaluate and require fresh approval.
- Approval controls reflect unresolved findings and pictures. The server still
  validates the exact current version and asset hashes before export.
- Approved content can be downloaded as a ZIP and read in article history.
  Earlier articles can be imported into that history through the interface.
- Background work stays responsive to read-only viewing. One worker orders
  mutations. Sessions remain available across server restarts; interrupted work
  can be continued explicitly. No automatic approval or online publication.
- This is a local single-user application, not a hosted multi-user deployment.


## Evaluation dashboard — implemented

- Save automatic 0/1 results and reasons for exact evaluated draft versions.
- Keep human quality labels separate from automatic gates and export approval;
  approval records 1, requested changes/text edits record 0, cancellation is unlabelled.
- Record human-label corrections in an audit table. Edited versions require new labels.
- Calculate precision, recall, F1, agreement, confusion counts and coverage only
  from paired automatic/human results. Undefined denominators remain unavailable.
- Show previous frozen-dataset reports and allow selected dev/test cases and an
  optional initial-draft baseline. Explain per-case pass/fail, route, critic and rubric checks.
- Persist benchmark state and JSON results; interrupted jobs never rerun automatically.
- Keep candidate-reference status visible. Do not equate software test passes,
  LLM rubric scores, or reviewed production versions with certified held-out quality.
