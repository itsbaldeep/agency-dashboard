# Agency OS — Roadmap & Open Items

Mission: an autonomous agency platform that builds, deploys, maintains, and markets
web projects — deterministic core, LLMs as tools, humans at the gates that matter.

## Doctrine (the builder must respect these)
- LLMs generate artifacts and decisions-as-proposals; code executes and verifies.
- Every LLM output passes a deterministic validator before entering the ledger.
- One PR per file in flight; client repos (hearth, technoflavour, streamwise) are
  NEVER auto-merged — human merge only. agency-os and agency-dashboard may
  auto-merge when CI is green and no hold exists.
- Every new failure mode found by a human becomes a validator rule or a check.
- Nightly builder hard limits: max 6 items/night, stop when day spend > $2.00,
  stop on two consecutive rollbacks, never touch ROADMAP items marked [HUMAN].

## Phase 0 — Autonomy prerequisites (IN ORDER, blocking)
- [ ] Verify CI actually runs green on a fresh agency-os PR (open one trivially,
      check the checks tab). If red/absent: diagnose via gh run view, fix ci.yml.
      AUTO-MERGE MUST NOT SHIP UNTIL THIS IS GREEN.
- [ ] Auto-merge job (deterministic): scripts/auto-merge.sh as background job,
      every 3 min, gh CLI: merge open worker-authored PRs on agency-os and
      agency-dashboard when checks green AND pr age >= 5 min AND no "hold" label.
      Discord notify per merge. Register as job 11 + crontab + seed sync.
- [ ] Bot: !hold <pr-number> [repo] and !unhold — add/remove the hold label.
- [ ] Machine review pass: in handle_propose_fix after push, one call_zen with a
      model different from the authoring model: "list merge-blocking defects or
      reply CLEAN"; post as PR comment via GitHub API; on non-CLEAN apply hold
      label. Include tokens in task cost.
- [ ] Assistant channel: assistant_messages table (id, channel_id, role, content,
      created_at). Bot: in ASSISTANT_CHANNEL_ID (new env var), treat every
      non-command message as a turn: store, then enqueue task type
      'assistant_turn' with last 20 turns. Worker handle_assistant_turn: build
      context = turns + live state (open PRs via gh, last 10 tasks, pending
      suggestions, running jobs), one call_zen (glm-5.2) with a system prompt
      restricting powers to: enqueue existing task types (emit a JSON action
      block the handler parses and executes as INSERTs), answer questions,
      recommend holds. Post reply to channel. Deterministic action parsing —
      the LLM never gets raw psql or gh.
- [ ] Nightly builder: scripts/roadmap-builder.sh + handle_builder_step: read
      ROADMAP.md from the repo, pick first unchecked non-[HUMAN] item in the
      current phase, enqueue propose_fix (model=glm-5.2, timeout=600) with the
      item text as description, then a follow-up propose_fix to check the item
      off in ROADMAP.md once merged+deployed. Enforce doctrine limits. Register
      as nightly job (1:00 AM) + morning report line in digest.
- [ ] [HUMAN] Raise Zen monthly spend cap deliberately before first builder night.

## Phase 1 — Platform hardening (builder-safe, any order)
- [ ] token_usage wiring: MODEL_PRICING dict keyed by model id (seed real Zen
      prices); compute cost per-model in call_zen; INSERT INTO token_usage on
      every call. Fixes silent mispricing of glm runs.
- [ ] agent_task/ask real cost capture: switch to --format json, reconstruct
      text from events like propose_fix, record tokens/cost.
- [ ] Split worker.py into handlers/ package (one file per handler, DISPATCH
      built by import). Do as ONE dedicated night with nothing else in flight.
- [ ] Worker concurrency: claim with FOR UPDATE SKIP LOCKED; systemd template
      unit agency-worker@.service; run 2 workers.
- [ ] Fix function-local "import os, subprocess, tempfile" in handle_propose_fix
      (module-level imports) — known scoping landmine.
- [ ] self_review v2: include the window's merged PR titles in the prompt so it
      stops re-reporting already-fixed issues; cap failed_txt at 4000 chars.
