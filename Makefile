# Makefile - Convenience wrapper for ./dev
# Primary interface is ./dev, this just provides make aliases

.PHONY: help setup lint test test-integration security ci build clean

help:
	@./dev help

setup:
	@./dev setup

lint:
	@./dev lint

test:
	@./dev test

test-integration:
	@./dev test-integration

security:
	@./dev security

ci:
	@./dev ci

build:
	@./dev build

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache __pycache__ .coverage htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

# Fast native commands (no Docker)
native-lint:
	@./dev native lint

native-test:
	@./dev native test
