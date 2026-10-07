# yabot.jobs-agent

A [Claude Code](https://github.com/anthropics/claude-code-action) agent on
GitHub Actions that finds the real logo for companies missing one in
[yabot.jobs-backend](https://github.com/davicho01/yabot.jobs-backend).

Each run takes a batch of rows from the backend's
`migrations/data/companies_missing_logos_with_domain.csv` and:

1. finds each company's square brand icon — logo.dev, the company's own
   site icons, Google's favicon service, then web search — and checks it
   visually (Claude); wide wordmarks are rejected;
2. normalizes it to a 128×128 PNG with the backend's own code and uploads it
   to S3 as `logos/<slug>-<sha256[:8]>.png` (`tools/logo_pipeline.py`);
3. writes `logo_key`, `logo_source_url` and `logo_status` (found /
   not_found) into the CSV;
4. adds an Alembic upgrade setting `companies.logo_key` (origin `url`);
5. opens one PR on the backend for review.

A run is skipped while a previous logo PR is still open, so batches never
conflict. Merging deploys the backend and runs the migration; then run
`python -m one_off.backfill_logo_aliases` and invalidate `/logos/c/*` so
static pages pick the logos up.

## Setup

Repository secrets:

| Secret | What |
| --- | --- |
| `CLAUDE_CODE_OAUTH_TOKEN` | from `claude setup-token` |
| `BACKEND_REPO_TOKEN` | fine-grained PAT, *yabot.jobs-backend only*: Contents + Pull requests read/write |
| `AWS_ROLE_ARN` | created by `deploy/aws-setup.sh` (S3 upload to `logos/*` only) |
| `LOGO_DEV_SECRET_KEY` | optional: the backend's logo.dev key, the best first source |

## Running

Actions → **Logo agent** → **Run workflow**, or
`gh workflow run agent.yml -f batch_size=10`. Uncomment `schedule` in the
workflow to run automatically.

## Testing the pipeline locally

```bash
pip install -r requirements.txt   # needs the cairo library (brew install cairo)
python tools/logo_pipeline.py next --csv path/to/companies.csv --limit 3
python tools/logo_pipeline.py fetch --domain insperity.com --via site --out /tmp/logo.png --backend ../yabot.jobs-backend
python tools/logo_pipeline.py store --csv path/to/companies.csv --id <id> --png /tmp/logo.png --source-url <url> --dry-run
```
