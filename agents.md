# agents.md — KajovoSpend (Codex CLI)

Repo root (lokálně): `c:\github\kajovospend`

Tento soubor je určen pro Codex CLI agenta. Popisuje stabilní entrypointy, mapu klíčových souborů a praktická pravidla, aby změny byly konzistentní s tím, jak KajovoSpend funguje (import, OCR, DB, forenzní logování).

## 0) Co je „kanonický“ runtime

Primární běh je desktop GUI. Zpracování dokladů se spouští z GUI na kartě RUN tlačítkem „IMPORTUJ“ (není potřeba samostatná služba) [docs/README.md] fileciteturn41file1L1-L13.

Repo zároveň obsahuje volitelnou background service (watcher/fronta) přes `service_main.py` (viz níže v mapě) fileciteturn42file0L14-L21.

## 1) Nejčastější příkazy

### Windows (lokálně)
- Setup:
  - `python -m venv .venv`
  - `.\.venv\Scriptsctivate`
  - `pip install -r requirements.txt` fileciteturn41file4L3-L7
- Konfigurace:
  - `copy config.example.yaml config.yaml`
  - upravit `paths.input_dir` a `paths.output_dir` fileciteturn41file2L4-L12
- Spuštění GUI:
  - `py -m app_gui` (doporučeno) fileciteturn41file2L45-L55
  - alternativně `py run_gui.py` fileciteturn41file2L52-L55
- Testy:
  - `PYTHONPATH=src pytest tests` fileciteturn41file2L72-L76
  - `python -m compileall -q src tests` (sanity check; používá se i v auditech) fileciteturn41file10L9-L12

### Codex / CI shell (typicky Linux)
- Preferovaný bootstrap kvůli Python verzi:
  - `bash scripts/bootstrap_codex_env.sh`
  - `source .venv312/bin/activate` fileciteturn41file1L37-L46
- Pozn.: bootstrap vyžaduje `pyenv` fileciteturn37file8L8-L11.

## 2) Zásady pro změny (neporušovat)

1) Migrace DB musí být deterministické a idempotentní.
- `init_db(engine)` se může spouštět opakovaně; test explicitně ověřuje 2× běh a existenci FTS/indexů fileciteturn43file1L17-L37.
- Styl migrací: `CREATE ... IF NOT EXISTS`, `PRAGMA table_info`, podmíněné `ALTER TABLE ... ADD COLUMN`, `CREATE INDEX IF NOT EXISTS` (viz `migrate.py`) fileciteturn33file12L55-L61.

2) Bezpečnost:
- žádné `yaml.load` bez safe loaderu, žádné `eval/exec`, HTTP volání musí mít timeout, logy nesmí obsahovat citlivé tokeny fileciteturn35file0L18-L23.
- práce se soubory: používej `safe_move` (sanitizace basename + kontrola, že cíl je uvnitř složky) fileciteturn37file6L8-L17. Regresní testy to hlídají fileciteturn38file0L8-L29.

3) Import/extrakce musí být „best-effort“:
- Nové větve zpracování nesmí shodit celý import. Chyby se mají promítnout do karantény/review, ale GUI/service musí pokračovat.

4) Minimalizuj nové závislosti:
- pokud to jde, drž se stdlib a stávajících knihoven; repo má explicitně pinované závislosti a požaduje Python 3.11–3.13 fileciteturn41file1L16-L20.

## 3) Mapa souborů (orientace „kde co je“)

### Entrypointy (root)
- `run_gui.py` — hlavní GUI entrypoint: nastaví `sys.path` na `src`, instaluje global excepthook, nastaví ikonu a otevře `MainWindow` fileciteturn36file0L13-L18 fileciteturn36file0L52-L62.
- `app_gui/__main__.py` — umožní `python -m app_gui`, jen deleguje na `run_gui.main()` fileciteturn41file0L1-L7.
- `service_main.py` — volitelný běh background service: načte config, vytvoří paths/logging/DB, spustí `ServiceApp` a control server, nebo provede `sync-ares` fileciteturn42file0L24-L64.

