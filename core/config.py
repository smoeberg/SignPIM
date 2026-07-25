import os
import sys
import logging

logger = logging.getLogger("ConfigValidator")

REQUIRED_ENV_VARS = [
    "DATABASE_URL"
]

def validate_environment():
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        logger.warning(f"WARNING: Missing optional environment variables for local dev: {', '.join(missing)}")
    else:
        logger.info("Environment configuration validated successfully.")
