# ADR-001: Test Architecture and CI/CD

**Status:** Accepted
**Date:** 2026-10-27
**Decision Makers:** Alex (Tech Lead), Diego (QA), Thiago (DevOps), Camila (Security)
**Deciders:** Development Team

## Context

The Google Maps Scraper Kit project has:
- Unit tests in `scripts/test_scrape.py` using `unittest`
- No CI/CD pipeline for automated test execution
- No coverage reporting
- No automated quality gates before merging

The production code (`scripts/scrape.py`) is dependency-free (Python stdlib only), which simplifies testing. However, without CI/CD:
- Bugs can be introduced without detection
- Code quality degrades over time
- New contributors have no automated feedback
- Test coverage is unknown and untracked

## Decision

### 1. Test Framework
- **Keep unittest** for existing tests (maintains compatibility)
- **Add pytest** for advanced features (coverage, fixtures, parametrize)
- **Both frameworks coexist** - pytest can run unittest tests without modification

### 2. Coverage Tooling
- Use `pytest-cov` for coverage reporting
- Target: ≥80% coverage for `scripts/scrape.py`
- Coverage gate in CI: fail if below threshold

### 3. CI/CD Platform
- **GitHub Actions** (project is hosted on GitHub)
- Triggers: push to `main`, pull requests
- Matrix: Python 3.9, 3.10, 3.11, 3.12 (ensure compatibility)
- Execution time target: <3 minutes

### 4. Test Characteristics
- **All tests remain offline** (mock engine, no network, no Docker in CI)
- **No external dependencies** for test execution
- **No secrets or credentials** required

### 5. Pre-commit Hooks (Optional)
- Use `pre-commit` framework
- Hook: run pytest before commit
- Can be skipped with `--no-verify` (not recommended)

### 6. Branch Protection (Recommended)
- Require PR for merge to `main`
- Require 1 approval
- Require CI passing
- Block force push

## Consequences

### Positive
- **Fast feedback**: PRs show test results automatically
- **Measurable quality**: Coverage is tracked and gated
- **Consistency**: CI ensures tests pass on all Python versions
- **Zero additional risk**: Tests are offline, no secrets, no external dependencies
- **Low maintenance**: stdlib-only production code means no dependency updates

### Negative
- **Slight learning curve**: pytest features for contributors unfamiliar with it
- **CI latency**: 1-2 minutes per PR (acceptable for this project size)
- **Coverage target pressure**: May encourage low-quality tests to hit 80% (mitigated by code review)

### Neutral
- **Dual framework support**: unittest + pytest coexist (no migration cost)
- **No Docker in CI**: Tests are offline, so Docker not needed for CI (simpler)

## Alternatives Considered

### Alternative 1: Use tox for Multi-Env Python
**Rejected:** Overkill for this project. Only one Python version needed in production (stdlib). GitHub Actions matrix is sufficient.

### Alternative 2: Use GitLab CI or Jenkins
**Rejected:** Project is on GitHub. GitHub Actions is native, free, and well-integrated.

### Alternative 3: Docker-in-Docker for CI
**Rejected:** Tests are offline and mock the engine. Docker is not needed for test execution. Would add unnecessary complexity.

### Alternative 4: Set Coverage Target at 100%
**Rejected:** Unrealistic for CLI scripts with error handling paths. 80% is a practical target that catches most bugs without being burdensome.

### Alternative 5: Add Integration Tests with Real Engine
**Rejected:** Would require Docker in CI, longer execution time, and potential rate-limiting issues. Unit tests with mocks are sufficient for this project.

## Implementation Plan

1. ✅ Measure baseline coverage (65% with 11 tests)
2. ✅ Add `pytest` and `pytest-cov` to `requirements-dev.txt`
3. ✅ Create GitHub Actions workflow (`.github/workflows/test.yml`)
4. ✅ Create `TESTING.md` documentation
5. ✅ Document this ADR
6. ⏳ (Optional) Configure pre-commit hooks
7. ⏳ (Optional) Configure branch protection (manual step by repo owner)
8. ⏳ Improve coverage to ≥80% (add tests for uncovered functions)

## Compliance and Security Review

### Security (Camila)
- **Status:** ✅ Approved
- **Risks:** None identified
- **Justification:**
  - Tests are offline (no network, no API calls)
  - No secrets or credentials in CI
  - Test data is mock/fictional (no PII)
  - CI does not increase scraping volume

### Compliance
- **Google Maps ToS:** CI does not interact with Google Maps (safe)
- **GDPR/CCPA/CAN-SPAM:** No real PII in tests (safe)
- **Data Processing:** Only mock data (safe)

## Metrics and KPIs

| Metric | Target | Measurement |
|--------|--------|-------------|
| Test Pass Rate | 100% | GitHub Actions logs |
| Coverage | ≥80% | pytest-cov report |
| CI Execution Time | <3 min | GitHub Actions timing |
| Flaky Test Rate | 0% | GitHub Actions re-runs |

## References

- [TESTING.md](../TESTING.md) - Testing guide and instructions
- [pytest Documentation](https://docs.pytest.org/)
- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [unittest Documentation](https://docs.python.org/3/library/unittest.html)
