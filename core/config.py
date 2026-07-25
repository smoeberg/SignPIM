import os
import sys
import logging

logger = logging.getLogger("ConfigValidator")

REQUIRED_ENV_VARS = [
    # Add env vars required at runtime if strictly enforced
]

def validate_environment():
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        logger.critical(f"CRITICAL: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
    logger.info("Environment configuration validated successfully.")
