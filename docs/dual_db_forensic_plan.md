# Dual-DB Forensic Plan (pre-implementation)

## 1) Current state (audit)
- **Primary DB (single SQLite):** `paths.resolve_app_paths` provides one `db_path` (default `%LOCALAPPDATA%/KajovoSpend/kajovospend.sqlite`). All business + workflow tables live together in `src/kajovospend/db/models.py` (suppliers, files, documents, items, standard_receipt_templates, document_page_audit, import_jobs, service_state). Queries in `db_api.py`, writer flows in `service/processor.py`, migrations in `db/migrate.py`, and GUI/service entrypoints (`service_main.py`, `ui/main_window.py`, `ui/main_window_newdb.py`) all bind to this single DB via `db/session.make_engine` / `sessionmaker`.
- **Processing DB (separate already):** `processing_session.py` / `processing_models.py` create a distinct SQLite (`processing.db` default) for ingest queue (`ingest_files` etc.). It is only used for intake staging; main workflow later writes to primary DB.
- **Status filters:** UI dashboards and run stats use `DocumentFile.status` (NEW/PROCESSED/QUARANTINE/DUPLICATE/ERROR) to simulate separation. No physical split.
- **Dashboard/read paths:** `ui/db_api.py` aggregates counts and run stats from the single DB; business numbers depend on `files.status == 'PROCESSED'` filter. Lists and stats derive from same DB.
- **Report/business tables:** Suppliers, Documents, LineItems, StandardReceiptTemplate, DocumentPageAudit all reside in the single DB; same session used for workflow.
- **Import jobs & service state:** `ImportJob` and `ServiceState` tables live in primary DB; service uses them for queue/state tracking.
- **Duplicate/quarantine workflow:** Implemented via `DocumentFile.status` and related logic in `processor.py`; quarantine files remain in the same DB with status flags.
- **Migrations:** `db/migrate.py` handles schema init/idempotent migrations for the single DB, including FTS setup. Processing DB has its own create_all in `processing_session.py`.
- **Entry/startup:** `service_main.py`, `src/kajovospend/__main__.py`, `run_gui.py`, `ui/main_window_newdb.py` all construct a single engine/session bound to one DB path.
- **Tests:** All listed tests assume a single business DB; processing DB separation is not asserted. Duplicate checks and migrations rely on status filters. No tests enforce physical dual DB.
- **CI:** `.github/workflows/ci.yml` runs `pytest tests` with a single-Python 3.11 job; no dual-DB setup/guards.

## 2) Target dual-DB architecture
- Two physical SQLite files with canonical-path guard preventing equality:
  - **Working DB:** workflow/provozní data only — files, import_jobs, service_state, quarantine/duplicate markers, in-progress documents/items/supplier drafts, audit/promotion queue traces.
  - **Production DB:** business/reporting data only — suppliers, documents, items, document_page_audit (final), standard_receipt_templates (if considered business-facing), other reportable views. No quarantine/duplicate/incomplete records.
- Distinct engines, session factories, ORM models + query modules per DB (`working_*`, `production_*`), wired via config/paths keys (e.g., `paths.working_db`, `paths.production_db` with backward-compat migration from legacy single `db_path`).
- Promotion pipeline moves validated complete documents from working -> production deterministically and idempotently, with forensic logging and rollback-on-fail behavior (document stays in working with reason).
- Dashboard/read APIs point exclusively to production DB; operational/queue views point to working DB.
- Migration utility splits legacy single DB into working + production (idempotent), with validation counts and hard fail on ambiguity.
- Forensic guard utilities/tests prove physical separation (distinct files/engines, no status-filter simulation).

## 3) Table/entity mapping (planned)
- **Working DB:** DocumentFile (workflow statuses), ImportJob, ServiceState, in-progress Document and LineItem drafts (pre-promotion), quarantine/duplicate markers, any operational audit trails, processing links.
- **Production DB:** Supplier, Document, LineItem (final), DocumentPageAudit (final), StandardReceiptTemplate, FTS tables linked to production data, any business-facing aggregates.
- **Shared-but-separated:** If a logical entity needs presence in both (e.g., supplier candidate vs confirmed), maintain distinct model classes/tables in each DB with explicit promotion semantics.

