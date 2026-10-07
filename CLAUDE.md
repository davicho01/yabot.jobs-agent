# yabot.jobs-agent

A GitHub Actions agent that designs logos for yabot.jobs companies that are
missing one and opens a PR on davicho01/yabot.jobs-backend. GOAL.md is the
task; tools/logo_pipeline.py does the exact parts (render, hash, upload,
CSV, Alembic head) — use it rather than doing those by hand.

- The backend is checked out at ./backend; use `git -C backend ...`.
- Keep each PR to one batch: the CSV and one new migration, nothing else.
- Never push to the backend's main branch.
