# Build (Windows + macOS)

Tento adresář obsahuje postup a skripty pro build aplikace na obou platformách.

## Předpoklady
- Python 3.11–3.13
- `pip install -r requirements.txt`
- `pip install pyinstaller`

## Důležité: ikona všude
Projekt používá:
- `assets/app.ico` pro Windows executable a ikonu okna.
- `assets/logo.png` jako univerzální asset pro UI/fallback.

Skripty níže nastavují ikonu i ve výsledném buildu (`--icon`).
Aplikace sama nastavuje ikonu okna v `run_gui.py`.

## Windows build
Spusť v PowerShellu:

```powershell
./Build/build_windows.ps1
```

Výstup: `dist/KajovoSpend/KajovoSpend.exe`

## macOS build
Spusť v shellu:

```bash
bash ./Build/build_macos.sh
```

Výstup: `dist/KajovoSpend.app`

> Poznámka: `assets/app.ico` je používána i na macOS build kroku. Pokud chceš nativní `.icns`, přidej `assets/app.icns` a uprav parametr `--icon` ve skriptu.

## Ruční build (stejná logika)

```bash
pyinstaller \
  --noconfirm \
  --clean \
  --name KajovoSpend \
  --windowed \
  --icon assets/app.ico \
  --add-data "assets:assets" \
  --add-data "src:src" \
  run_gui.py
```

Na Windows použij oddělovač `;` v `--add-data` (skript to řeší automaticky).
