# Spend Better OpenAI Audit Plan

Datum: 2026-03-06
Scope: forensic map only (no large implementation in this step)

## Cile auditu

Tento dokument mapuje presne, jak dnes funguje pipeline pro OpenAI vytazeni dokladu, kde jsou slabiny a jaky je navrh implementacnich kroku pro dalsi prompty.

## 1) Oprava rezimu OpenAI a gatingu - soucasny stav

### Konfiguracni vstupy

- `config.example.yaml`:
  - `openai.enabled: false`
  - `openai.auto_enable: true`
  - `openai.primary_enabled: true`
  - `openai.only_openai: false`
  - `features.openai_fallback.enabled: false`
- `config.yaml` (lokalni runtime):
  - `openai.enabled: true`
  - `openai.auto_enable: false`
  - `openai.primary_enabled: false`
  - `openai.only_openai: true`
  - `openai.fallback_enabled: false`
  - `features.openai_fallback.enabled: false`

### Realna gating logika v `src/kajovospend/service/processor.py`

- Hlavni prepinac rezimu: `openai_only = bool(openai_cfg.get("only_openai", False))` (`processor.py:1435`).
- Vypocet OpenAI dostupnosti:
  - `openai_feature_enabled = bool((features.get("openai_fallback", {}) or {}).get("enabled", False))` (`processor.py:1697`)
  - `openai_enabled = bool((openai_feature_enabled or openai_only) and (OpenAIConfig is not None) and api_key and (openai_cfg.get("enabled") or auto_enable))` (`processor.py:1698`)
- `primary_enabled` je pouzito pro primary vetveni (`processor.py:1699`, `processor.py:1736`).
- `fallback_enabled` se v processoru nepouziva jako gate pro fallback vetveni. Fallback se ridi podminkou `if not template_used and openai_enabled and (need_openai or openai_only)` (`processor.py:1860`).
- Duledek: UI/konfigurace `fallback_enabled` neodpovida runtime chovani.

### Rizika

- Nekonzistentni konfiguracni model mezi UI, `config.yaml` a runtime gatingem.
- Mozne prekvapive chovani: fallback muze bezet i kdyz je `fallback_enabled: false`.

## 2) Odstraneni hlavniho zdroje failu - truncace JSON - soucasny stav

- OpenAI request nastavuje fixni `max_output_tokens` (`openai_fallback.py:721`).
- Pokud model vrati dlouhy JSON, odpoved muze byt utnuta na cap.
- V aktualnich forenznich datech jsou parse fail odpovedi vazane na `output_tokens == 2000` (hard cap).

### Slabina

- Pri parse fail neni explicitni retry strategie "zvys cap a opakuj".
- Soucasny retry mechanismus je orientovan hlavne na HTTP chyby (`openai_fallback.py:584+`) nebo 400 schema fallback (`openai_fallback.py:738-781`), ne na truncaci validni 200 odpovedi.

## 3) Robustnejsi parse OpenAI odpovedi - soucasny stav

- Parse flow:
  1. `_extract_output_text` slozi output text.
  2. Vezme se substring `first {` az `last }` (`openai_fallback.py:795-802`).
  3. `json.loads(fragment)` (`openai_fallback.py:803`).
- Loguje se `structured_output.parse` se strategi `first_last_brace` (`openai_fallback.py:812-821`).

### Slabina

- Strategie je jednorazova a krehka:
  - truncace,
  - extra text v odpovedi,
  - vice JSON-like bloku.
- Pri fail parse se dale nejde do robustnejsi recover strategie.

## 4) Zmirneni supplier hard gate - soucasny stav

- Supplier gate je centralizovan v `_supplier_details_complete` (`processor.py:221+`).
- Dnes vyzaduje: ICO, nazev, adresa, ulice, cislo popisne, mesto, PSC, pravni forma, ARES synchronizace, a stav platce DPH.
- Pokud chybi, doklad jde do QUARANTINE (`processor.py:2107-2120`).

### Slabina

- Na cast dokladu je gate prilis prisny: i dobre vytezene doklady mohou spadnout kvuli doplnkovym supplier metadatum.
- Vysoka zavislost na ARES kvalite/dostupnosti pro finalni pruchod.

## 5) Zlepseni forenzni traceability - soucasny stav

- `forensic_scope` umi pole: `correlation_id`, `document_id`, `file_sha256`, `job_id`, `phase`, `attempt`, `mode`, `openai_request_id_client` (`utils/forensic_context.py`).
- Processor nastavuje globalni ingest scope s `correlation_id`, `job_id`, `file_sha256` (`processor.py:1371`).
- OpenAI vrstva vklada vlastni scope jen s `openai_request_id_client`, `attempt`, `mode` (`openai_fallback.py:503`).

### Co chybi

- V OpenAI eventech neni garantovane propojeni na `correlation_id`/`file_sha256`/`job_id` pro rychle 1:1 sparovani requestu na konkretni doklad (zalezi na scope propagaci mezi vlakny/callbacky).
- Chybi explicitni test, ktery to hlida end-to-end na `openai.request`/`openai.response` eventech.

## 6) Sablony pro opakujici se formaty + reason cleanup - soucasny stav

### `src/kajovospend/extract/standard_receipts.py`

- Modul umi:
  - schema parse/validaci/serializaci (`parse_template_schema_text`, `validate_template_schema_text`, `serialize_template_schema`),
  - matching podle `match_supplier_ico_norm` + `match_texts_json` (`match_template`),
  - extrakci podle ROI z preddefinovanych poli (`extract_using_template`).
