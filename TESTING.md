# Testing Guide

This document explains how to run tests locally, how the CI/CD pipeline works, and how to maintain test quality.

## Running Tests Locally

### Prerequisites
- Python 3.9+
- Development dependencies (see [requirements-dev.txt](requirements-dev.txt))

### Install Development Dependencies

```bash
pip install -r requirements-dev.txt
```

Or install manually:

```bash
pip install pytest pytest-cov
```

### Run All Tests

**Using unittest (existing method):**
```bash
python scripts/test_scrape.py
```

**Using pytest (recommended):**
```bash
pytest scripts/test_scrape.py -v
```

### Run with Coverage

```bash
pytest scripts/test_scrape.py --cov=scrape --cov-report=term-missing
```

Expected output:
```
================ coverage: platform linux, python 3.11 =================
Name              Stmts   Miss  Cover   Missing
-------------------------------------------------
scrape.py           319    112    65%   38-41, 58, 68-80, ...
-------------------------------------------------
TOTAL                319    112    65%
============================ 11 passed in 0.80s ============================
```

**Coverage Target:** ≥80% for `scripts/scrape.py`

## Test Structure

### Current Test Suite
- **File:** `scripts/test_scrape.py`
- **Framework:** unittest (compatible with pytest)
- **Test Classes:**
  - `GridCells`: Tests geographic tile calculation
  - `Dedupe`: Tests deduplication logic (place_id, cid, link)
  - `InBbox`: Tests geographic filtering with margin
  - `ExtractEmails`: Tests email regex extraction
  - `ContactLinks`: Tests contact page discovery
  - `EndToEnd`: Tests full flow with mocked engine

### Test Characteristics
- **Offline:** All tests use mocks (no network, no Docker required)
- **Fast:** 11 tests run in <1 second
- **Isolated:** No external dependencies

## CI/CD Pipeline

### GitHub Actions Workflow

**File:** `.github/workflows/test.yml`

**Triggers:**
- Push to `main` branch
- Pull requests to `main` branch

**Matrix:**
- Python versions: 3.9, 3.10, 3.11, 3.12

**Steps:**
1. Checkout code
2. Set up Python
3. Install development dependencies
4. Run pytest with coverage
5. Check coverage threshold (≥80%)
6. Upload coverage artifact (Python 3.11 only)

**Execution Time:** ~1-2 minutes per run

### Branch Protection (Recommended Configuration)

To enable branch protection for `main`:

1. Go to repository Settings → Branches
2. Add rule for `main` branch:
   - ✅ Require pull request before merging
   - ✅ Require approvals (1)
   - ✅ Require status checks to pass before merging
   - ✅ Require branches to be up to date before merging
   - ✅ Block force pushes

**Required status checks:**
- `test (python-version: 3.9)`
- `test (python-version: 3.10)`
- `test (python-version: 3.11)`
- `test (python-version: 3.12)`

## Pre-commit Hooks (Optional)

Install pre-commit to run tests automatically before commits:

```bash
pip install pre-commit
pre-commit install
```

**File:** `.pre-commit-config.yaml`

```yaml
repos:
  - repo: local
    hooks:
      - id: pytest
        name: Run pytest
        entry: pytest scripts/test_scrape.py
        language: system
        pass_filenames: false
        always_run: true
```

## Coverage Analysis

### Current Coverage (After Enhancement)
- **Date:** 2026-10-27
- **Coverage:** 81% (319 statements, 62 missed)
- **Test Count:** 27 tests (16 new tests added)
- **Execution Time:** 0.85s
- **Target:** ≥80% ✅ **MET**

### Coverage History
- **Baseline:** 65% (11 tests)
- **After Enhancement:** 81% (27 tests)
- **Improvement:** +16 percentage points (+16 tests)

### Uncovered Areas (Remaining)
- Lines 38-41: Environment loading (`load_env_file()`)
- Lines 132-133, 136, 151: Job creation and polling error handling
- Lines 163-164: Keywords file parsing edge cases
- Lines 187-197, 202: HTML fetching error handling
- Line 212: Social link regex edge case
- Lines 328, 332, 334, 343, 347-354: CLI argument validation (in `main()`)
- Lines 370-371: Health check error handling
- Lines 377-383, 401: Job execution and grid logic error cases
- Lines 407, 409: Field selection logic
- Lines 416-421, 425-431, 437-438, 451: Output writing logic

### Target: ≥80% Coverage ✅ ACHIEVED

The 80% target has been met. Remaining uncovered areas are primarily:
- CLI argument validation (requires testing `main()` with different argv)
- Error handling paths (network failures, timeouts)
- Output writing logic (CSV/JSON export)
- Environment loading edge cases

## Troubleshooting

### Tests Fail Locally But Pass in CI
- Check Python version mismatch (CI runs 3.9-3.12)
- Ensure no local environment differences
- Check for missing development dependencies

### Coverage Below 80%
- Run `pytest --cov=scrape --cov-report=term-missing` to see uncovered lines
- Add tests for uncovered functions
- Consider if uncovered code is dead code (remove if so)

### CI is Slow
- All Python versions must pass (cannot reduce matrix without losing compatibility)
- Consider caching pip dependencies (not needed for stdlib-only production code)

### Pre-commit Hook Fails
- Run tests manually first: `pytest scripts/test_scrape.py`
- Fix failing tests before committing
- Temporarily skip with `git commit --no-verify` (not recommended)

## Adding New Tests

When adding new functionality to `scrape.py`:

1. Write test FIRST (TDD approach)
2. Test should be offline (mock external dependencies)
3. Add test to appropriate class in `test_scrape.py`
4. Run coverage to verify improvement
5. Update this document if coverage target changes

## Compliance and Security

### Test Data
- All test data is mock/fictional (no real PII)
- Email addresses are generic (e.g., `owner@gmail.com`, `info@acmeplumbing.com`)
- Domains are generic (e.g., `acmeplumbing.com`, `biz.net`)

### CI/CD Security
- No secrets or credentials required
- No external API calls in tests
- No deployment operations
- Only executes offline tests

### Compliance Notes
- Tests do not increase scraping volume
- Tests do not process real user data
- CI/CD pipeline is ToS-compliant (no Google Maps API calls)

## References

- [ADR-001: Test Architecture and CI/CD](docs/ADR-001-test-cicd.md)
- [pytest Documentation](https://docs.pytest.org/)
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
