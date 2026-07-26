import os
import sys
import logging

logger = logging.getLogger("ConfigValidator")

REQUIRED_ENV_VARS = [
    "DATABASE_URL",
    "JWT_SECRET"
]

def validate_environment():
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        logger.critical(f"CRITICAL: Missing required environment variables: {', '.join(missing)}")
        if os.getenv("NODE_ENV") == "production":
            sys.exit(1)
        else:
            logger.warning("Running in dev mode without required env vars.")
    else:
        logger.info("Environment configuration validated successfully.")
