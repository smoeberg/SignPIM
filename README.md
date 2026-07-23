# SignPIM (v3.2.0)

SignPIM er en "Virtual PIM" motor udviklet af Signalement, der automatiserer overvågning og forbedring af produktdata på tværs af salgskanaler.

## Kernefunktioner

### 1. Kvalitetsscoring (Quality Score)
Systemet beregner en samlet kvalitetsscore for hvert tenant baseret på tre dimensioner:
- **Completeness (40%)**: Tjekker om påkrævede felter (SKU, navn, pris, EAN, billeder) er udfyldt.
- **Consistency (40%)**: Tjekker om produktet overholder definerede forretningsregler.
- **Accuracy (20%)**: Validerer om data i PIM'en er synkroniseret med den definerede "Source of Truth" (SoT).

### 2. Auto-Resolution
SignPIM kan automatisk lukke opgaver, hvis fejlen er rettet i kildesystemet. For at undgå "flapping" (hvor en opgave åbner og lukker hele tiden), skal følgende kriterier være opfyldt:
- Reglen overholdes nu.
- Mindst **2 på hinanden følgende succesfulde synkroniseringer**.
- Mindst **10 minutter** siden sidste fejl.

### 3. Regler (Rule Evaluator)
Systemet understøtter en række prædefinerede regler:
- `missing_images`: Finder produkter uden billeder.
- `price_deviation`: Finder produkter hvor prisen afviger for meget fra kildeprisen.
- `valid_ean_13`: Validerer EAN-13 format og checksum.
- `missing_sku`: Tjekker for manglende eller for korte SKU'er.
- `invalid_image_url`: Validerer at billed-URL'er er korrekte (http/https).
- `unsupported_currency`: Sikrer at priser er i understøttede valutaer.

## Installation & Udvikling

### Node.js (API)
```bash
npm install
npm start
```

### Python (Scripts)
Scripts til kvalitetsscoring og auto-resolution kræver Python 3.8+:
```bash
pip install -r requirements.txt
python quality_score.py
```

## Arkitektur
SignPIM er designet til at køre asynkront som baggrundsopgaver, der fodrer data ind i Dolibarr (via EIRA Commerce Hub).

---

# Volume 27 – Legacy Migration (SignPIM to EIRA v6.1)

Dette afsnit beskriver strategien for at migrere kernefunktionaliteten fra SignPIM v3.2 ind i EIRA Commerce Hub v6.1 arkitekturen.

## 1. Genbrug af komponenter (Reused)
Følgende kerne-logik genbruges direkte:
*   **`rule_evaluator.py`**: Hele validerings-motoren og de individuelle regel-funktioner.
*   **`quality_score.py`**: Den vægtede scorings-algoritme (Completeness, Consistency, Accuracy).
*   **`auto_resolver.py`**: Stabilitets-logikken (Stability window) og flapping-beskyttelse.

## 2. Omskrivning (Rewritten)
*   **Database Layer**: SignPIMs direkte `psycopg2` kald omskrives til at bruge EIRA's centrale `DB_Service`.
*   **API Interface**: Den Express-baserede `app.js` i SignPIM erstattes af EIRA's REST API.
*   **Auth & Tenant Management**: SignPIMs isolerede tenant-styring migreres til EIRA's globale Multi-Tenant framework.

## 3. Sletning (Deleted)
*   **SignPIM Express Boilerplate**: Alle filer relateret til den selvstændige Node.js server (`middleware/`, `routes/`, `package.json`).
*   **Redundante Scripts**: Gamle migrations-scripts der kun vedrører SignPIM v2.x.

## 4. Migrations-test (Acceptance Criteria)
En komponent anses først for migreret, når følgende test er bestået:
1.  **Parity Test**: scanning i både SignPIM (legacy) og EIRA v6.1 (new) skal give identiske scores.
2.  **Auto-Resolve Loop**: En bevidst provokeret fejl skal automatisk lukkes af den nye motor efter 2 syncs.
3.  **Performance Baseline**: Beregning for 1.000 produkter må ikke tage over 5 sekunder.

## 5. Cleanup Strategy (Fjernelse af /legacy)
Mappen `legacy/` bevares i **30 dages stabil drift** efter udrulning. Når `quality_snapshots` tabellen har 30 dages sammenhængende data uden afvigelser, fjernes mappen permanent.


