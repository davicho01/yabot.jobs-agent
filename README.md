# yabot.jobs-agent

A [Claude Code](https://github.com/anthropics/claude-code-action) agent on
GitHub Actions that creates logos for companies missing one in
[yabot.jobs-backend](https://github.com/davicho01/yabot.jobs-backend).

Each run takes a batch of rows from the backend's
`migrations/data/companies_missing_logos_with_domain.csv` and:

1. designs an original SVG logo per company (Claude);
2. renders a 128×128 PNG and uploads both to S3 as
   `logos/<slug>-<sha256[:8]>.{png,svg}` (`tools/logo_pipeline.py`);
3. writes that key into the CSV's `logo_key` column;
4. adds an Alembic upgrade setting `companies.logo_key` (origin `upload`);
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

## Running

Actions → **Logo agent** → **Run workflow**, or
`gh workflow run agent.yml -f batch_size=10`. Uncomment `schedule` in the
workflow to run automatically.

## Testing the pipeline locally

```bash
pip install -r requirements.txt   # needs the cairo library (brew install cairo)
python tools/logo_pipeline.py next --csv path/to/companies.csv --limit 3
python tools/logo_pipeline.py process --csv path/to/companies.csv --id <id> --svg logo.svg --png-out logo.png --dry-run
```
