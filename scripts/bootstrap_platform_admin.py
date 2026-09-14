"""Create the first platform owner. Run from a trusted deployment shell only."""
import argparse
import os

from core.persistence import PersistenceService
from core.saas_auth import SaaSAuthService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", default=os.environ.get("SIGNPIM_BOOTSTRAP_PASSWORD"))
    args = parser.parse_args()
    if not args.password:
        parser.error("provide --password or SIGNPIM_BOOTSTRAP_PASSWORD")
    dsn = os.environ.get("POSTGRES_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("POSTGRES_DSN or DATABASE_URL is required")
    service = SaaSAuthService(PersistenceService(dsn=dsn))
    account = service.create_account(args.email, args.password)
    service.grant_platform_role(account.id, "platform_owner")
    print(f"Platform owner ready: {account.email}")


if __name__ == "__main__":
    main()
