#!/usr/bin/env bash
set -euo pipefail

REPO_URL_DEFAULT="https://github.com/karelmartinek-a11y/KajovoSpend.git"
VENV_DIR="${1:-.venv312}"
REPO_URL="${REPO_URL:-$REPO_URL_DEFAULT}"

if ! command -v pyenv >/dev/null 2>&1; then
  echo "Chyba: pyenv není dostupný. Nainstaluj pyenv nebo spusť bootstrap na Pythonu 3.11+ ručně." >&2
  exit 1
fi

pick_python_version() {
  local versions
  versions="$(pyenv versions --bare | tr -d ' ')"
  if grep -qx '3.12.12' <<<"$versions"; then
    echo '3.12.12'; return
  fi
  if grep -qx '3.13.8' <<<"$versions"; then
    echo '3.13.8'; return
  fi
  if grep -Eq '^3\.(1[1-9]|[2-9][0-9])\.' <<<"$versions"; then
    grep -E '^3\.(1[1-9]|[2-9][0-9])\.' <<<"$versions" | sort -V | tail -n1
    return
  fi
  return 1
}

PY_VERSION="$(pick_python_version)" || {
  echo "Chyba: nebyla nalezena žádná pyenv verze Pythonu >= 3.11." >&2
  exit 1
}

echo "Použitý Python: ${PY_VERSION}"
PYENV_VERSION="$PY_VERSION" python -m venv "$VENV_DIR"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
pip install -r requirements.txt
pip install build

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REPO_URL"
  echo "Remote origin aktualizován na: $REPO_URL"
else
  git remote add origin "$REPO_URL"
  echo "Remote origin přidán: $REPO_URL"
fi

echo "Hotovo. Aktivace prostředí: source $VENV_DIR/bin/activate"