- Modul neum:
  - import/export helper pro sablony (bulk IO),
  - seed mechanismus opakovanych vzoru (katalog + aplikace seedu).

### Reason cleanup

- `_prune_receipt_reasons` odfiltruje jen presny text `nekompletni vytezeni` bez diakritiky (`processor.py:1245-1254`).
- Runtime reason se pridava jako `nekompletní vytěžení` (`processor.py:1982`).
- Duledek: pruning netrefi realny text.

---

## Konretni mista, ktera se maji menit v dalsich krocich

1. `src/kajovospend/service/processor.py`
   - OpenAI gating (sekce kolem `openai_enabled`, `primary_enabled`, fallback vetveni).
   - Supplier gate politika (`_supplier_details_complete` + callsite po supplier upsert).
   - Receipt reason pruning (`_prune_receipt_reasons`).

2. `src/kajovospend/integrations/openai_fallback.py`
   - Retry strategie na truncaci/parse fail.
   - Robust parse strategie po `first_last_brace` failu.
   - Rozsirene forenzni pole v OpenAI eventech.

3. `src/kajovospend/extract/standard_receipts.py`
   - (minimal) helper API pro import/export sablon.
   - priprava hooku pro seed sablon.

4. `src/kajovospend/utils/forensic_context.py`
   - pouze pokud bude potreba rozsireni scope propagace (jinak beze zmen).

5. `config.example.yaml` a `config.yaml`
   - sladeni vychozich rezimu OpenAI a fallback prepinacu s runtime logikou.

6. `.github/workflows/ci.yml`
   - pouze pokud pridane testy budou vyzadovat novy krok (aktualne pravdepodobne bez nutnosti zmeny).

---

## Stavajici testy, ktere se maji upravit

- `tests/unit/test_openai_fallback_retry.py`
  - doplnit scenar parse-fail/truncation retry (nejen 429).
- `tests/unit/test_supplier_gate.py`
  - upravit ocekavani podle nove "soft" supplier policy (ktera pole jsou hard vs soft).
- `tests/test_forensic_logging.py`
  - overit, ze OpenAI eventy nesou navazane forensic identifikatory pro pairing.

Dalsi zjevne dotcene testy ke kontrole/udrzeni kompatibility:
- `tests/test_json_schema_defaults.py`
- `tests/test_openai_schema_invariants.py`
- `tests/unit/test_processor_forensic_bundle.py`
- `tests/unit/test_standard_receipt_templates_schema.py`
- `tests/unit/test_standard_receipts_multi_page_items.py`

---

## Nove testy, ktere se maji pridat

1. `tests/unit/test_openai_truncation_retry.py`
   - 200 odpoved + parse fail + output token cap -> retry se zvysenym limitem -> success.

2. `tests/unit/test_openai_parse_recovery.py`
   - fail `first_last_brace`, success pres fallback parser strategii.

3. `tests/unit/test_openai_mode_gating.py`
   - matice `only_openai/primary_enabled/fallback_enabled/enabled/auto_enable/features.openai_fallback.enabled`.

4. `tests/unit/test_forensic_openai_linkage.py`
   - explicitni assert na `correlation_id`, `file_sha256`, `job_id` v OpenAI eventech.

5. `tests/unit/test_template_seed_io.py`
   - smoke test import/export helperu a validace seed payloadu.

6. `tests/unit/test_receipt_reason_pruning.py`
   - diakritika / normalizace reason textu.

---

## CI zmeny

Aktualni stav:
- CI uz spousti `pytest tests/unit` a `pytest tests/integration` + compile sanity.

Predbezne rozhodnuti:
- Pro tento plan neni nutna okamzita zmena CI workflow.
- Pokud pribudou test fixtures se slow markerem, zvazit rozdeleni unit test jobu (zatim N/A).

---

## Poradi implementace (navrh pro dalsi prompty)

1. Gating rezimu OpenAI (nejdriv logika, potom config defaults).
2. Truncation retry + robust parse recovery.
3. Forenzni traceability v OpenAI eventech.
4. Supplier gate softening.
5. Reason cleanup + template helper/seed skeleton.
6. Final test/CI stabilization.

---

## Exit criteria pro dalsi prompty

### Prompt 2 - OpenAI gating + config consistency
- `processor.py` respektuje `primary_enabled` i `fallback_enabled`.
- Kombinace prepinacu ma deterministicke chovani pokryte testem (`test_openai_mode_gating.py`).
- `config.example.yaml` reflektuje realne podporovane rezimy.

### Prompt 3 - Truncation + parse robustness
- Pri parse fail na capu se provede retry se zvysenym `max_output_tokens`.
- Parse recovery fallback snizi fail rate proti baseline scenari.
- Testy `test_openai_fallback_retry.py`, `test_openai_truncation_retry.py`, `test_openai_parse_recovery.py` prochazi.

### Prompt 4 - Supplier gate policy
- Hard gate zustava jen pro skutecne kriticka pole.
- Soft metadata chyby jdou do review reasons, ne automaticky do blokace.
- `test_supplier_gate.py` odpovida nove politice.

### Prompt 5 - Forenzni linkage
- `openai.request/openai.response/openai.error` nesou jednoznacne linkovaci forensic pole.
- Pairing request-response-document je testovatelny z logu.
- `test_forensic_logging.py` + novy linkage test prochazi.

### Prompt 6 - Template ops + reason cleanup
- Pridan helper API pro import/export sablon a validace seedu.
- Opraveno pruning reason textu (vcetne diakritiky).
- Template a reason testy prochazi.

### Final hard gate
- `PYTHONPATH=src pytest tests`
- `python -m compileall -q src tests`
- Bez regresi v `tests/integration`.

