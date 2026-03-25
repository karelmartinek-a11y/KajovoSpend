# AUDIT_REMEDIATION_HEADERS

## Scope

Tento zásah řeší pouze dvě zbytkové vady z posledního auditu:

- useknuté hlavičky tabulky v pohledu `POLOŽKY`
- useknuté hlavičky tabulky v pohledu `ÚČTENKY`

Současně doplňuje automatický GUI audit a regresní test tak, aby se stejná kategorie vady nemohla tiše vrátit.

## Reprodukce vad

Reprodukce proběhla ve skutečném headless runtime aplikace přes screenshot runner a GUI audit.

Před opravou byly uloženy screenshoty:

- `docs/audit_artifacts/header_fix/before_POLOZKY.png`
- `docs/audit_artifacts/header_fix/before_UCTENKY.png`

Zjištění při reprodukci:

- v `POLOŽKY` byly hlavičky hlavní tabulky zkrácené, protože šířky sekcí neodpovídaly délce českých názvů sloupců
- v `ÚČTENKY` byly hlavičky hlavní tabulky zkrácené stejným mechanismem
- původní audit tuto kategorii minul, protože nekontroloval `QHeaderView` a neporovnával potřebnou šířku header textu proti reálné šířce sekce

Reprodukční běhy před opravou:

- `python scripts/ui_audit.py --report docs/audit_artifacts/header_repro_ui_audit.json` -> PASS, ale audit zůstal slepý (`incident_count = 0`)
- `python scripts/gui_smoke.py --report docs/audit_artifacts/header_repro_gui_smoke.json` -> PASS

## Kořenová příčina pro POLOŽKY

Kořenová příčina byla v `src/kajovospend/ui/main_window.py`:

- `ItemsTable` používala `QHeaderView.Interactive`, ale po nasazení modelu dostala pouze obecné implicitní šířky sekcí
- pro dlouhé české názvy jako `ID účtenky (KajovoSpend)`, `Název dodavatele` nebo `Cena bez DPH za kus` neexistovala žádná systematická sizing politika headerů
- detailní tabulka položek v pravém panelu (`items_doc_items_table`) měla stejnou slabinu v empty-state i po načtení detailu

Důsledek: hlavičky byly useknuté už v samotné šířce sekce, nikoli jen mimo viewport.

## Kořenová příčina pro ÚČTENKY

Kořenová příčina byla v `src/kajovospend/ui/main_window.py`:

- `DocsTable` neměla po nasazení modelu robustní sizing policy pro hlavičky
- tabulka se opírala o výchozí nebo průběžně přepočítané šířky, které neodpovídaly délce českých header textů
- při reálném renderu hlavního pohledu pak některé sekce nedostaly minimální šířku potřebnou pro plnou čitelnost hlavičky

Důsledek: hlavičky typu `Číslo účtenky`, `Celkem vč. DPH` a `IČO dodavatele` nebyly plně viditelné.

## Implementovaná oprava

Změněné soubory:

- `src/kajovospend/ui/main_window.py`
- `tests/gui/smoke_support.py`
- `tests/gui/test_smoke_infra.py`

Implementace v UI:

- do `src/kajovospend/ui/main_window.py` byla přidána společná sizing politika `_apply_header_width_policy()`
- `POLOŽKY` nově používají `_configure_items_table_headers()` s minimální šířkou sekcí, výpočtem šířky dle textu hlavičky a explicitními šířkami pro dlouhé sloupce
- `ÚČTENKY` nově používají `_configure_docs_table_headers()` se stejným principem
- detailní tabulka položek v pravém panelu nově používá `_configure_line_items_header()`, aby stejná vada nevznikala v přidruženém detailu
- obě hlavní tabulky mají explicitně `ScrollPerPixel`, takže při omezené šířce nevzniká agresivní vizuální skok a layout zůstává čitelný

## Změny v automatickém auditu

V `tests/gui/smoke_support.py` byla doplněna nová kontrola `_audit_table_headers(table)`.

Checker nově umí zachytit:

- `header_overflow` — text hlavičky potřebuje víc místa, než má samotná sekce
- `header_viewport_overflow` — když neexistuje horizontální scroll a viditelný viewport hlavičky je menší než potřebný text
- `header_not_visible` — když bez horizontálního scrollu hlavička nemá žádný viditelný viewport

Regresní ochrana v `tests/gui/test_smoke_infra.py`:

- `test_header_audit_detects_too_narrow_sections` vytváří syntetickou tabulku s úmyslně úzkou hlavičkou a ověřuje, že audit vrátí `header_overflow`
- `test_gui_audit_reports_no_header_overflow_for_main_window` spouští skutečný GUI audit hlavního okna a ověřuje, že po opravě na aktuálním runtime nezůstávají incidenty `header_overflow`, `header_viewport_overflow` ani `header_not_visible`

Tím je doloženo obojí:

- starý typ vady audit nově umí zachytit
- opravený runtime už na stejné kategorii nepadá

## Spuštěné příkazy a výsledky

- `python -m py_compile src/kajovospend/ui/main_window.py tests/gui/smoke_support.py tests/gui/test_smoke_infra.py` -> PASS
- `PYTHONPATH=src pytest tests/gui/test_smoke_infra.py -q` -> PASS (`5 passed`)
- `PYTHONPATH=src pytest tests -q` -> PASS
- `python scripts/gui_smoke.py --report docs/audit_artifacts/header_fix_gui_smoke.json` -> PASS
- `python scripts/ui_audit.py --report docs/audit_artifacts/header_fix_ui_audit.json` -> PASS (`incident_count = 0`)

## Před/po důkazy

Před:

- `docs/audit_artifacts/header_fix/before_POLOZKY.png`
- `docs/audit_artifacts/header_fix/before_UCTENKY.png`
- `docs/audit_artifacts/header_repro_ui_audit.json`
- `docs/audit_artifacts/header_repro_gui_smoke.json`

Po:

- `docs/audit_artifacts/header_fix/after_POLOZKY.png`
- `docs/audit_artifacts/header_fix/after_UCTENKY.png`
- `docs/audit_artifacts/header_fix_ui_audit.json`
- `docs/audit_artifacts/header_fix_gui_smoke.json`

Ověřený výsledek po opravě:

- `POLOŽKY` PASS — hlavičky hlavní tabulky jsou plně viditelné
- `ÚČTENKY` PASS — hlavičky hlavní tabulky jsou plně viditelné
- GUI audit PASS — nová kontrola headerů běží a na aktuálním runtime hlásí `0` incidentů
- GUI smoke PASS — render všech tabů a dialogů proběhl bez incidentů

## Verdict

Binární kritéria tohoto zadání jsou splněna.

- obě zbytkové vady jsou odstraněny ve skutečném renderu
- audit nově umí tuto kategorii vady detekovat
- existuje regresní test pro selhávající stav i pro aktuální opravený runtime
- důkaz je uložen ve screenschotech a JSON reportech v `docs/audit_artifacts/`
