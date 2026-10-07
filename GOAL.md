# Goal: real logos for companies that are missing one

Work through `backend/migrations/data/companies_missing_logos_with_domain.csv`
(columns `id, company_name, company_domain`, plus `logo_key`,
`logo_source_url`, `logo_status` once handled). `id` is the company's
`companies.id` in the backend database. For a batch of companies not yet
handled: **find the company's real logo** (never design or draw one), store
it, record it in the CSV, and add an Alembic upgrade that sets it on the
company record. Then the workflow opens one pull request for review.

All paths are relative to the workflow's working directory; the backend repo
is at `./backend`. Run git as `git -C backend ...`. `$CSV` below means
`backend/migrations/data/companies_missing_logos_with_domain.csv`.

## What a good logo is here

Logos are shown as a 128×128 square next to a company name, often at
16–56px. So the right image is the brand's **square icon/symbol** — its app
icon, apple-touch-icon, social-media avatar, or the symbol part of its logo
(e.g. Marvell's square "M" mark, Insperity's star mark) — **not** a wide
wordmark, which shrinks to an unreadable strip. The pipeline rejects
anything wider than 1.5:1 or smaller than 64px for that reason.

It must be the right *brand*: base it on the company behind `company_domain`
and the name (ignore leading numeric codes and legal suffixes — "21 Marvell
Asia Pte. Ltd." is Marvell). A subsidiary with its own distinct brand and
logo (e.g. Columbia Gas under nisource.com) gets that brand's logo if you
can find it, otherwise the parent's.

## Steps

1. **Pick the batch.** `python tools/logo_pipeline.py next --csv $CSV --limit <batch size>`
   If it returns `[]`, stop without changes.

2. **Branch.** `git -C backend checkout -b agent/logos-<run id>`

3. **Find each logo.** Try sources in this order, writing each candidate to
   `/tmp/logos/<id>-<n>.png`, until one is right:
   1. `python tools/logo_pipeline.py fetch --domain <domain> --via logo_dev --out ...`
      (logo.dev, the backend's usual source; skipped if no key is configured)
   2. `python tools/logo_pipeline.py fetch --domain <domain> --via site --out ...`
      (the company's homepage icons: manifest, apple-touch-icon, SVG favicon)
   3. `python tools/logo_pipeline.py fetch --url "https://www.google.com/s2/favicons?domain=<domain>&sz=256" --out ...`
   4. Web search (WebSearch/WebFetch) for an official square icon: the
      company's brand/press/media-kit page, its LinkedIn/X/Facebook/GitHub
      avatar, Wikimedia Commons, Wikipedia infobox. Then
      `python tools/logo_pipeline.py fetch --url <direct image URL> --out ...`
      (PNG/JPG/WebP/ICO or SVG). For a subsidiary brand, also try its own
      domain (e.g. columbiagasohio.com).

   `fetch` prints `{"status": "ok", "png": ..., "source_url": ...}` or why
   it was rejected. **Read every "ok" PNG and look at it** before accepting:
   it must be recognizably this company's logo mark, crisp, not a generic
   placeholder/globe, not a hosting provider's or CMS's default icon, not a
   photo, and not cut off.

4. **Record it.**
   - Found: `python tools/logo_pipeline.py store --csv $CSV --id <id> --png <file> --source-url <source_url>`
     (uploads `logos/<slug>-<sha256[:8]>.png` to S3 and fills `logo_key`,
     `logo_source_url`, `logo_status=found`).
   - Nothing acceptable after all sources:
     `python tools/logo_pipeline.py skip --csv $CSV --id <id> --reason "<what you tried>"`
     (`logo_status=not_found`, so it isn't retried). Never fall back to
     generating a logo.

5. **Write the Alembic upgrade** (skip this and step 6's migration if no
   logo was found in the whole batch — commit just the CSV). Get the parent
   with `python tools/logo_pipeline.py heads --versions backend/migrations/versions`
   (must print exactly one revision; otherwise stop and explain) and a new
   id with `python tools/logo_pipeline.py newrev`. Create
   `backend/migrations/versions/<rev>_company_logos_<rev>.py`:

   ```python
   """Logos for companies missing one

   Revision ID: <rev>
   Revises: <head>
   Create Date: <YYYY-MM-DD 00:00:00.000000>

   Each company's own logo, found by the logo agent for rows of
   migrations/data/companies_missing_logos_with_domain.csv (whose logo_key
   and logo_source_url columns record the same values). The files are already
   in the static-pages bucket. Pinned as origin "url", like a logo an admin
   set from a URL, so the logo.dev sync leaves them alone. Only fills
   companies still without a logo; downgrade clears just the ones this set.
   """
   from typing import Sequence, Union

   import sqlalchemy as sa
   from alembic import op
   from sqlalchemy.dialects import postgresql

   revision: str = '<rev>'
   down_revision: Union[str, None] = '<head>'
   branch_labels: Union[str, Sequence[str], None] = None
   depends_on: Union[str, Sequence[str], None] = None

   # companies.id -> (logo_key, logo_source_url)
   LOGOS = {
       "<id>": ("logos/<slug>-<hash>.png", "<source_url>"),
   }

   companies = sa.table(
       "companies",
       sa.column("id", postgresql.UUID(as_uuid=False)),
       sa.column("logo_key", sa.String),
       sa.column("logo_origin", sa.String),
       sa.column("logo_source_url", sa.Text),
   )


   def upgrade() -> None:
       conn = op.get_bind()
       for company_id, (logo_key, source_url) in LOGOS.items():
           conn.execute(
               companies.update()
               .where(companies.c.id == company_id, companies.c.logo_key.is_(None))
               .values(logo_key=logo_key, logo_origin="url", logo_source_url=source_url)
           )


   def downgrade() -> None:
       conn = op.get_bind()
       for company_id, (logo_key, _) in LOGOS.items():
           conn.execute(
               companies.update()
               .where(companies.c.id == company_id, companies.c.logo_key == logo_key)
               .values(logo_key=None, logo_origin=None, logo_source_url=None)
           )
   ```
   Check it with `python -m py_compile <file>`.

6. **Commit, push, and write the PR description.** Only the CSV and the new
   migration may change. `git -C backend add` them, commit
   ("Add logos for N companies"), and `git -C backend push -u origin HEAD`.
   Do **not** open the PR yourself — the workflow opens it from
   `/tmp/pr-body.md`. Write that file (Markdown) with:
   - a first line `# Add logos for N companies` (used as the title);
   - a table of company name, domain, logo `![](https://yabot.jobs/<logo_key>)`
     and where it came from (source URL);
   - a list of companies marked not_found, with what you tried;
   - any logos you're unsure about;
   - this post-merge note: *Merging deploys and runs the migration. Then run
     `python -m one_off.backfill_logo_aliases` and invalidate `/logos/c/*` on
     CloudFront so static job pages show the new logos.*

Never push to `main`, and never change any other backend file.
