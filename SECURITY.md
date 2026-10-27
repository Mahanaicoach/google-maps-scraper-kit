# Security policy

This page tells you what to report, where to report it, and what belongs elsewhere.

## What to report

Report any way the kit could expose the engine, leak data or run code it should not. Examples: a
change that binds the API beyond `127.0.0.1`, a script that leaks `SCRAPER_API_KEY`, or a
command in `.claude/settings.json` that grants more than local access.

The kit binds the engine's API to `127.0.0.1` because the engine has no built-in authentication.
Do not expose port 8080 to a network without an authenticating proxy in front of it.

## Where to report

Use GitHub's private
[security advisory form](https://github.com/Mahanaicoach/google-maps-scraper-kit/security/advisories/new).
Do not open a public issue for security problems.

## Out of scope

- Bugs in the engine itself. Report those upstream to
  [gosom/google-maps-scraper](https://github.com/gosom/google-maps-scraper).
- Problems that only occur after you expose the API to a network without an auth proxy.
- Google rate-limiting your IP. That is expected behavior, covered in [SETUP.md](SETUP.md).

## CI/CD Security Best Practices

### Pinned GitHub Actions

**Policy:** All GitHub Actions in workflows MUST be pinned to absolute commit SHAs, not version tags.

**Rationale:** Version tags (e.g., `@v4`, `@v5`) can change to point to different versions over time. If a malicious version is published, using tags could introduce a supply chain attack. Pinning to SHAs ensures the workflow always uses the exact same version of an action.

**Implementation:**
```yaml
# ❌ NOT ALLOWED - tags can change
- uses: actions/checkout@v4

# ✅ REQUIRED - absolute SHA is immutable
- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
```

**How to Pin Actions:**
1. Get the latest release SHA:
   ```bash
   gh api repos/actions/checkout/releases/latest --jq '{tag: .tag_name, sha: .target_commitish}'
   gh api repos/actions/checkout/git/refs/tags/<tag> --jq '.object.sha'
   ```
2. Update workflow with the SHA
3. Add comment with version tag for reference

**Verification:**
- All SHAs must be verified against official GitHub releases
- Use `gh api repos/<owner>/<repo>/git/refs/tags/<tag>` to confirm
- Document the verification in commit message

**Enforcement:**
- This policy is enforced via code review for all workflow changes
- See [ADR-001: Test Architecture and CI/CD](docs/ADR-001-test-cicd.md) for implementation details

### GITHUB_TOKEN Permissions

**Policy:** GITHUB_TOKEN must have minimum required permissions only.

**Rationale:** Reducing permissions limits the blast radius if the CI is compromised.

**Implementation:**
```yaml
permissions:
  contents: read  # Only if workflow needs to read/write repo
  # Do NOT include unnecessary permissions like:
  # - packages: read (unless using package registry)
  # - issues: write (unless creating issues)
  # - pull-requests: write (unless creating PRs)
```

**Current Workflow Permissions:**
- `contents: read` ✅ (minimum for checkout)
- No other permissions ✅

### Secrets Management

**Policy:** No secrets should be stored in the repository or exposed in CI logs.

**Current Status:**
- No secrets in `.env` (only template)
- No secrets in workflows
- No secrets in test code
- GITHUB_TOKEN is automatically managed by GitHub Actions and masked in logs ✅
