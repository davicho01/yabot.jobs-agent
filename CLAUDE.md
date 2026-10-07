# yabot.jobs-agent

A GitHub Actions agent that finds the real logo (square brand icon) for
yabot.jobs companies missing one and opens a PR on
davicho01/yabot.jobs-backend. GOAL.md is the task; tools/logo_pipeline.py
does the exact parts (fetch + normalize, hash, upload, CSV, Alembic head) —
use it rather than doing those by hand. Never generate or draw a logo.

- The backend is checked out at ./backend; use `git -C backend ...`.
- Keep each PR to one batch: the CSV and one new migration, nothing else.
- Never push to the backend's main branch.
