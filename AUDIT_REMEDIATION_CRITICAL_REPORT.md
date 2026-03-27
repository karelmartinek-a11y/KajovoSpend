# Audit Remediation Critical Report

## Scope

Tento report shrnuje odstranění nálezů z posledního kritického auditu nad aktuálním stavem repozitáře `KajovoSpend`.
Jediným referenčním stavem byl:

- aktuální obsah repozitáře,
- skutečný runtime aplikace,
- skutečné výsledky clean install, testů a smoke běhů,
- poslední kritický audit.

## Seznam nálezů z kritického auditu

### P0. Clean install neprochází na deklarovaném Pythonu 3.13

- Kořenová příčina:
  repo deklarovalo podporu Pythonu 3.13, ale `requirements.txt` držel piny `numpy==2.0.1` a `pandas==2.2.2`, pro které už na Pythonu 3.13 nebyly dostupné wheel balíčky.
- Soubory změněné k opravě:
  - `requirements.txt`
  - `pyproject.toml`
  - `docs/README.md`
  - `scripts/bootstrap_codex_env.sh`
  - `src/kajovospend.egg-info/PKG-INFO`
  - `src/kajovospend.egg-info/SOURCES.txt`
- Jak byla odchylka odstraněna:
  - `numpy` byl posunut na `2.1.3`,
  - `pandas` byl posunut na `2.2.3`,
  - `requires-python` byl zúžen na pravdivý rozsah `>=3.11,<3.14`,
  - bootstrap už nepreferuje zastaralý `.venv312`, ale bere nejvyšší dostupný stabilní Python `>=3.11`.
- Důkaz:
  fresh install na Pythonu `3.13.11` doběhl úspěšně přes:
  - `py -3.13 -m venv <temp>`
  - `python -m pip install -U pip setuptools wheel`
  - `python -m pip install -r requirements.txt`

### P0. Business taby po importu ztrácely vazbu na skutečný zdrojový soubor

- Kořenová příčina:
  promotion do production DB ukládala `file_id=None`, takže business UI nemělo kanonickou vazbu zpět na working file; navíc detail v `MainWindow` četl z production/working DB nekonzistentně.
- Soubory změněné k opravě:
  - `src/kajovospend/db/production_queries.py`
  - `src/kajovospend/service/promotion.py`
  - `src/kajovospend/ui/main_window.py`
  - `tests/unit/test_dual_db_promotion.py`
  - `tests/integration/test_dual_db_dashboard_reads.py`
- Jak byla odchylka odstraněna:
  - production dokument teď nese rekonstruovatelnou vazbu `file_id` na working file,
  - opakovaná promotion umí doplnit chybějící `file_id` i do starého rozbitého záznamu,
  - detail dokladu i položek čte v UI přes production DB + `working_session`,
  - `Zdroj:` line, tlačítka otevření i preview jsou řízené jen podle reálně dostupné cesty.
- Důkaz:
  ve finálním UI auditu jsou populated evidence pro `ÚČTENKY` i `POLOŽKY` konzistentní:
  - `backend_file_id == 1`
  - `backend_path == ui_source_text`
  - `action_enabled == true`
  - `preview_has_pixmap == true`
  - `truth_issue_count == 0`
  viz `docs/audit_artifacts/final_ui_audit_report.json`.

### P1. Auditní a smoke infrastruktura neuměla zachytit backendově prázdné UI

- Kořenová příčina:
  původní smoke vrstva kontrolovala hlavně geometrii a render, ale nepravdivost business UI po importu.
- Soubory změněné k opravě:
  - `tests/gui/smoke_support.py`
  - `tests/gui/test_smoke_infra.py`
  - `scripts/gui_smoke.py`
  - `scripts/ui_audit.py`
- Jak byla odchylka odstraněna:
  - přibyl populated-state audit nad skutečně naimportovaným dokladem,
  - přibyly truth guards pro `ÚČTENKY` i `POLOŽKY`,
  - přibyl negativní test, který simuluje starý vadný stav a musí failnout na logické úrovni,
  - GUI smoke i UI audit teď reportují `truth_issue_count`.
