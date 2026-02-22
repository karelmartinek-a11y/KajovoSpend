# Verification – 02-ci-pytest-fix

## Cíl
- opravit CI pád `pytest: command not found` v GitHub Actions.

## Jak ověřeno
- statická kontrola workflow: přidán explicitní krok `pip install pytest` po instalaci runtime závislostí.
- lokální smoke validace YAML parserem:
  - `python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/ci.yml').read_text())"`

## Co se změnilo
- `.github/workflows/ci.yml`: v kroku Install dependencies je nyní explicitně instalován `pytest`.
- `docs/regen/parity/parity-map.yaml`: aktualizován stav modulu CI.

## Rizika / known limits
- v lokálním prostředí nebyl spuštěn plný běh testů kvůli omezenému přístupu na síť/proxy pro instalaci balíčků.
- fix je cílený pouze na dostupnost `pytest` binárky v CI runneru.