### Dokumentace (docs/)
- `docs/README.md` — setup, běh, cesty, logy, build, testy fileciteturn41file2L20-L83.
- `docs/ARCHITECTURE.md` — stručná architektura a modulové rozdělení fileciteturn35file1L3-L10.
- `docs/SECURITY.md` — threat model a bezpečnostní pravidla fileciteturn35file0L3-L23.
- `docs/FORENSIC_AUDIT_REPORT.md` — mapování repo + auditní závěry, včetně poznámek k migracím, OpenAI retry a testům fileciteturn41file8L19-L35.
- `docs/RESET_2026-02-27.md` — baseline po resetu větví, doporučení k vývoji fileciteturn35file3L5-L13.

### UI (src/kajovospend/ui/)
- `main_window.py` — hlavní GUI (taby, import flow, náhled PDF). Obsahuje `_SilentRunner` pro background operace bez zamrznutí UI fileciteturn33file13L70-L79.
- `db_api.py` — DB dotazy pro UI: počty, seznamy, RUN statistiky. RUN statistiky jsou počítané jen z *plně zpracovaných* dokladů (files.status == PROCESSED) fileciteturn35file9L30-L45 a obsahují podíly podle `Document.extraction_method` fileciteturn35file9L85-L93.
- (další UI moduly existují, ale hlavní integrace je přes `main_window.py` + `db_api.py`; nové větší taby dávej do samostatných souborů, ne přifukovat `main_window.py`.)

### Service/orchestrace (src/kajovospend/service/)
- `processor.py` — jádro zpracování souboru: OCR/PDF, extrakce, deduplikace, zápis do DB. Inicializuje OCR engine primárně RapidOCR a fallback pytesseract; obě větve jsou best-effort (nesmí to shodit app) fileciteturn33file5L59-L86.
- `app.py` — `ServiceApp`: watcher + fronta `ImportJob` + worker pool (aktuálně vynuceně 1 worker, sekvenční zpracování) fileciteturn42file2L33-L38.
- `watcher.py` — `DirectoryWatcher` nad watchdog; na Windows+Py3.13 preferuje polling kvůli konfliktu watchdog emitteru fileciteturn42file1L40-L47.
- `file_ops.py` — `safe_move` (sanitizace názvu + path traversal guard + fallback copy/unlink při locku) fileciteturn37file6L8-L46.

### DB (src/kajovospend/db/)
- `session.py` — DB engine factory; SQLite pragmy pro WAL, busy_timeout, cache atd. fileciteturn39file2L7-L29.
- `migrate.py` — idempotentní migrace + vytvoření FTS5 tabulek (`documents_fts`, `items_fts`, `items_fts2`) fileciteturn33file12L11-L26.
- `models.py` — ORM modely (Supplier, DocumentFile, Document, LineItem, ImportJob, ServiceState…). Např. `Document.processing_profile`, page range a `extraction_method` default “offline” fileciteturn35file10L21-L29.
- `queries.py` — zápis dokumentů/položek, upsert dodavatele, rebuild FTS, update service_state. U položek provádí deterministický backfill legacy sloupců fileciteturn35file13L33-L41.
- `processing_models.py` + `processing_session.py` — „processing DB“ (ingest_files) pro stabilní ID a auditní stav fronty; separátní SQLite s best-effort dispose proti Windows lockům fileciteturn39file0L28-L51 fileciteturn39file1L12-L26.

### Extrakce (src/kajovospend/extract/)
- `parser.py` — dataclass `Extracted` + heuristiky pro částky/IČO/položky, včetně korekcí a DPH logiky fileciteturn35file7L19-L35.
- (další moduly: např. structured PDF; při změnách extrakce preferuj izolované unit testy.)

### OCR (src/kajovospend/ocr/)
- `pdf_render.py` — render PDF do PIL přes PDFium (`pypdfium2`), podporuje start_page/max_pages fileciteturn37file0L11-L27.
- `rapidocr_engine.py` — `RapidOcrEngine` je volitelný; pokud runtime není dostupný, nesmí shodit aplikaci fileciteturn37file1L32-L39. Obsahuje robustní preprocessing varianty a rekonstrukci pořadí textu.
- (fallback OCR: pytesseract; inicializace v `processor.py`.)

### Integrace (src/kajovospend/integrations/)
- `ares.py` — fetch dodavatele podle IČO a mapování adresy; používá cache (viz audit docs/regen) fileciteturn41file7L17-L26.
- `openai_fallback.py` — volitelný „placený“ fallback. Používá OpenAI `/v1/models` a `/v1/responses` s timeouty a forenzním logováním; payload je validovaný proti internímu JSON schematu a chyby jsou redigované fileciteturn36file6L13-L21 fileciteturn36file11L32-L60.

