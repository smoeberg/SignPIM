import uuid
from typing import Optional, Dict, Any

class AppError(Exception):
    def __init__(self, message: str, status_code: int = 500, error_code: str = "INTERNAL_ERROR", details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.details = details or {}

class ValidationError(AppError):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message, status_code=400, error_code="VALIDATION_ERROR", details=details)

class TenantIsolationError(AppError):
    def __init__(self, message: str = "Tenant ID missing or invalid"):
        super().__init__(message, status_code=403, error_code="TENANT_ISOLATION_ERROR")

def generate_correlation_id() -> str:
    return str(uuid.uuid4())
