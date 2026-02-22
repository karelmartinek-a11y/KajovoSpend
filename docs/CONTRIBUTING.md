# Contributing

## Lokální setup
1. `python -m venv .venv`
2. Aktivace venv (Windows): `.\.venv\Scripts\activate`
3. Instalace: `pip install -r requirements.txt`

## Běh aplikace
- Služba: `python service_main.py --config config.yaml`
- GUI: `python -m app_gui`

## Testování
- Unit/integration: `PYTHONPATH=src pytest tests`

## CI lokálně
- Spustit stejné kroky jako CI workflow: lint (pokud dostupný), testy.

## Standardy
- malé, reviewovatelné commity
- regresní test pro každý fix
- dokumentace + diagramy musí odpovídat realitě v kódu
