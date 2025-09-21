# BookBot Development Makefile

.PHONY: help install install-dev test test-fast lint format type-check clean build docs

help:  ## Show this help message
	@echo "BookBot Development Commands:"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install BookBot for production use
	pip install .

install-dev:  ## Install BookBot with development dependencies
	pip install -e ".[dev]"

test:  ## Run all tests
	pytest -v

test-fast:  ## Run tests excluding slow integration tests
	pytest -v -m "not slow"

test-cov:  ## Run tests with coverage report
	pytest --cov=bookbot --cov-report=html --cov-report=term

lint:  ## Run code linting
	ruff check bookbot tests
	black --check bookbot tests

format:  ## Format code with black and ruff
	black bookbot tests
	ruff check --fix bookbot tests

type-check:  ## Run type checking with mypy
	mypy bookbot

clean:  ## Clean build artifacts and cache
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf htmlcov/
	find . -type d -name __pycache__ -delete
	find . -type f -name "*.pyc" -delete

build:  ## Build distribution packages
	python -m build

package:  ## Build and test packages thoroughly
	python scripts/package.py

binary:  ## Build single-file binary
	python scripts/build_binary.py

completions:  ## Generate shell completion scripts
	mkdir -p completions
	python scripts/completions.py all --output-dir completions

test-install:  ## Test installation from built package
	pip install dist/*.whl --force-reinstall

release-patch:  ## Release new patch version
	python scripts/release.py patch

release-minor:  ## Release new minor version
	python scripts/release.py minor

release-major:  ## Release new major version
	python scripts/release.py major

demo:  ## Run demo with sample data
	@echo "Creating sample audiobook structure..."
	mkdir -p demo_library/
	mkdir -p "demo_library/Brandon Sanderson - The Way of Kings/CD1"
	mkdir -p "demo_library/Brandon Sanderson - The Way of Kings/CD2"
	touch "demo_library/Brandon Sanderson - The Way of Kings/CD1/01 - Prologue.mp3"
	touch "demo_library/Brandon Sanderson - The Way of Kings/CD1/02 - Chapter 1.mp3"
	touch "demo_library/Brandon Sanderson - The Way of Kings/CD2/01 - Chapter 2.mp3"
	mkdir -p "demo_library/Ready Player One"
	touch "demo_library/Ready Player One/Ready Player One.m4b"
	@echo "Demo structure created in demo_library/"
	@echo "Run: bookbot tui demo_library/"

clean-demo:  ## Remove demo files
	rm -rf demo_library/

check-deps:  ## Check for dependency issues
	pip check

security-check:  ## Run security checks (requires safety)
	@if command -v safety >/dev/null 2>&1; then \
		safety check; \
	else \
		echo "Install safety: pip install safety"; \
	fi

pre-commit:  ## Run all pre-commit checks
	$(MAKE) format
	$(MAKE) lint
	$(MAKE) type-check
	$(MAKE) test-fast

dev-setup:  ## Complete development environment setup
	$(MAKE) install-dev
	$(MAKE) demo
	@echo ""
	@echo "Development setup complete!"
	@echo "Try: bookbot tui demo_library/"