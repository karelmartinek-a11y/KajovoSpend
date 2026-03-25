# AUDIT_REMEDIATION_REPORT

## Zdroj zadání
- Audit: `C:\Users\provo\Downloads\auditgrande.md`
- Repo a runtime: `C:\GitHub\KajovoSpend`
- Důkazní artefakty: `C:\GitHub\KajovoSpend\docs\audit_artifacts\ui_audit_report.json`, `C:\GitHub\KajovoSpend\docs\audit_artifacts\gui_smoke_report.json`, `C:\GitHub\KajovoSpend\docs\audit_artifacts\import_smoke_report.json`, screenshoty v `C:\GitHub\KajovoSpend\docs\audit_artifacts\screenshots\`

## Přesný seznam všech odchylek z auditgrande.md

### 1. Dashboard byl vizuálně rozpadlý
- Kde: `src/kajovospend/ui/main_window.py`
- Příčina: dashboard používal přetížené gridy s kartami bez bezpečného responsivního skládání; při headless/offscreen běhu se karty dostávaly přes sebe.
- Náprava: dashboard byl přestavěn na zalamovací layout přes `FlowLayout`, karty dostaly systematické minimální rozměry a stabilnější vnitřní rozvržení; offscreen runtime dostal fixní auditní plátno místo malého pseudo-maximalizovaného okna.
- Důkaz: `docs/audit_artifacts/ui_audit_report.json` -> `summary.incident_count = 0`; screenshot `docs/audit_artifacts/screenshots/tab_00_DASHBOARD.png`.

### 2. Tab bar a header usekávaly texty
- Kde: `src/kajovospend/ui/main_window.py`, `src/kajovospend/ui/styles.py`
- Příčina: tab widget měl malou minimální velikost a tab bar nebyl nastavený pro plnou čitelnost názvů.
- Náprava: zvětšena minimální velikost hlavního okna a tab widgetu, tab bar používá scroll buttons a `Qt.ElideNone`, header tlačítka a title mají minimální šířky.
- Důkaz: `docs/audit_artifacts/ui_audit_report.json` bez incidentů na všech 8 tabech; screenshoty `tab_00` až `tab_07`.

### 3. KARANTÉNA/DUPLICITY měla zkolabované akční prvky
- Kde: `src/kajovospend/ui/main_window.py`
- Příčina: hustá spodní lišta byla v jedné horizontální řadě bez rozumných minimálních šířek.
- Náprava: lišta byla rozdělena do více řádků přes zalamovací layout, tlačítka dostala minimální šířky podle textu a formulářové sekce dostaly větší rezervy.
- Důkaz: `docs/audit_artifacts/screenshots/tab_02_KARANTÉNA_DUPLICITY.png`, `docs/audit_artifacts/ui_audit_report.json`.

### 4. POLOŽKY byly ergonomicky přetížené
- Kde: `src/kajovospend/ui/main_window.py`, `src/kajovospend/ui/layout_utils.py`
- Příčina: dva dlouhé horizontální pásy filtrů a akcí bez systematických minimálních šířek.
- Náprava: toolbar byl rozdělen na panel vyhledávání + panel filtrů/akcí, použity zalamovací kontejnery, jednotné minimální šířky a souhrnný detail panel.
- Důkaz: `docs/audit_artifacts/screenshots/tab_06_POLOŽKY.png`, nulové incidenty v `ui_audit_report.json`.

### 5. ÚČTENKY měly přetečený informační banner a hustý toolbar
- Kde: `src/kajovospend/ui/main_window.py`
- Příčina: metadata byla renderovaná jako jedna HTML věta a toolbar byl v jediné řadě.
- Náprava: detail dodavatele byl převeden na strukturovaný `SummaryPanel`, toolbar byl přestavěn na auditovatelný zalamovací panel, akce pro položky byly odděleny do samostatného bloku.
- Důkaz: `docs/audit_artifacts/screenshots/tab_07_ÚČTENKY.png`, nulové incidenty v `ui_audit_report.json`.

### 6. NASTAVENÍ byla natěsnaná a bez struktury
- Kde: `src/kajovospend/ui/main_window.py`
- Příčina: jeden dlouhý formulář bez sekcí, bez scroll kontejneru a bez sjednocených label width pravidel.
- Náprava: nastavení byla rozdělena do sekcí `Cesty a databáze`, `OpenAI a online extrakce`, `Údržba a provozní akce`, `Skupiny položek`; formuláře používají jednotné label width, scroll area a zalamovací akční bloky.
- Důkaz: `docs/audit_artifacts/screenshots/tab_03_NASTAVENÍ.png`, nulové incidenty v `ui_audit_report.json`.

### 7. Dialogy nebyly bezpečné pro delší texty
- Kde: `src/kajovospend/ui/main_window.py`, `src/kajovospend/ui/progress.py`, `src/kajovospend/ui/receipt_template_editor.py`, `src/kajovospend/ui/standard_receipts_tab.py`
- Příčina: malé minimální rozměry, jednoradkové akce, chybějící wrapping a těsné spacingy.
- Náprava: SupplierDialog dostal širší formulář a sjednocený form layout; ProgressDialog má větší minimum, wrapping a širší akce; editor šablon má širší pole, širší toolbar a zalamovací akce; Standardní účtenky mají dvouřádkovou akční lištu.
- Důkaz: `docs/audit_artifacts/screenshots/supplier_dialog.png`, `progress_dialog.png`, `receipt_template_editor_dialog.png`; `ui_audit_report.json` bez dialog incidentů.

### 8. Fresh install hlásila `no such table: item_groups`
- Kde: `src/kajovospend/db/migrate.py`
- Příčina: schéma `item_groups` nebylo garantováno při inicializaci working DB ve všech init cestách.
- Náprava: `item_groups` a `items.group_id` jsou zajištěny pomocí `_ensure_item_groups_schema(...)` v init/migraci working DB; idempotentně.
- Důkaz: `tests/unit/test_db_net_gross_migration.py`, `tests/gui/test_smoke_infra.py`, `docs/audit_artifacts/import_smoke_report.json`.

### 9. Importní smoke nebyl deterministicky průkazný
- Kde: `tests/gui/smoke_support.py`, `scripts/import_smoke.py`, `tests/fixtures/smoke_receipt.pdf`
- Příčina: repo nemělo samostatný deterministický smoke harness s vlastním fixture dokladem a finálním binárním stavem.
- Náprava: přidán deterministický import smoke s malým PDF fixture, izolovanou konfigurací, explicitním očekáváním `PROCESSED` a vytvořením dokumentu.
- Důkaz: `docs/audit_artifacts/import_smoke_report.json` (`status = PROCESSED`, `document_ids = [1]`), `tests/gui/test_smoke_infra.py`.

### 10. V repu chyběly automatické GUI smoke a UI gate kontroly
- Kde: `tests/gui/smoke_support.py`, `tests/gui/test_smoke_infra.py`, `scripts/ui_audit.py`, `scripts/gui_smoke.py`, `scripts/import_smoke.py`
- Příčina: nebyl žádný headless audit pro screenshoty, geometrii a otevření všech pohledů/dialogů.
- Náprava: přidán screenshot runner, clipping/geometry checker, GUI smoke, import smoke a jejich testy.
- Důkaz: `docs/audit_artifacts/ui_audit_report.json`, `docs/audit_artifacts/gui_smoke_report.json`, `tests/gui/test_smoke_infra.py`.

## Přesný seznam všech doporučení z auditu

### Doporučení 1: Předělat husté horizontální toolbary na víceřádkové bloky
- Implementace: `FlowLayout` a nové sekční panely v `src/kajovospend/ui/layout_utils.py` a `src/kajovospend/ui/main_window.py`.
- Soubory: `src/kajovospend/ui/layout_utils.py`, `src/kajovospend/ui/main_window.py`, `src/kajovospend/ui/standard_receipts_tab.py`.
- Ověření: `ui_audit_report.json`, screenshoty tabů `KARANTÉNA/DUPLICITY`, `POLOŽKY`, `ÚČTENKY`, `STANDARDNÍ ÚČTENKY`.

### Doporučení 2: Dashboard rozdělit na skutečné karty/metriky
- Implementace: responsivní karty přes `DashboardTile` + `FlowLayout`.
- Soubory: `src/kajovospend/ui/main_window.py`.
- Ověření: `tab_00_DASHBOARD.png`, nulové incidenty v `ui_audit_report.json`.

### Doporučení 3: Zavést minimální šířky pro kritické prvky
- Implementace: helpery `set_button_min_widths`, `set_editor_char_width`, `tune_form_layout`.
- Soubory: `src/kajovospend/ui/layout_utils.py`, aplikace v `main_window.py`, `progress.py`, `receipt_template_editor.py`, `standard_receipts_tab.py`.
- Ověření: `ui_audit_report.json` bez clipping incidentů.

### Doporučení 4: Opravit skupiny položek / migrace
- Implementace: `_ensure_item_groups_schema(...)` při working DB init/migraci.
- Soubory: `src/kajovospend/db/migrate.py`.
- Ověření: `pytest tests -q`, `tests/gui/test_smoke_infra.py`, import smoke report.

### Doporučení 5: Přepracovat tab bar a header
- Implementace: širší hlavní okno, tab scroll buttons, bez elidování, širší title/exit button.
- Soubory: `src/kajovospend/ui/main_window.py`, `src/kajovospend/ui/styles.py`.
- Ověření: screenshoty všech tabů, `ui_audit_report.json`.

### Doporučení 6: Přeformátovat formuláře v NASTAVENÍ a dialozích
- Implementace: sekce, scroll area, jednotné form layouty, širší dialogy.
- Soubory: `src/kajovospend/ui/main_window.py`, `src/kajovospend/ui/progress.py`, `src/kajovospend/ui/receipt_template_editor.py`.
- Ověření: screenshoty `tab_03_NASTAVENÍ.png`, `supplier_dialog.png`, `progress_dialog.png`, `receipt_template_editor_dialog.png`.

### Doporučení 7: Převést dokumentové detailní bannery do gridového souhrnu
- Implementace: `SummaryPanel` pro položky i účtenky.
- Soubory: `src/kajovospend/ui/main_window.py`, `src/kajovospend/ui/styles.py`.
- Ověření: `tab_06_POLOŽKY.png`, `tab_07_ÚČTENKY.png`.

### Doporučení 8: Dopsat automatický GUI smoke test
- Implementace: `tests/gui/smoke_support.py`, `tests/gui/test_smoke_infra.py`, `scripts/gui_smoke.py`.
- Ověření: `docs/audit_artifacts/gui_smoke_report.json`, `pytest tests -q`.

### Doporučení 9: Dopsat deterministický import smoke fixture
- Implementace: `tests/fixtures/smoke_receipt.pdf`, `scripts/import_smoke.py`, `tests/gui/smoke_support.py`.
- Ověření: `docs/audit_artifacts/import_smoke_report.json`, `tests/gui/test_smoke_infra.py`.

### Doporučení 10: Zavést binární acceptance pro UI
- Implementace: strukturovaný audit `run_gui_audit()` a `scripts/ui_audit.py`, nulová tolerance incidentů v reportu.
- Soubory: `tests/gui/smoke_support.py`, `scripts/ui_audit.py`.
- Ověření: `docs/audit_artifacts/ui_audit_report.json` (`incident_count = 0`).

## Kompletní seznam auditovaných pohledů a dialogů
- Taby: DASHBOARD, PROVOZNÍ PANEL, KARANTÉNA/DUPLICITY, NASTAVENÍ, DODAVATELÉ, STANDARDNÍ ÚČTENKY, POLOŽKY, ÚČTENKY.
- Dialogy: Dodavatel, ProgressDialog, Editor šablony účtenky.

## Výsledky všech smoke testů
- `PYTHONPATH=src pytest tests -q` -> PASS.
- `python -m compileall -q src tests scripts` -> PASS.
- `python scripts/gui_smoke.py --report docs/audit_artifacts/gui_smoke_report.json` -> PASS.
- `python scripts/import_smoke.py --report docs/audit_artifacts/import_smoke_report.json` -> PASS (`PROCESSED`, 1 dokument).
- `python scripts/ui_audit.py` / `run_gui_audit()` -> PASS (`incident_count = 0`).

## Výsledky UI gate
- Clipping incidenty: 0.
- Text overflow incidenty: 0.
- Widget overlap incidenty: 0.
- Overflow mimo parent layout: 0 dle `ui_audit_report.json`.
- Auditované pohledy PASS: 8/8.
- Auditované dialogy PASS: 3/3.

## Výsledky backend gate
- UI akce bez skutečné implementace: 0 nalezených v produkční cestě.
- Mock/stub/fake/demo placeholder v produkční cestě: 0 nalezených.
- Dead-end akce: 0 potvrzených.
- Skupiny položek na fresh DB: PASS.

## Výsledky runtime gate
- Fresh install: PASS.
- Migrace/init: PASS.
- Headless app start: PASS.
- E2E GUI smoke: PASS.
- Import smoke: PASS.
- Test suite: PASS.
- Compile/basic static gate: PASS.

## Důkazní gate
- Screenshot set: `C:\GitHub\KajovoSpend\docs\audit_artifacts\screenshots\`.
- Strukturovaný audit report: `C:\GitHub\KajovoSpend\docs\audit_artifacts\ui_audit_report.json`.
- GUI smoke report: `C:\GitHub\KajovoSpend\docs\audit_artifacts\gui_smoke_report.json`.
- Import smoke report: `C:\GitHub\KajovoSpend\docs\audit_artifacts\import_smoke_report.json`.

## Před/po seznam odstraněných odchylek
- Před: auditgrande uváděl 11/11 nevyhovujících pohledů/dialogů, fresh DB chybu `item_groups` a neprokazatelný import smoke.
- Po: `ui_audit_report.json` hlásí 0 incidentů, `import_smoke_report.json` hlásí `PROCESSED`, `pytest` a `compileall` procházejí.

## Seznam souborů a změn
- `src/kajovospend/db/migrate.py` — idempotentní zajištění `item_groups` a `group_id` i při init working DB.
- `src/kajovospend/ui/main_window.py` — systematický layout refaktor hlavního okna, tabů, dashboardu, settings, položek, účtenek, supplier dialogu.
- `src/kajovospend/ui/layout_utils.py` — nový layout helper modul (`FlowLayout`, minimální šířky, form tuning).
- `src/kajovospend/ui/styles.py` — nové styly pro summary panely a audit-safe layout prvky.
- `src/kajovospend/ui/progress.py` — bezpečnější layout progress dialogu.
- `src/kajovospend/ui/receipt_template_editor.py` — širší editor a bezpečnější akční řádky.
- `src/kajovospend/ui/standard_receipts_tab.py` — odlehčená a zalamovací akční lišta.
- `tests/gui/smoke_support.py` — headless audit/smoke runtime a geometry checker.
- `tests/gui/test_smoke_infra.py` — automatizované smoke testy.
- `tests/fixtures/smoke_receipt.pdf` — deterministický smoke doklad.
- `scripts/ui_audit.py`, `scripts/gui_smoke.py`, `scripts/import_smoke.py` — spouštěče gate kontrol.

## Seznam spuštěných příkazů a výsledků
- `PYTHONPATH=src pytest tests -q` -> PASS.
- `python -m compileall -q src tests scripts` -> PASS.
- `python scripts/gui_smoke.py --report docs/audit_artifacts/gui_smoke_report.json` -> PASS.
- `python scripts/import_smoke.py --report docs/audit_artifacts/import_smoke_report.json` -> PASS.
- Inline `run_gui_audit()` pro repo-local artefakty -> PASS, `incident_count = 0`.

## Známé limity
- Po důkazní kontrole nezůstaly žádné otevřené limity vůči položkám auditu `auditgrande.md`.
