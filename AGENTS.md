# Repository Guidelines

## Project Structure & Modules
- `src/kajovospend/`: aplikační kód – databázové modely, service (watcher/processor), UI (Qt/PySide6) a utilitky.
- `service_main.py`: start služby (filesystem watcher + job queue).
- `app_gui.py`: desktop GUI klient.
- `assets/`: ikony a grafika, `INPUT/` a `OUTPUT/` slouží jako pracovní adresáře.
- `scripts/`: helpery (např. stažení OCR modelů, registrace služby).
- `tests/`: připravené místo pro testy (zatím prázdné).

## Setup, Build & Run
- Vytvoř venv: `python -m venv .venv && .\\.venv\\Scripts\\activate` (Windows).
- Závislosti: `pip install -r requirements.txt`.
- Služba: `python service_main.py --config config.yaml` (watcher běží proti `paths.input_dir`).
- GUI: `python app_gui.py` (načte/uloží `config.yaml`, komunikuje se službou).
- DB migrace: `python -c "from kajovospend.db.migrate import init_db; from kajovospend.db.session import make_engine; from kajovospend.utils.paths import resolve_app_paths; import kajovospend.utils.config as c, pathlib; cfg=c.load_yaml(pathlib.Path('config.yaml')); paths=resolve_app_paths(cfg['app'].get('data_dir'), cfg['app'].get('db_path'), cfg['app'].get('log_dir'), cfg.get('ocr',{}).get('models_dir')); init_db(make_engine(str(paths.db_path)))"` (typicky nutné jen při prvním spuštění).

## Coding Style & Naming
- Python 3.13+, preferuj typové anotace a f-strings.
- Pojmenování: snake_case pro funkce/proměnné, PascalCase pro třídy; srozumitelné názvy bez zkratek.
- Logger získávej injekcí nebo `logging.getLogger(__name__)`.
- U SQLAlchemy používej `select(...)` nebo `text(...)` pro raw SQL (nutné v UI money přehledech).

## Testing Guidelines
- Preferuj `pytest`; umisťuj testy do `tests/` se jmény `test_*.py`.
- Přidej rychlé smoke testy pro watcher a processor; u UI izoluj logiku do testovatelných funkcí.
- Před PR spusť alespoň cílené testy: `pytest tests` (jakmile existují).

## Commit & Pull Requests
- Commity: stručný imperativ (`fix watcher polling on win`, `add money aggregates text()`). Drž jeden logický celek na commit.
- PR: krátký popis problému + řešení, zmínit dopad na službu/GUI, přiložit příkazy/testy, relevantní screenshoty GUI.
- Kontroluj, že `config.yaml` neobsahuje tajemství; API klíče ukládej jen lokálně.

## Security & Configuration Tips
- `config.yaml` drž mimo verzování citlivých údajů; pokud je třeba sdílet šablonu, použij `config.example.yaml`.
- Watchdog na Windows+Py3.13 používá fallback polling (viz `service/watcher.py`); při výkonových problémech zvaž snížení scan intervalů v configu.
