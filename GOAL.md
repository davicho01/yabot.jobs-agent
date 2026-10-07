# Goal: logos for companies that are missing one

Work through `backend/migrations/data/companies_missing_logos_with_domain.csv`
(columns `id, company_name, company_domain`, plus `logo_key` once a logo
exists). `id` is the company's `companies.id` in the backend database. For a
batch of companies without a `logo_key`: design a logo, store it, record it
in the CSV, and add an Alembic upgrade that sets it on the company record.
Then open one pull request on the backend for review.

All paths below are relative to the workflow's working directory; the
backend repo is at `./backend`. Run git as `git -C backend ...`.

## Steps

1. **Pick the batch.**
   `python tools/logo_pipeline.py next --csv backend/migrations/data/companies_missing_logos_with_domain.csv --limit <batch size>`
   If it returns `[]`, every company has a logo: stop without changes.

2. **Branch.** `git -C backend checkout -b agent/logos-<run id>`

3. **Design each logo** as an SVG in `/tmp/logos/<id>.svg`:
   - Square `viewBox="0 0 128 128"`, `xmlns="http://www.w3.org/2000/svg"`.
   - A clean, professional mark: a rounded-square tile (rx≈20) in a color
     that suits the company, with its initial(s) or a simple geometric
     mark. 1–3 letters max; use `font-family="DejaVu Sans, Arial, sans-serif"`,
     bold, centered, high contrast. Must be legible at 32px.
   - Base it on the *brand*, not the legal entity: for "60 Insperity Services,
     L.P." on insperity.com the brand is Insperity (ignore leading numeric
     codes and legal suffixes). Companies sharing a domain/brand (e.g. the
     Columbia Gas entities on nisource.com) should get a consistent look.
   - Do not reproduce or imitate a real trademarked logo design; make an
     original monogram-style mark.
   - Only plain shapes, paths and text: no `<script>`, `<image>`, `<style>`,
     `<foreignObject>`, event handlers or external references (the pipeline
     rejects them).

4. **Store it.** For each company:
   `python tools/logo_pipeline.py process --csv backend/migrations/data/companies_missing_logos_with_domain.csv --id <id> --svg /tmp/logos/<id>.svg --png-out /tmp/logos/<id>.png`
   This renders the 128×128 PNG, names both files
   `logos/<slug>-<sha256[:8]>.{png,svg}`, uploads them to S3, and writes the
   PNG key into the CSV's `logo_key` column. Look at the PNG (Read it) and
   if it doesn't look right, that company is already recorded — note it in
   the PR rather than re-running.

5. **Write the Alembic upgrade.** Get the parent with
   `python tools/logo_pipeline.py heads --versions backend/migrations/versions`
   (must print exactly one revision; if not, stop and explain in the run
   output) and a new id with `python tools/logo_pipeline.py newrev`. Create
   `backend/migrations/versions/<rev>_generated_company_logos_<rev>.py`
   following the shape of the existing data migrations (e.g.
   `e6b3c9d4f2a7_tenet_sub_brands.py`):

   ```python
   """Generated logos for companies missing one

   Revision ID: <rev>
   Revises: <head>
   Create Date: <YYYY-MM-DD 00:00:00.000000>

   Logos designed by the logo agent for companies in
   migrations/data/companies_missing_logos_with_domain.csv (this batch's rows,
   whose logo_key column records the same keys). The files are already in the
   static-pages bucket. Pinned as origin "upload" so the logo.dev sync leaves
   them alone. Only fills companies still without a logo; downgrade clears
   just the ones this set.
   """
   from typing import Sequence, Union

   import sqlalchemy as sa
   from alembic import op
   from sqlalchemy.dialects import postgresql

   revision: str = '<rev>'
   down_revision: Union[str, None] = '<head>'
   branch_labels: Union[str, Sequence[str], None] = None
   depends_on: Union[str, Sequence[str], None] = None

   # companies.id -> logo_key
   LOGOS = {
       "<id>": "logos/<slug>-<hash>.png",
   }

   companies = sa.table(
       "companies",
       sa.column("id", postgresql.UUID(as_uuid=False)),
       sa.column("logo_key", sa.String),
       sa.column("logo_origin", sa.String),
   )


   def upgrade() -> None:
       conn = op.get_bind()
       for company_id, logo_key in LOGOS.items():
           conn.execute(
               companies.update()
               .where(companies.c.id == company_id, companies.c.logo_key.is_(None))
               .values(logo_key=logo_key, logo_origin="upload")
           )


   def downgrade() -> None:
       conn = op.get_bind()
       for company_id, logo_key in LOGOS.items():
           conn.execute(
               companies.update()
               .where(companies.c.id == company_id, companies.c.logo_key == logo_key)
               .values(logo_key=None, logo_origin=None)
           )
   ```
   Check it with `python -m py_compile <file>`.

6. **Commit and open the PR.** Only two files may change: the CSV and the
   new migration. `git -C backend add` them, commit
   ("Add generated logos for N companies"), `git -C backend push -u origin HEAD`, then
   `gh pr create --repo davicho01/yabot.jobs-backend --base main --head agent/logos-<run id>`
   with a body containing:
   - a table of company name, domain and logo key, each with its public URL
     `https://yabot.jobs/<logo_key>` so the reviewer can see the logo;
   - any logos you think need a second look;
   - this post-merge note: *Merging deploys and runs the migration. Then run
     `python -m one_off.backfill_logo_aliases` and invalidate `/logos/c/*` on
     CloudFront so static job pages show the new logos.*

Never push to `main`, and never change any other backend file.
