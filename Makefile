.PHONY: install start stop restart logs status test

install:
	./install.sh

start:
	docker compose up -d

stop:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

status:
	docker compose ps

test:
	python3 -m pytest -v