- [ ] deploy-agency-dashboard: add staleness clause (container start time vs
      last commit) mirroring agency-os deploy clause 3.
- [ ] run-job.sh: raise result detail truncation 1000 -> 4000 chars.
- [ ] tasks.result_ref truncation: already 20000; verify generate_draft path too.
- [ ] [HUMAN] Backups: restic + off-box target (R2/B2/laptop-over-tailnet) for
      both Postgres instances, ClickHouse events, MinIO, /etc/agency. Mandatory
      before any paying client's data lives here. Includes one tested restore.
- [ ] [HUMAN] Re-rotate agencyos DB password (leaked to chat/Discord pre-redaction).

## Phase 2 — Dashboard as the cockpit
- [ ] Projects page: proper table (name linked, badges for agent/black-box,
      base branch, last audited), onboard form gains "black-box site" mode
      taking just name + website URL (INSERT projects with repo_url=website,
      agent_allowed=false, auto-create brand).
- [ ] Project detail polish: Actions card on top; capabilities evidence
      collapsed behind a details toggle; human dates; status badges.
- [ ] Suggestions on project page get Approve/Reject buttons wired to the
      approval-executor convention (approve -> creates the proposed task).
- [ ] Content list gist: include params->>'suggestion' in the COALESCE.
- [ ] Spend page: per-project and per-model breakdowns from token_usage.
- [ ] Task pages: filter bar (type, status, project); auto-refresh when running.

## Phase 3 — Marketing engine (technoflavour is the pilot)
- [ ] [HUMAN] Get 3 real competitor names from the owner; seed competitors table.
- [ ] competitor_content_scan handler: for each competitor, fetch sitemap
      (reuse defend_audit sitemap code), diff against last snapshot stored in a
      content_inventory table (competitor_id, url, lastmod, first_seen), report
      new/updated pages; weekly job; findings into suggestions as content briefs.
- [ ] Draft variants: !draft/dashboard form gains variants=N; one cheap call
      generates N {title, angle, outline} rows into concept_variations;
      dashboard shows pick-one cards; picking queues the full validated draft.
- [ ] publish_capability + publish_config columns on projects; Approve flow
      consults them: none -> preview-only (current), api -> execute publish.
- [ ] WordPress publisher: publish_content step in approval-executor for
      publish_capability='api' with wp_rest config: POST to /wp-json/wp/v2/posts
      (Application Password auth from publish_config), status=draft first,
      trace to events, error -> approval kept + Discord alert.
- [ ] [HUMAN] Ask owner for a WP Application Password; set technoflavour
      publish_config; first approve->WP-draft roundtrip.
- [ ] Refresh/decay attack: for own+client sitemaps, list pages older than N
      months as refresh briefs into suggestions (the 2024-12-11 finding, as a
      product).
- [ ] Findings->suggestions bridge: defend_audit defects (alt text, gmail in
      Organization schema, xmlrpc enabled) auto-filed as pending suggestions
      with plain-language rationale.
- [ ] Image slots v1: render image_slots in preview as styled placeholders with
      alt+prompt visible (generation comes later).

## Phase 4 — Signals & learning (after a real owner is engaged)
- [ ] signals table (project_id, source, metric, dimension, value, captured_at).
- [ ] GSC connector: service-account OAuth per project (config in
      publish_config-style jsonb), nightly collector job.
- [ ] GA4 connector: same pattern.
- [ ] PageSpeed API collector into signals (free, no auth).
- [ ] Weekly marketing decision job: reads signals + capabilities + gaps +
      content inventory; files a prioritized plan into suggestions (the "CEO
      SEO agent" as one scheduled task).
- [ ] experiments table (action, project, baseline jsonb, started_at,
      review_at); monthly review job comparing signal deltas; seasonal
      weighting via Trends later.

## Parked (deliberate)
- Local SEO / GBP management, ecommerce SEO, ads/WhatsApp/RCS, PR marketplace,
  third-party influence tracking, image generation, hearth blog build
  (recon done: no blog exists; needs owner decision), Weft extraction from
  worker.py, streamwise revival.