- Důkaz:
  - `python -m pytest tests/gui/test_smoke_infra.py -vv`
  - výsledek: `7 passed`
  - finální UI audit: `incident_count == 0`, `truth_issue_count == 0`

### P1. Import smoke nebyl dostatečný provozní důkaz

- Kořenová příčina:
  původní smoke pokrýval jen jediný embedded PDF případ a neověřoval širší branch coverage importu.
- Soubory změněné k opravě:
  - `tests/gui/smoke_support.py`
  - `tests/gui/test_smoke_infra.py`
  - `scripts/import_smoke.py`
  - `scripts/gui_smoke.py`
- Jak byla odchylka odstraněna:
  import smoke teď deterministicky kryje šest scénářů:
  - `embedded_success`
  - `ocr_path`
  - `template_path`
  - `quarantine_case`
  - `duplicate_case`
  - `ares_failure_case`
- Důkaz:
  `docs/audit_artifacts/final_import_smoke_report.json`
  obsahuje:
  - `status: PASS`
  - `case_count: 6`
  - `embedded_success -> PROCESSED`
  - `ocr_path -> QUARANTINE`, `text_method: image_ocr`
  - `template_path -> QUARANTINE`, `text_method: template`
  - `quarantine_case -> QUARANTINE`
  - `duplicate_case -> DUPLICATE`
  - `ares_failure_case -> QUARANTINE`

Poznámka k pravdivosti:
OCR a template case jsou v tomto deterministickém fixture kryté jako skutečně aktivované branch (`image_ocr`, `template`) a smoke je považuje za PASS jen tehdy, když branch proběhne bez pádu a skončí očekávaným, explicitně kontrolovaným stavem. Nejde o falešný happy-path claim.

### P1. Lokalizační a UX nekonzistence

- Kořenová příčina:
  část textů zůstávala anglicky nebo se spoléhala na implicitní Qt texty.
- Soubory změněné k opravě:
  - `src/kajovospend/ui/main_window.py`
  - `src/kajovospend/ui/receipt_template_editor.py`
- Jak byla odchylka odstraněna:
  - `EXIT` -> `KONEC`
  - `Fit` -> `Přizpůsobit`
  - `OK/Cancel` a `Save/Cancel` jsou teď explicitně lokalizované na `OK`, `Zrušit`, `Uložit`
- Důkaz:
  screenshoty dialogů a hlavních tabů ve `docs/audit_artifacts/final_screenshots/` a nulové incidenty ve finálním UI auditu.

### P2. Pohled POLOŽKY byl příliš hustý a křehký

- Kořenová příčina:
  filtry a akce byly stlačené do příliš malého počtu řádků a inner tabulky neměly dostatečně explicitní width policy.
- Soubory změněné k opravě:
  - `src/kajovospend/ui/main_window.py`
- Jak byla odchylka odstraněna:
  - search panel a filtry v `POLOŽKY` byly rozdělené do více řádků,
  - zvýšily se minimální šířky klíčových filtrů,
  - přibyly explicitní width policy i pro editable item tabulky v `ÚČTENKY` a `KARANTÉNA/DUPLICITY`.
- Důkaz:
  finální UI audit vrací `incident_count == 0` a screenshot `docs/audit_artifacts/final_screenshots/tab_06_POLOŽKY.png` už neobsahuje přetékající hlavičky ani husté kolize.

## Clean install truth

- Deklarovaná podpora je nyní pravdivě `Python 3.11 až 3.13`.
- `pyproject.toml` nově říká `>=3.11,<3.14`.
- Clean install na `Python 3.13.11` byl skutečně spuštěn a prošel.

## Dual-DB/source-link truth

- Kanonický kontrakt je nyní:
  production dokument nese `file_id` na working file.
- Business UI dohledává zdroj přes production detail + working file lookup.
- Když cesta existuje:
  - `Zdroj:` ukazuje skutečnou cestu,
  - akce otevření jsou aktivní,
  - preview ukazuje skutečný dokument.
