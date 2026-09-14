# SignPIM Pilot Runbook — Bygmarked A/S

## Formål
Reel leverandørfeed (Skovgaard Industri A/S, uge 38) gennem hele pipeline: import →
kolonne-mapping → normalisering → motor → kvalitetsscore → ERP-eksport.

## Hurtig start
```bash
docker compose -f docker-compose.prod.yml up -d db        # eller lokal Postgres
export DATABASE_URL="postgresql+psycopg://signpim:...@localhost/signpim"
python pilot/setup_pilot.py                                # idempotent — trygt at re-køre
```

## Pipeline-trin (svarer til output [1]-[7])
1. **Tenant** — `bygmarked-as` oprettes med settings (kolonne-map, VAT-flag, LLM-provider)
2. **Mappings** — leverandørværdier → kanoniske (`SKOVGAARD INDUSTRI A/S` → `Skovgaard Industri A/S` osv.)
3. **Ingest** — CSV med `;`-delimiter, danske kolonnenavne (`varenr`, `pris_ink_moms`) via `feed_column_map`
4. **Kvalitet** — 3D-summary (completeness/consistency/accuracy) + per-produkt score & violations
5. **ERP-eksport** — kun produkter med score ≥ 75, kolonner omdøbt til ERP-navne (`varetekst`, `stregkode`, `lagerantal`)
6. **Feed-poll** — lokal/SFTP-import med SHA-256-checksum-idempotens
7. **Oversigt** — tenant-niveau summary

## Kritiske config-flag
| Flag | Effekt |
|---|---|
| `feed_column_map` | Leverandør-kolonner → interne felter (config, ikke kode) |
| `feed_price_includes_vat` | `true` = priser er inkl. moms → VAT-multiply skip i `full_sync` |
| `export_column_map` | Interne felter → ERP-kolonne-navne |
| `llm.provider` | `mock` (offline pilot) → `ollama`/`openai` når klar |

## Fejl der blev fundet af piloten (og fikset)
1. **Dobbel moms:** `full_sync.yaml` multiplicerede pris × 1.25 på feeds der
   allerede var inkl. moms (`pris_ink_moms`) → 499,95 blev til 624,94.
   Løsning: `feed_price_includes_vat`-flag i runtime — config, ikke kode.
2. **Dansk CSV:** `;`-delimiter og komma-decimaler (`499,95`) — auto-detekteret
   i `parse_csv` via `csv.Sniffer`.

## Reglerne der ramte pilotfeedet
- `missing_images`: alle 7 produkter mangler billeder → completeness 51%.
  Næste skridt: bilde-batch-import eller AI-generering.
- `valid_ean_13`: 2 produkter mangler EAN, 1 har invalid checksum (5901234999991).
- `ready_for_publication`: automatisk draft pga. lav score.

## SFTP-skift når leverandøren er klar
```python
imp = SFTPFeedImporter(persistence, ingestion)
imp.poll("bygmarked-as", host="ftp.leverandoer.dk", remote_dir="/out",
         username="signpim", password="...")
```
API: `POST /feeds/{slug}/poll` — samme checksum-idempotens.
