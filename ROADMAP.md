# Agency OS — Roadmap (dashboard repo mirror)

> **This file is a mirror. The canonical roadmap is
> `/home/agency/projects/agency-os/ROADMAP.md` (repo: itsbaldeep/agency-os),
> and the strategic context is `/home/agency/projects/agency-os/CEO_DIRECTIVE.md`.
> Read those two first — this file only tracks dashboard-specific items.
>
> State locked: 2026-08-16 (fresh-context handoff). The previous contents of
> this file (auto-merge, nightly builder, assistant channel, autonomy
> prerequisites) described killed parasite work — do not resurrect those plans.

## Mission
A self-hosted AI digital-marketing agency that does real client work for
black-box brands. Dashboard at :5001 is the cockpit and the pitchable surface.

## Doctrine
- LLMs generate artifacts and decisions-as-proposals; code executes and verifies.
- Every LLM output passes a deterministic validator before entering the ledger.
- Black-box first: every feature must work without repo/CMS access.
- Everything visible on the dashboard — if it's not on :5001, it doesn't exist.
- Determinism first, LLM second. Use the cheapest reliable model (free fallback
  chain exists when credits are exhausted — see agency-os ROADMAP Phase 0).

## Dashboard-specific state (DONE)
- [x] Brand audit report page: `/engagements/brand/<id>/report` — executive
      summary, capabilities checklist (severity colors), gated capabilities
      (GSC/WP/Code), per-prompt AI visibility (ClickHouse), competitor
      analysis, capability-gated suggestions with how-to instructions,
      content & drafts, audit history, recent activity, methodology+raw JSON.
      Suggestion approve/reject buttons only for agent_allowed brands;
      "How to implement" text gated on agent_allowed.
- [x] Engagements cockpit: unified clients/projects/brands, hot-first sort
      (last activity), type badges, pending-action counts, report buttons,
      onboarding wizard (name+URL min, optional enrichment fields).
      Deduped + junk removed (brand 22, project 22, 8 junk clients, archived
      infra excluded from list).
- [x] Content UI: clickable rows → preview; sticky action bar (Download /
      Approve / Regenerate / Compose Full Draft for outlines); content_blocks
      rendered as formatted HTML.
- [x] Competitors page: links to brand engagement + report, "why" text,
      baseline-vs-delta scan status, unverified-domain warning chips.
- [x] CSS: cap-grid, severity colors, citation bars, suggestion impact cards,
      eng-card badges, content actionbar.

## Dashboard-specific open items
- [ ] Charts: visibility trend, ranking movement, spend per brand (Chart.js)
- [ ] Per-brand spend breakdown from token_usage
- [ ] Pipeline config UI: brand_pipelines (enabled_stages, schedule_cron,
      Run Now) — table exists in DB, not rendered
- [ ] Wire suggestion Approve → creates a task (still a no-op status flip;
      buttons hidden for black-box brands)
- [ ] Fix activity timeline query (events traced with project="brands", not
      brand name)

## Deploy notes
- Dashboard deploys via job 9 (every 3 min) or manually:
  `cd /home/agency/projects/agency-dashboard && docker compose -f docker-compose.yml up -d --build dashboard`
- Deploy script refuses when uncommitted local changes exist in the repo.
- The dashboard makes NO direct LLM calls — all AI work goes through
  agency-os worker task types (insert a row in `tasks`).