- Když cesta neexistuje:
  - UI ukáže pravdivou nedostupnost,
  - akce jsou disabled,
  - preview nevytváří iluzi dokumentu.

## Rozšíření auditů a smoke bran

- `gui_smoke.py` reportuje:
  - počet truth issues,
  - počet import case.
- `ui_audit.py` reportuje:
  - geometrii,
  - truth issues v populated stavu.
- `tests/gui/test_smoke_infra.py` nově kryje:
  - více import scénářů,
  - populated-state audit,
  - negativní důkaz proti starému stavu.

## Lokalizace a UX úpravy

- hlavní helper akce a dialogové button boxy jsou sjednocené do češtiny,
- `POLOŽKY` mají větší ergonomickou rezervu,
- inner tabulky v `ÚČTENKY` a `KARANTÉNA/DUPLICITY` mají explicitní header policy, takže audit už nehlásí přetečení.

## Spuštěné příkazy a výsledky

### Install a sanity

1. `py -3.13 -m venv <temp>`
2. `python -m pip install -U pip setuptools wheel`
3. `python -m pip install -r requirements.txt`
4. `python -m compileall -q src tests scripts`

Výsledek:
- clean install PASS na Pythonu 3.13.11
- compile sanity PASS

### Testy

1. `python -m pytest tests/unit/test_dual_db_promotion.py tests/integration/test_dual_db_dashboard_reads.py`
   - `6 passed`
2. `python -m pytest tests/gui/test_smoke_infra.py -vv`
   - `7 passed`
3. `python -m pytest tests/unit/test_receipt_template_editor_dialog.py`
   - `5 passed`
4. `python -m pytest tests`
   - `101 passed`

### Runtime evidence

1. `python scripts/gui_smoke.py --workspace-name final-gui-smoke --report docs/audit_artifacts/final_gui_smoke_report.json`
   - `truth issues: 0`
   - `import cases: 6`
2. `python scripts/ui_audit.py --workspace-name final-ui-audit --report docs/audit_artifacts/final_ui_audit_report.json`
   - `incidenty: 0`
   - `truth issues: 0`
3. `python scripts/import_smoke.py --workspace-name final-import-smoke --report docs/audit_artifacts/final_import_smoke_report.json`
   - `status: PASS`
   - `case_count: 6`

## Před/po důkazy

### Před

- clean install padal na 3.13 kvůli pinům bez wheelů,
- production dokumenty po promotion neměly `file_id`,
- business UI po importu umělo ukázat dokument, ale bez funkční vazby na zdroj,
- audit uměl projít i backendově prázdným UI.

### Po

- clean install na 3.13 prochází,
- production dokumenty drží zdrojovou vazbu přes `file_id`,
- populated-state audit v `ÚČTENKY` i `POLOŽKY` vrací nulové truth issues,
- finální screenshoty jsou archivované v `docs/audit_artifacts/final_screenshots/`,
- finální reporty jsou v:
  - `docs/audit_artifacts/final_gui_smoke_report.json`
  - `docs/audit_artifacts/final_ui_audit_report.json`
  - `docs/audit_artifacts/final_import_smoke_report.json`

## Závěrečný verdict

Repo po provedených změnách neobsahuje nálezy popsané v posledním kritickém auditu ve stejné třídě:

- install story je pravdivá a doložená reálným během,
- dual-DB/source-link kontrakt je pravdivý a otestovaný,
- business UI po importu nelže,
- auditní a smoke infrastruktura tuto třídu vady nově umí chytit,
- UI je jazykově konzistentnější a `POLOŽKY` mají větší rezervu.

## Definice úspěchu

Za splněnou považuji tuto remediaci proto, že:

- clean install PASS na oficiálně deklarovaném Pythonu 3.13 byl skutečně proveden,
- `pytest tests` PASS,
- `compileall` PASS,
- `gui_smoke` PASS,
- `ui_audit` PASS,
- populated-state truthfulness PASS,
- rozšířený import smoke PASS,
- screenshoty a reporty jsou archivované v repozitáři.
