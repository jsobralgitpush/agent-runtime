.PHONY: install run worker test lint typecheck check demo migrate

install:
	uv sync --extra dev

run:
	uv run uvicorn app.main:app --reload

worker:
	uv run agent-worker

test:
	uv run pytest --cov=app --cov-report=term-missing

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy app

check: lint typecheck test

demo:
	./scripts/demo.sh

migrate:
	uv run alembic upgrade head
