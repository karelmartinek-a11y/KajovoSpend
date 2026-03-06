# Dual DB – čtení a migrace

- **Working DB**: provozní data (files, import_jobs, service_state, incomplete/quarantine/duplicate dokumenty).  
- **Production DB**: obchodní data (suppliers, documents, line_items, document_page_audit, standard_receipt_templates). Dashboard, statistiky, exporty a per-item search čtou výhradně z production DB.

## Guardy
- `kajovospend.db.dual_db_guard.ensure_separate_databases` odmítne stejné cesty.
- `kajovospend.utils.forensic_dual_db.assert_separate_sessions` kontroluje, že sessiony míří na různé SQLite soubory; `snapshot_counts` vrací rychlý forenzní přehled počtu dokumentů v každé DB.

## Rozdělení čtecích cest
- Business přehledy: `db_api.production_counts`, `db_api.dashboard_counts`, `db_api.run_stats`, `db_api.list_documents`, `db_api.list_items` používají production session; working session se přidává pouze pro doplnění cest k souborům.
- Workflow/ops: karanténa/duplicitní fronty, service_state, import_jobs používají working session.

## Migrace z legacy single DB
- Pro jednorázový split použij `kajovospend.db.dual_db_migrate.migrate_legacy_single_db(legacy, working, production)`. Funkce zkopíruje workflow tabulky do working DB a obchodní tabulky do production DB a selže, pokud cíle nejsou prázdné.
- Nové instalace vytvářejí párové soubory automaticky (`run_gui.py`, `service_main.py`, GUI „Nová DB“).

## Ověření po migraci
- Spusť integraci `tests/integration/test_dual_db_dashboard_reads.py` – ověřuje, že business dashboard ignoruje data z working DB a guard odmítne stejnou DB cestu.
- Pro rychlý check můžeš zavolat `snapshot_counts(working_session, production_session)` a zkontrolovat, že počty odpovídají očekávanému splitu.
