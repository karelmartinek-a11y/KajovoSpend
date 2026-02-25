# REQUIRED FIXES ROADMAP

## Prioritizované kroky

| Priorita | Krok | Velikost | Závislosti | Acceptance criteria |
|---|---|---|---|---|
| P0 | Udržet test baseline zelený při každé změně | S | žádné | `PYTHONPATH=src pytest tests` prochází v CI i lokálně |
| P1 | Stabilizace OpenAI HTTP vrstvy (retry/backoff + klasifikace chyb) | M | žádné | Retry pro 429/5xx + request exception, testy s mocky |
| P1 | Idempotence migrací ověřená 2× během testu | S | DB migrace | Test spustí `init_db` dvakrát bez side-efektů |
| P2 | Housekeeping repozitáře (dočasné soubory v rootu) | S | rozhodnutí maintainera | `tmp*`/artifact soubory odstraněné nebo přesunuté mimo repo |
| P2 | Postupné sjednocení UTC helperu ve všech modulech | M | none | Žádné `datetime.utcnow()` v aktivním kódu |

## Mapování AUD-XXX -> oprava

| AUD | Stav | Implementace |
|---|---|---|
| AUD-001 | Fixed | `src/kajovospend/db/migrate.py` — odstraněn duplicitní karanténní SQL blok |
| AUD-002 | Fixed | `src/kajovospend/integrations/openai_fallback.py` — `_openai_post_with_retry`, retryable statusy, backoff |
| AUD-003 | Fixed (pokryté moduly) | `src/kajovospend/utils/time.py` + náhrady v `db/models.py`, `db/queries.py`, `service/*`, `ui/main_window.py`, `integrations/ares.py` |
| AUD-004 | Fixed | `tests/unit/test_db_net_gross_migration.py` — test idempotence migrace |
| AUD-005 | Fixed | `tests/unit/test_openai_fallback_retry.py` — test retry scénáře 429 -> success |
| AUD-006 | Remaining | Root soubory `tmp.ps1`, `tmp_script.py`, `{line}')` k potvrzení/odstranění |
