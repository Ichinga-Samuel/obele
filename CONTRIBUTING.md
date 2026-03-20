# Contributing to obele

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/ichinga-samuel/obele.git
cd obele
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest tests/ -v
```

All tests use an in-memory SQLite database and should complete in under 1 second.

## Code Style

- Follow [PEP 8](https://peps.python.org/pep-0008/)
- Use type annotations for all public APIs
- Keep zero external dependencies for the core library

## Pull Requests

1. Fork the repo and create your branch from `main`
2. Add tests for any new functionality
3. Ensure all tests pass
4. Update the `CHANGELOG.md` under an `[Unreleased]` section
5. Open a pull request with a clear description

## Reporting Issues

Open a [GitHub issue](https://github.com/ichinga-samuel/obele/issues) with:
- A minimal reproducible example
- Your Python version and OS
- Expected vs actual behavior

