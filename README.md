# KájovoSpend

Desktopová aplikace pro evidenci a kategorizaci dokladů (faktury, účtenky, daňové doklady, stvrzenky). Zpracování dokladů se spouští přímo z GUI na kartě RUN tlačítkem „IMPORTUJ“ (žádná externí služba / Scheduled Task).

## Funkce (aktuální implementace)

- GUI (PySide6): karta RUN (IMPORT + status + přehledné statistiky), ÚČTY, POLOŽKY, NEROZPOZNANÉ, DODAVATELÉ, PROVOZNÍ PANEL, PODEZŘELÉ, VÝDAJE.
- Deduplikace podle SHA256 (duplicitní soubory se přesunou do OUTPUT/DUPLICITY).
- OCR varianta B: RapidOCR (offline) + PDF render přes PDFium (pypdfium2). Pokud PDF obsahuje textovou vrstvu, použije se primárně ta.
- ARES: dotažení dodavatele podle IČO (při neúspěchu jde doklad do kontroly).
- Režim „raději do karantény“: při nízké jistotě vytěžení se soubor přesune do OUTPUT/KARANTENA a objeví se v kartě NEROZPOZNANÉ pro ruční doplnění.
- Forenzní bundle k analýze vytěžení: po zpracování vzniká JSON v `OUTPUT/FORENSIC`, který obsahuje důvody selhání/karantény, korelační ID, textové metriky a výtah textu pro následnou AI analýzu.
- Fulltext (FTS5) pro hledání v dokladech i položkách.
- Exporty CSV/XLSX ze seznamu dokladů.

## Požadavky

- Windows 10/11 nebo macOS
- Python 3.11 - 3.13

## Instalace

```powershell
cd KajovoSpend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Na macOS/Linux použij aktivaci:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

1) Vytvoř konfiguraci:

```powershell
copy config.example.yaml config.yaml
```

2) Uprav v `config.yaml` cesty `paths.input_dir` a `paths.output_dir`.

3) Inicializace DB proběhne automaticky při startu GUI.

## Spuštění

```powershell
.\.venv\Scripts\python.exe -m app_gui
```

## Práce s importem

1) Nakopíruj soubory (PDF/obrázky) do adresáře `paths.input_dir`.
2) Otevři kartu RUN a klikni na „IMPORTUJ“.
3) Doklady se po zpracování přesunou do `paths.output_dir` (případně do podadresářů DUPLICITY / KARANTENA).

## OCR modely (volitelné „připnutí“)

RapidOCR funguje i bez ručního stahování modelů. Pokud chceš držet modely explicitně lokálně, použij:

```powershell
.\.venv\Scripts\python.exe scripts\download_ocr_models.py --models-dir "%LOCALAPPDATA%\KajovoSpend\models\rapidocr"
```

## Databáze

SQLite je v `%LOCALAPPDATA%\\KajovoSpend\\kajovospend.sqlite` (pokud v configu nepřepíšeš).

## Logy

- `%LOCALAPPDATA%\KajovoSpend\logs\kajovospend_gui.log` – textový log běhu GUI/služby.
- `%LOCALAPPDATA%\KajovoSpend\logs\kajovospend_forensic.jsonl` – strukturovaný forenzní audit trail (JSONL).
- `paths.output_dir/FORENSIC/*.forensic.json` – balíček pro analýzu konkrétního dokladu (vhodné poslat spolu s `OUTPUT/KARANTENA` nebo `OUTPUT`).


## Spuštění GUI

Doporučené:

- `py -m app_gui` (Windows)
- `python -m app_gui` (macOS/Linux)

Alternativně lze spustit přímo skript:

- `py run_gui.py`


## Build aplikace (PyInstaller)

- Windows: `./Build/build_windows.ps1`
- macOS: `bash ./Build/build_macos.sh`

macOS build preferuje `assets/app.icns`; pokud soubor neexistuje, použije fallback `assets/app.ico`.

## Testování

```powershell
PYTHONPATH=src pytest tests
```

## Dokumentace

- Architektura: `docs/ARCHITECTURE.md`
- Bezpečnost: `docs/SECURITY.md`
- Přispívání: `docs/CONTRIBUTING.md`
- Diagramy: `docs/diagrams/`
