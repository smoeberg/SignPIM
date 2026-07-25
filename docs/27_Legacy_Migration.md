# Volume 27: Legacy Migration

**Status:** Aktiv – Sprint 0-9
**Formål:** Beskrive migreringsvejen fra virtual-pim-api til den nye V6.1-platform

---

## 27.1 Oversigt

```yaml
legacy_migration:
  kilde: "virtual-pim-api v3.2.0"
  destination: "SignPIM V6.1"
  strategi: "Incremental migration – byg nyt, portér logik"
  genbrugsgrad: "~30% (regler, auto-resolve, kvalitetsscore)"
```

## 27.2 Fil-for-fil Beslutninger

| Fil (legacy) | Handling | Ny fil | Status |
|--------------|----------|--------|--------|
| app.js | Omskriv | packages/backend/src/index.js | ✅ |
| middleware/auth.js | Slet | middleware/auth.js | ✅ |
| rule_evaluator.py | Genbrug | packages/python-services/src/rule_evaluator.py | ✅ |
| auto_resolver.py | Genbrug | packages/python-services/src/auto_resolver.py | ✅ |
| quality_score.py | Genbrug | packages/python-services/src/quality_score.py | ✅ |

## 27.3 Hvornår kan legacy/ fjernes?
- Alle funktioner fra virtual-pim-api er portéret til ny platform
- Alle tests (unit, integration, E2E) består på ny platform
- Pilotkunde har kørt i 30 dage på ny platform uden kritiske fejl
- CTO + Teknisk Arkitekt godkender
