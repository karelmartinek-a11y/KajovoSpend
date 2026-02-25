# FORENSIC AUDIT REPORT — KajovoSpend

## Shrnutí
Audit proběhl nad celým repozitářem (zdrojový kód, testy, dokumentace, skripty, konfigurace, artefakty). Byly ověřeny klíčové oblasti: GUI tok importu, OCR/PDF pipeline, SQLite/SQLAlchemy migrace včetně FTS5, OpenAI fallback integrace, ARES integrace, bezpečnost práce se soubory a logováním.

Hlavní zjištění:
- Kritické oblasti (P0) nebyly nalezeny.
- Byly odstraněny konkrétní technické nedostatky (duplicitní migrační blok, chybějící retry wrapper OpenAI, deprecated UTC API).
- Testy byly rozšířeny o idempotenci migrací a retry scénář OpenAI.
- Zůstávají nízkorizikové položky (P2): dočištění dočasných souborů v rootu repozitáře.

## Metodika
1. Inventář souborů a modulů (`rg --files`, kontrola stromu).
2. Read-only průchod soubor po souboru s evidencí nálezů AUD-XXX.
3. Opravy P0/P1 a vybraných P2.
4. Verifikace přes `compileall` a `pytest`.
5. Zápis závěrů + roadmapy.

## Mapování repo

### Entrypointy a spouštění
- GUI: `run_gui.py`, `python -m app_gui`.
- Služba: `service_main.py` + `src/kajovospend/service/*`.
- Skripty: `scripts/*` (download OCR modelů, fixture nástroje).

### Moduly
- `src/kajovospend/ui/` — Qt/PySide6 GUI.
- `src/kajovospend/service/` — orchestrace importu, watcher, processor.
- `src/kajovospend/extract/`, `src/kajovospend/ocr/` — extrakce a OCR/PDF pipeline.
- `src/kajovospend/db/` — modely, session, migrace, dotazy.
- `src/kajovospend/integrations/` — ARES + OpenAI fallback.
- `src/kajovospend/utils/` — config/env/logging/forenzní context.

### Datový tok (zkráceně)
Import souboru -> hash/duplicity -> OCR/PDF text -> parser/extrakce -> enrich (ARES/OpenAI dle konfigurace) -> DB zápis dokumentů/položek -> FTS rebuild -> přesun do OUTPUT/KARANTÉNA -> log audit trail.

## Detailní nálezy AUD-001…

### AUD-001 — Duplicitní migrační blok karantény
- Oblast: `src/kajovospend/db/migrate.py`
- Dopad: výkon + opakované lepení `review_reasons` (datová konzistence)
- Závažnost: P1
- Stav: **Fixed**
- Oprava: odstraněn druhý duplicitní blok SQL update pro karanténu.

### AUD-002 — OpenAI klient neměl jednotné HTTP retry/backoff pro retryable chyby
- Oblast: `src/kajovospend/integrations/openai_fallback.py`
- Dopad: robustnost při 429/5xx, síťových výpadcích
- Závažnost: P1
- Stav: **Fixed**
- Oprava: přidán jednotný wrapper `_openai_post_with_retry` s exponenciálním backoff a klasifikací retryable statusů.

### AUD-003 — Deprecated `datetime.utcnow()` ve více modulech
- Oblast: db/service/ui/integrations
- Dopad: runtime warnings na Python 3.12+, budoucí kompatibilita
- Závažnost: P2
- Stav: **Fixed (pokryté moduly)**
- Oprava: zavedena utilita `utc_now_naive()` a nahrazení volání v aktivních modulech.

### AUD-004 — Chybějící explicitní test idempotence migrace (2× běh)
- Oblast: `tests/unit/test_db_net_gross_migration.py`
- Dopad: riziko regresí migrace
- Závažnost: P1
- Stav: **Fixed**
- Oprava: doplněn test, který spustí `init_db` dvakrát a validuje singleton/index/FTS existenci.

### AUD-005 — Chybějící test retry scénáře OpenAI fallbacku
- Oblast: tests
- Dopad: riziko regresí robustnosti OpenAI volání
- Závažnost: P1
- Stav: **Fixed**
- Oprava: přidán unit test 429 -> retry -> success (mock requests, bez reálného API).

### AUD-006 — Root obsahuje dočasné soubory
- Oblast: `tmp.ps1`, `tmp_script.py`, `{line}')`
- Dopad: údržba, forenzní šum
- Závažnost: P2
- Stav: **Remaining**
- Doporučení: potvrdit původ a odstranit/archivovat mimo repo.

## Sekce DB (schéma, migrace, FTS5, výkon, locking)
- Schéma je rozšířené o VAT/net-gross kompatibilitu, auditní metadata, service state telemetry.
- Migrace používá idempotentní pattern (`IF NOT EXISTS`, kontrola `PRAGMA table_info`, podmíněné `ALTER TABLE`).
- FTS5 tabulky `documents_fts`, `items_fts`, `items_fts2` se vytváří deterministicky.
- Přidány indexy pro lookupy/sort/duplicitní business klíč.
- Doporučení: držet transakční hranice v `engine.begin()` (aktuálně splněno v migraci).

## Sekce OpenAI
- Bezpečnost: API klíč je řešen mimo repo; redakce logů existuje (`redact`, hash/mask).
- Structured output: `json_schema strict`, validační invariants, fallback na `json_object` při kompatibilních 400.
- Robustnost: nově centrální retry/backoff wrapper pro retryable HTTP chyby i request exceptions.
- Testovatelnost: unit testy běží s mockem `requests.post`, bez externích volání.

## Sekce Bezpečnost
- Path traversal: `safe_move` normalizuje cílový název a hlídá destinaci uvnitř cílové složky.
- YAML: načítání přes `yaml.safe_load`.
- Citlivá data: OpenAI logy redigované; report neobsahuje klíče.
- Doporučení: pokračovat v redakční politice a nedávat citlivé config hodnoty do verzování.

## Sekce Testy a provoz
- Test baseline: `PYTHONPATH=src pytest tests`
- Doplňkové ověření: `python -m compileall -q src tests`
- Smoke check entrypointů: import/kompilace modulů proběhla bez pádu.

---

## PULS
**P = Připravenost: 93 %** — kritické oblasti jsou stabilní, testy prochází, P1 nálezy byly opraveny.

**U = Úplnost: 90 %** — audit pokryl celý repo a hlavní rizika; zbývá vyřešit pouze P2 housekeeping (dočasné soubory).

**L = Limitace:** bez reálného OpenAI klíče a produkčních dokladů nelze plně ověřit behaviorální kvalitu extrakce na live datech; GUI runtime smoke zde proběhl bez plné interaktivní uživatelské relace.

**S = Stabilita: 94 %** — evidence: všechny testy `pytest tests` prošly, plus `compileall`; přidány regresní testy na retry OpenAI a idempotenci migrací.
