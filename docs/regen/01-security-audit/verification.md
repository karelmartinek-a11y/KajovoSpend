# Verification – 01-security-audit

## Cíl
- provést audit bezpečnosti/stability a opravit kritické riziko globálního socket timeoutu v ARES integraci
- doplnit regresní test a základní dokumentaci + CI/pre-commit minimum

## Ověření
- `pytest -q tests/unit/test_ares_timeout_isolation.py tests/unit/test_ico_self_healing.py` (NOT PASS: chybí lokální dependencies / import balíčku)
- `PYTHONPATH=src pytest -q tests/unit/test_ares_timeout_isolation.py tests/unit/test_ico_self_healing.py` (NOT PASS: chybí `requests` v prostředí)
- `pip install requests pytest -q` (NOT PASS: blokovaný přístup na PyPI přes proxy)

## Co se změnilo
- odstraněno globální `socket.setdefaulttimeout(...)` z ARES klienta
- přidán unit regresní test na izolaci socket timeoutu
- přidána dokumentace architektury/security/contributing + mermaid diagramy
- přidán minimální CI workflow a pre-commit konfigurace

## Rizika / limity
- plné testy nebyly spuštěny kvůli nedostupnosti instalace závislostí v tomto prostředí
- CI nově testuje pouze pytest; lint/typecheck zatím není zaveden v repu
