"""
Batch quality scoring service — the tenant-facing API of the ported
quality_score engine. Computes per-product and tenant-level 3D scores
and persists per-product scores via the repository (persistence is
injected, never assumed).
"""
import logging
from typing import Any, Dict, List, Optional

from engine.operators.rules import evaluate_rule
from engine.quality import (
    calculate_scores,
    PRODUCT_BATCH_SIZE,
    DEFAULT_REQUIRED_FIELDS,
    score_completeness,
    score_consistency,
    score_accuracy,
)

logger = logging.getLogger("signpim.batch_scoring")


class QualityScoringService:
    def __init__(self, repository=None):
        self.repo = repository

    def _batches(self, products: List[Dict], size: int):
        for i in range(0, len(products), size):
            yield products[i:i + size]

    def _score_product(
        self,
        product: Dict[str, Any],
        rules: List[Dict],
        sot: Dict[str, str],
        required_fields: List[str],
    ) -> Dict[str, Any]:
        """Single-product 3D breakdown."""
        completeness = score_completeness([product], required_fields)
        consistency = score_consistency([product], rules)
        accuracy = score_accuracy([product], sot)
        total = round(
            completeness * 0.4 + consistency * 0.4 + accuracy * 0.2, 2
        )
        return {
            'sku': product.get('sku'),
            'quality_score': total,
            'by_dimension': {
                'completeness': completeness,
                'consistency': consistency,
                'accuracy': accuracy,
            },
            'failing_rules': [
                r['rule_type'] for r in rules
                if evaluate_rule(r['rule_type'], product, r.get('parameters', {}))
            ],
        }

    def run(
        self,
        products: List[Dict[str, Any]],
        rules: Optional[List[Dict]] = None,
        source_of_truth_config: Optional[Dict] = None,
        required_fields: Optional[List[str]] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Scores a product list in fixed-size batches (legacy PRODUCT_BATCH_SIZE).
        Returns tenant-level 3D summary plus per-product breakdown.
        If repository is injected, per-product scores are persisted.
        """
        products = products or []
        rules = rules or []
        sot = source_of_truth_config or {}
        required_fields = required_fields or DEFAULT_REQUIRED_FIELDS

        if not products:
            return {
                'quality_score': 100.0,
                'by_dimension': {'completeness': 100.0, 'consistency': 100.0, 'accuracy': 100.0},
                'products_evaluated': 0,
                'per_product': [],
            }

        per_product: List[Dict[str, Any]] = []
        for batch in self._batches(products, PRODUCT_BATCH_SIZE):
            for p in batch:
                per_product.append(self._score_product(p, rules, sot, required_fields))

        # Tenant-level: batch-accurate 3D summary over all products
        all_scores = calculate_scores(
            products, rules=rules, source_of_truth_config=sot,
            required_fields=required_fields,
        )
        all_scores['per_product'] = per_product

        if self.repo is not None and tenant_id:
            for p in per_product:
                self.repo.save_quality_score(tenant_id, p['sku'], p['quality_score'])

        return all_scores
