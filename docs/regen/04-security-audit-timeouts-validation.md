# Security audit: timeout/cache validation (ARES)

## Fáze 0 — Mapování projektu

- **Struktura**: hlavní aplikační kód je ve `src/kajovospend/` (service, integrace, OCR, DB, UI), GUI entrypoint je `python -m app_gui` (`app_gui/__main__.py`), service start je přes `service_main.py`.
- **Instalace**: `pip install -r requirements.txt` + editable install `-e .`.
- **Python verze**: dle `pyproject.toml` `>=3.11,<3.15`.
- **Testy**: `pytest` (adresář `tests/`), ale v tomto prostředí nešly spustit kvůli chybějícím balíčkům a blokovanému přístupu na index.
- **CI/quality tooling**: repo má GitHub Actions workflow (`.github/workflows/ci.yml`), dokumentace pro vývoj je v `docs/CONTRIBUTING.md`.
- **Kritické části domény**:
  - síťové integrace (`integrations/ares.py`, `integrations/openai_fallback.py`),
  - background processing (`service/app.py`, `service/processor.py`),
  - práce se soubory (`service/file_ops.py`).

## Fáze 1 — Diagnostika (triage)

1. **Medium** — `src/kajovospend/integrations/ares.py`: chyběla validace `timeout`; při `timeout<=0` dochází k runtime chybě v síťové vrstvě (`requests`) mimo doménově srozumitelnou hlášku.
2. **Medium** — `src/kajovospend/integrations/ares.py`: chyběla validace `cache_ttl_seconds`; záporná TTL vede na neintuitivní chování cache (prakticky vždy cache miss).
3. **Low/Medium** — `src/kajovospend/integrations/ares.py`: `_ARES_CACHE` byla neomezená; při dlouhodobém běhu service hrozí nárůst paměti (DoS-style pressure přes velké množství různých IČO).
4. **BLOCKER (env)** — není možné stáhnout závislosti (`requests`, `sqlalchemy`, ...), protože přístup na package index je blokovaný (HTTP 403 přes proxy), takže nelze spustit plnou regresi.

## Implementované opravy

- Přidána explicitní validace vstupů `timeout` a `cache_ttl_seconds` ve `fetch_by_ico`.
- Přidán horní limit velikosti ARES cache (`MAX_CACHE_SIZE`) + evikce nejstarší položky při přetečení.
- Doplněny regresní testy pokrývající novou validaci.

## Ověření

- `PYTHONPATH=src pytest -q tests/unit/test_ares_timeout_isolation.py` — **NOT PASS** kvůli chybějící závislosti `requests` v prostředí.
