from functools import lru_cache
from typing import Tuple

class QualityScoreCache:
    @staticmethod
    @lru_cache(maxsize=256)
    def _compute_cached_score(violations_tuple: Tuple[str, ...], total_rules: int) -> float:
        if total_rules == 0:
            return 100.0
        fail_count = len(violations_tuple)
        score = max(0.0, 100.0 - (fail_count * (100.0 / total_rules)))
        return round(score, 2)

    @classmethod
    def get_score(cls, violations: list, total_rules: int) -> float:
        # Convert list to tuple for immutability and hashing
        return cls._compute_cached_score(tuple(sorted(violations)), total_rules)
