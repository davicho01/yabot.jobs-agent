# yabot.jobs-agent

A GitHub Actions agent powered by [Claude Code](https://github.com/anthropics/claude-code-action)
that works toward the goal in `GOAL.md` and opens a pull request for review when it makes changes.

## Setup

1. Add the repo secret `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`).
2. Settings → Actions → General → enable **Allow GitHub Actions to create and approve pull requests**.
3. Edit `GOAL.md`.

## Running

Actions → **Goal agent** → **Run workflow** (optionally enter a one-off goal),
or `gh workflow run agent.yml -f goal="..."`. Enable the `schedule` trigger in
`.github/workflows/agent.yml` to run it automatically.
