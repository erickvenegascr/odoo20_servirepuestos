.PHONY: config build up down logs test check backup
config:
	docker compose config --quiet
build:
	docker compose build api
up:
	docker compose up -d api worker
# Named volumes are retained. Do not use down -v for normal shutdown.
down:
	docker compose down
logs:
	docker compose logs --tail=100 -f api worker
test:
	docker compose --profile test run --rm test
check:
	python3 scripts/check_repository.py
backup:
	./scripts/backup.sh
