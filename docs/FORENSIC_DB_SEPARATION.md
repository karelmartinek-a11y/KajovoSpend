# Forenzní analýza: oddělení ingest a produkční DB

## Cíl
Nepropustit do produkční DB žádný doklad, který není **kompletně vytěžen** (včetně kompletních detailů dodavatele).

## Zjištěné odchylky
1. V procesoru byl fallback, který po selhání ARES vytvářel dodavatele „best-effort“ a umožňoval pokračovat dál.
2. Pseudo-IČO mohlo vést k vytvoření dodavatele bez kompletních atributů.
3. Chyběla explicitní centrální „hard gate“ kontrola kompletnosti dodavatele před zápisem dokumentu.

## Oprava (zakotveno „do mramoru")
- Přidána metoda `_supplier_details_complete(...)`, která kontroluje povinné atributy dodavatele.
- Po selhání ARES už nedochází k produkčnímu fallbacku dodavatele.
- Pseudo-IČO je explicitně blokováno pro produkční zápis.
- Pokud dodavatel není kompletní, dokument jde do karantény a do produkční DB se nezapíše.

## Povinné dodavatelské atributy pro produkční DB
- IČO
- název
- adresa
- ulice
- číslo popisné
- město
- PSČ
- právní forma
- status plátce DPH
- timestamp ARES synchronizace

## Fyzické oddělení databází
- Ingest/processing DB: `paths.processing_db` nebo fallback `app.data_dir/kajovospend-processing.sqlite`.
- Produkční DB: `app.db_path`.

Tím je oddělení fyzicky i logicky zachováno.
