# Plan: offline VAT / net-gross (`feature/offline-vat-net-gross`)

## Rychlá mapa repo (kde jsou klíčová místa)

### 1) Datové modely (documents, items, suppliers, page audit)
- `src/kajovospend/db/models.py`
  - `Supplier` (`suppliers`): IČO/DIČ + ARES metadata.
  - `Document` (`documents`): hlavička dokladu (`total_with_vat`, `currency`, `requires_review`, `review_reasons`, text quality).
  - `LineItem` (`items`): položky (`quantity`, `unit_price`, `vat_rate`, `line_total`).
  - `DocumentPageAudit` (`document_page_audit`): audit volby embedded/OCR po stránkách.

### 2) Migrace a upgrade DB
- `src/kajovospend/db/migrate.py`
  - `init_db(engine)`: `Base.metadata.create_all(...)` + idempotentní `_ensure_columns_and_indexes(...)`.
  - `_ensure_columns_and_indexes(...)`: non-breaking `ALTER TABLE ... ADD COLUMN`, backfill `ico_norm`, tvorba indexů a FTS tabulek.
- Spouštění migrace:
  - `service_main.py` (služba) volá `init_db(engine)` při startu.
  - `src/kajovospend/ui/main_window.py` (GUI) volá `init_db(engine)` při startu/obnově DB.

### 3) Exporty a UI místa se zobrazením částek
- Export:
  - `src/kajovospend/ui/main_window.py` → `_export(...)` (CSV/XLSX), exportuje `total_with_vat`, `item_vat_rate`, `item_line_total`.
- UI částky:
  - `src/kajovospend/ui/main_window.py` → `refresh_money()` (měsíční součty, top dodavatelé).
  - `src/kajovospend/ui/main_window.py` → dashboard tiles (součty s/bez DPH).
  - `src/kajovospend/ui/db_api.py` → `run_stats()` (`sum_items_wo_vat`, `sum_items_w_vat`, `avg_receipt`, `avg_item`).
  - `src/kajovospend/ui/db_api.py` → `list_items()` vrací per-item částky a navázaný `doc_total_with_vat`.

### 4) Normalizace položek (parser / postprocess)
- `src/kajovospend/extract/parser.py`
  - `extract_from_text(...)`: offline extrakce + interní canonicalizace položek.
  - `_canonicalize_items_to_unit_net_and_line_gross(...)`: převod na kanonický formát
    - `unit_price` = cena bez DPH / jednotka
    - `line_total` = řádková cena vč. DPH
    - validace vůči `total_with_vat` + audit přes `reasons`.
  - `postprocess_items_for_db(...)`: deterministický postprocess po offline/OAI extrakci.
- `src/kajovospend/service/processor.py`
  - importuje `extract_from_text` + `postprocess_items_for_db` a používá je při zpracování dokladu před uložením.

## Plán pořadí změn (implementační kroky)

1. **Model + migrace (non-breaking):**
   - Přidat nové sloupce pro net/gross granularitu tak, aby stará data i kód fungovaly beze změny.
   - Idempotentní migrace + bezpečný backfill (deterministické výpočty, bez heuristik bez logu).

2. **Parser/postprocess canonicalizace:**
   - Zpevnit pravidla pro rozlišení net vs gross na položce.
   - Každou korekci přidat do `review_reasons` (příp. `text_debug` v processoru).

3. **Persist + servisní vrstva:**
   - Ukládat nové hodnoty konzistentně v `processor.py`/DB API.
   - Zachovat kompatibilitu existujících dotazů a exportů.

4. **UI + exporty:**
   - Rozšířit přehledy a exportní sloupce (CSV/XLSX) o nové net/gross údaje.
   - Nezměnit stávající význam existujících polí (`total_with_vat`, `line_total`) bez migračního mostu.

5. **Testy (unit):**
   - Přidat testy pro canonicalizaci položek a backfill/migrační logiku.
   - Pokrýt hraniční případy (qty=0, VAT=0, chybějící total, rounding line).

## Guardrails pro další prompty
- Non-breaking migrace + backfill (stávající DB musí jít otevřít bez ručního zásahu).
- Deterministické výpočty částek.
- Každá oprava/korekce musí mít audit stopu (`review_reasons` nebo `text_debug`).
