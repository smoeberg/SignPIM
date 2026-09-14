"""Export the FastAPI OpenAPI spec for partner onboarding."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.app import app

spec = app.openapi()
out = sys.argv[1] if len(sys.argv) > 1 else "docs/openapi.json"
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
with open(out, "w") as f:
    json.dump(spec, f, indent=2, ensure_ascii=False)
print(f"OpenAPI spec written to {out} ({len(spec['paths'])} paths)")
