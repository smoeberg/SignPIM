# SignPIM — AI Operator Plan (v1)

## Positionering (beslutning)

SignPIM er **ikke** et "AI-drevet PIM". Det er en **determinisk datakvalitetsmotor med AI-operatorer som valgfrit berigelseslag**.
AI må aldrig kunne ændre data uden at gennemgå samme regel- og valideringspipeline som mennesker — det er kernen i det matematisk kvalitetskoncept.

| Positionering | Konsekvens |
|---|---|
| AI = operatorer i workflow-grafen | Samme governance som alle andre actions: audit, rollback, versionering |
| AI-output er altid "proposed" | Ny felt-status: `ai_proposed` — skal godkendes (regler/human) før `committed` |
| Offline-foretrukket | Default-provider er lokal Ollama; cloud kun som opt-in pr. tenant |

## Nye operatorer (RichOperatorRegistry)

| Operator | Indgang | Output | Provider | Risiko-kontrol |
|---|---|---|---|---|
| `llm_classify` | fritekst-felt (f.eks. leverandørbetegnelse) | kategori/farve/størrelse-normalisering | Ollama (qwen2.5:7b) eller OpenAI | confidence-threshold; under 0.85 → violation `ai_low_confidence` |
| `llm_enrich` | eksisterende produktdata | manglende felter (beskrivelse, nøgleord) | samme | SKRIV ALDRIG til `sku`/`ean`/`price` — whitelist af berigbare felter |
| `llm_match_supplier` | rå leverandør-CSV-kolonneoverskrifter | mapping til interne felter | samme | output = proposed mapping i YAML-diff, aldrig auto-applied |
| `llm_translate` | beskrivelse | da/sv/no/en oversættelser | samme | sprog-tag pr. felt (allerede JSONB-klar) |

## LLM-provider-adapter

`engine/llm_providers.py` — interface med tre implementeringer:
- `MockLLMProvider` — deterministisk, testbar (tests kører 100% offline)
- `OllamaProvider` — lokal, standard i prod
- `OpenAIProvider` — opt-in pr. tenant (API-nøgle i tenant-settings, aldrig i kode)

Fælles: timeout, retry (2x), token-budget pr. ingest-batch, og **cost-logger** i ny tabel `llm_calls` (tenant_id, operator, tokens, latency_ms, success).

## Workflow-integration

```yaml
# meta/workflows/enrich_sync.yaml (ny)
nodes:
  - validate            # eksisterende regler — kører først
  - llm_enrich          # kun felter der er tomme EFTER validering
  - validate            # gen-valider: AI-output skal bestå SAMME regler
  - calculate
  - persist
```

Nøgleprincip: **AI kan aldrig løfte en score uden at bestå reglerne.** Hvis `llm_enrich` skriver en beskrivelse der fejler `short_description`, fejler produktet — som al anden data.

## Sikkerhed & governance

1. **Ingen AI i cross-tenant-veje**: provider-kald får kun tenantens egne data (allerede håndhævet af isolation-laget).
2. **Audit**: hvert AI-kald logges i `llm_calls` med prompt-hash (ikke fuld prompt — PII-beskyttelse) og response-hash.
3. **Cost-control**: pr. tenant daily token-budget; overskrides → operatorer degraderes til "skip" med warning.
4. **Rollback**: AI-felter markeres i `data._ai_meta = {field: {model, confidence, at}}` — fuldt audit-spørbart.

## Testbarhed

- 100% af tests kører på MockLLMProvider (ingen netværk).
- Invariant-test: AI-output gennemgår altid samme validate-pipeline — "AI kan ikke snyde sig til højere score".
- CI-budget: nye tests må ikke bruge >5s (mock er instant).

## Leveringsplan (3 commits)

1. `engine/llm_providers.py` + MockLLMProvider + cost-logger (`llm_calls`-tabel)
2. De 4 operatorer + confidence-gate + felt-whitelist + audit-meta
3. `enrich_sync`-workflow + invariant-tester