## 4) File changes (commit-bound next steps)
- **Existing files to modify:** service_main.py; src/kajovospend/__main__.py; src/kajovospend/utils/paths.py; src/kajovospend/utils/config.py; src/kajovospend/ui/main_window_newdb.py; src/kajovospend/ui/main_window.py; src/kajovospend/db/session.py; src/kajovospend/db/migrate.py; src/kajovospend/db/models.py; src/kajovospend/db/queries.py; src/kajovospend/db/processing_models.py; src/kajovospend/db/processing_session.py; src/kajovospend/service/app.py; src/kajovospend/service/processor.py; src/kajovospend/service/control.py; src/kajovospend/service/control_client.py; src/kajovospend/service/watcher.py; src/kajovospend/ui/db_api.py; docs/README.md; listed tests (7 files).
- **New files to create:** src/kajovospend/db/working_models.py; src/kajovospend/db/production_models.py; src/kajovospend/db/working_session.py; src/kajovospend/db/production_session.py; src/kajovospend/db/working_queries.py; src/kajovospend/db/production_queries.py; src/kajovospend/db/dual_db_guard.py; src/kajovospend/db/dual_db_migrate.py; src/kajovospend/service/promotion.py; src/kajovospend/utils/forensic_dual_db.py; tests/unit/test_dual_db_config.py; tests/unit/test_dual_db_promotion.py; tests/unit/test_dual_db_forensic_guards.py; tests/integration/test_dual_db_migration.py; tests/integration/test_dual_db_dashboard_reads.py; docs/dual_db_migration.md.
- **CI:** Update `.github/workflows/ci.yml` to run dual-DB tests (potential matrix py311/py312/py313 if feasible) and ensure migration/promotion guards executed.

## 5) Implementation plan by layer
- **Config & paths:** Introduce working/production DB paths with canonical guard; maintain backward-compat loader that splits legacy single path into both, triggering migration step. Update UI/service bootstrap to require distinct paths; add validation failure on identical canonical paths.
- **DB models:** Split ORM into `working_models` vs `production_models`; remove workflow-only tables from production; ensure FTS/indices created where needed. Keep processing DB unchanged but re-point ingest flow to working DB.
- **Session factories:** Separate factory modules for working & production; ensure engine options equivalent; store both in runtime context; add guard to prevent cross-use.
- **Migration (legacy -> dual):** Implement `dual_db_migrate.py` to read legacy DB, classify rows, and write to new DBs idempotently; include validation counts and fail-fast on ambiguity or path collision.
- **Promotion:** Add `service/promotion.py` to validate completeness and transfer records atomically from working -> production; ensure idempotent (no duplicates), forensic log; update processor/service to invoke post-extraction.
- **UI read paths:** `db_api.py`, `main_window.py`, `main_window_newdb.py` to read business stats from production DB; operational views (queue/quarantine/duplicates) from working DB; ensure no status-based simulation remains.
- **Service/write paths:** `service_main.py`, `app.py`, `processor.py`, `control*`, `watcher.py` to write to working DB; promotion writes to production DB only after validation.
- **Tests:** Add dual-DB config/promotion/forensic/migration/dashboard integration tests; update existing tests to operate with separate DBs; add anti-cheat assertions (distinct files, engines, no status WHERE trick). Ensure tests runnable locally & in CI.
- **CI:** Adjust workflow to create two temp DB files for tests; run full pytest suite with PYTHONPATH=src; optionally matrix Python 3.11–3.13.
- **Local commands:** `PYTHONPATH=src pytest tests`; `python -m compileall -q src tests`; optional targeted test subsets during dev.

## 6) Risks
- Migration ambiguity between incomplete and complete documents; risk mitigated by fail-fast validation.
- UI regressions if tabs not correctly re-pointed; mitigated by integration tests for dashboard reads.
- Cross-session misuse; mitigated by explicit guard utilities and anti-cheat tests.
- Performance impact from double writes; mitigated via promotion batching and unchanged PRAGMAs.

## 7) CI workflow touch
- Existing `.github/workflows/ci.yml` to be updated to run dual-DB tests; ensure DB paths are isolated temp files and migration tool executed in CI setup.

