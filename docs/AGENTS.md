# Repository Guidelines

## Jazyk komunikace (POVINNE)
- AI agent **KOMUNIKUJE VYHRADNE V CESTINE**
- Plati pro:
  - popisy zmen
  - commit zpravy
  - tagy / milniky
  - vystupy, vysvetleni, komentare
  - dokumentaci
📌 Anglictina je povolena pouze:
- v samotnem aplikacnim kodu
- v nazvech promennych, funkci a API
- Poznamky u placeholderu i dalsi komentare ve zdrojovych kodech musi byt cesky; narazi-li Codex na jinou rec, prelozi je do cestiny. Veskerou dokumentaci drzte pouze v cestine.

## Project Structure & Modules
- `src/kajovospend/`: aplikační kód – databázové modely, service (watcher/processor), UI (Qt/PySide6) a utilitky.
- `app_gui/`: balíček se startem GUI pro `py -m app_gui` (interně volá `run_gui.py`).
- `assets/`: ikony a grafika, `INPUT/` a `OUTPUT/` slouží jako pracovní adresáře.
- `scripts/`: helpery (např. stažení OCR modelů, registrace služby).
- `tests/`: připravené místo pro testy (zatím prázdné).

## Setup, Build & Run
- Vytvoř venv: `python -m venv .venv && .\\.venv\\Scripts\\activate` (Windows).
- Závislosti: `pip install -r requirements.txt`.
- GUI: `python -m app_gui` (načte/uloží `config.yaml`, komunikuje se službou; alternativně `py run_gui.py`).

## Coding Style & Naming
- Python 3.13+, preferuj typové anotace a f-strings.
- Pojmenování: snake_case pro funkce/proměnné, PascalCase pro třídy; srozumitelné názvy bez zkratek.
- U SQLAlchemy používej `select(...)` nebo `text(...)` pro raw SQL (nutné v UI money přehledech).

## Testing Guidelines
- Preferuj `pytest`; umisťuj testy do `tests/` se jmény `test_*.py`.
- Přidej rychlé smoke testy pro watcher a processor; u UI izoluj logiku do testovatelných funkcí.
- Před PR spusť alespoň cílené testy: `pytest tests` (jakmile existují).

## Commit & Pull Requests
- Commity: stručný imperativ (`fix watcher polling on win`, `add money aggregates text()`). Drž jeden logický celek na commit.
- PR: krátký popis problému + řešení, zmínit dopad na službu/GUI, přiložit příkazy/testy, relevantní screenshoty GUI.
- GitHub repozitář: karelmartinek-a11y/KajovoSpend (přístupový token drž mimo verzování)

## Security & Configuration Tips
- Osobní tokeny (PAT) neukládej do repozitáře; používej git credential helper nebo `.env` (viz `.env.example`), který je ignorovaný.
- Sdílené hodnoty dávej jen do šablon typu `config.example.yaml` nebo `.env.example`.

