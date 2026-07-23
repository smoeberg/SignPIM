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

