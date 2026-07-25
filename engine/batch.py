from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Callable

class BatchRuleEvaluator:
    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers

    def evaluate_batch(self, items: List[Dict[str, Any]], eval_func: Callable[[Dict[str, Any]], Any]) -> List[Any]:
        """Evaluates a batch of catalog items in parallel using ThreadPoolExecutor."""
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            results = list(executor.map(eval_func, items))
        return results
