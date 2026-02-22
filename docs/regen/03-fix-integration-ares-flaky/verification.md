# Verification – 03-fix-integration-ares-flaky

## Cíl
- opravit pád integračního testu `test_pdf_embedded_path_logs_and_extracts`, který občas vracel prázdné `document_ids`.
- odstranit křehkost vůči dostupnosti ARES API během testu.

## Jak ověřeno
- statická kontrola změn v `processor.py`: při výpadku ARES se doklad dál uloží (ARES je enrich krok), místo tvrdého failu do review.
- test byl upraven na deterministické chování přes mock `fetch_by_ico`.
- lokální běh v tomto prostředí: NOT RUN (chybí runtime dependencies pro `pytest`/`sqlalchemy` bez síťové instalace).

## Co se změnilo
- `src/kajovospend/service/processor.py`: při výjimce ARES se provede best-effort `upsert_supplier` a zpracování pokračuje.
- `tests/integration/test_extraction_fixtures.py`: přidán mock `fetch_by_ico` + `AresRecord`, aby test nebyl závislý na síti.
- `docs/regen/parity/parity-map.yaml`: aktualizace stavu modulu integration tests.

## Rizika / known limits
- při trvalém výpadku ARES se dodavatel ukládá s omezenými daty (bez enrichmentu), což je preferovaný režim oproti zahození dokladu.
- plné testy musí potvrdit CI běh v prostředí s dostupnými dependencies.