### Utils (src/kajovospend/utils/)
- `config.py` — YAML load/save přes `yaml.safe_load` fileciteturn36file4L12-L17.
- `paths.py` — výpočet defaultních cest (data dir typicky `%LOCALAPPDATA%\KajovoSpend`) fileciteturn36file2L11-L16; default log dir je repo root/LOG, pokud není přepsáno v configu fileciteturn36file2L23-L26.
- `logging_setup.py` — logování je navrženo tak, aby žádný log write nikdy neshodil aplikaci (errors tolerant) fileciteturn36file3L7-L16.
- `forensic_context.py` — contextvars pro korelaci (correlation_id, job_id, phase…) a context manager `forensic_scope` fileciteturn36file14L8-L16 fileciteturn36file14L37-L43.

### Scripts / Build
- `scripts/bootstrap_codex_env.sh` — vytvoří venv na Pythonu >=3.11 (pyenv) a nastaví remote origin fileciteturn37file8L34-L49.
- `scripts/download_ocr_models.py` — volitelný download „pinned“ RapidOCR modelů; URL se můžou měnit fileciteturn37file7L1-L8.
- `Build/build_windows.ps1` a `Build/build_macos.sh` — PyInstaller build (viz `docs/build/README.md`) fileciteturn33file7L18-L35.

### Testy (tests/)
- `tests/unit/test_safe_move.py` — path traversal + Windows oddělovače pro `safe_move` fileciteturn38file0L8-L29.
- `tests/unit/test_db_net_gross_migration.py` — regresní test migrace a idempotence `init_db` fileciteturn43file1L17-L37.

## 4) Praktické „gotchas“ (časté chyby při změnách)

- Když přidáš nový `Document.extraction_method`, uprav RUN statistiky v `ui/db_api.py` (počítají podíly podle method) fileciteturn35file9L85-L93.
- Když přidáš nový sloupec/tabelu, musí existovat:
  1) ORM model (`models.py`),
  2) idempotentní migrace v `migrate.py`,
  3) unit test (nejméně ověření, že `init_db` jde spustit 2× bez změny výsledku).
- Nepřidávej UI operace, které blokují hlavní thread. `main_window.py` má `_SilentRunner` vzor pro background práci fileciteturn33file13L70-L79.
- Nepropaguj citlivé hodnoty do logů. OpenAI klíč se řeší mimo repo (uživatelská env proměnná) a sanitizuje se při načtení fileciteturn36file0L46-L50.
- Windows specifics: file locky a watchdog. `safe_move` má fallback copy+unlink pro PermissionError fileciteturn37file6L28-L46; watcher na Win+Py3.13 používá polling fileciteturn42file1L40-L47.

## 5) Doporučený workflow pro Codex CLI (jak postupovat)

1) Nejdřív dohledat existující vzor (grep/search) a vybrat nejmenší možný zásah.
2) Implementovat změnu v jedné vrstvě (ui/service/db/extract) a až pak napojit další vrstvy.
3) Přidat testy (minimálně: DB migrace idempotence / validační logika / bezpečnostní okraje).
4) Spustit:
   - `PYTHONPATH=src pytest tests`
   - `python -m compileall -q src tests`
5) Udržet malé, reviewovatelné commity (viz `docs/CONTRIBUTING.md`) fileciteturn41file4L17-L20.

## 6) Kde jsou data a logy (užitečné při debugování)

Typicky (dle README):
- DB: `%LOCALAPPDATA%\KajovoSpend\kajovospend.sqlite` fileciteturn41file2L34-L37
- Logy:
  - `%LOCALAPPDATA%\KajovoSpend\logs\kajovospend_gui.log`
  - `%LOCALAPPDATA%\KajovoSpend\logs\kajovospend_forensic.jsonl` fileciteturn41file2L38-L42
- Forenzní bundle pro konkrétní doklad: `paths.output_dir/FORENSIC/*.forensic.json` fileciteturn41file2L40-L42

Pozn.: `resolve_app_paths()` má fallbacky a může defaultovat logy do repo root/LOG, pokud config nepřepíše `app.log_dir` fileciteturn36file2L23-L26.
