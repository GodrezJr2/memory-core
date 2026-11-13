# Contributing

Thanks for taking a look. Bug reports, recall misses, docs fixes and pull requests are all welcome.

## How this repo is maintained

memory-core is developed in an upstream repository that also deploys a running instance; this GitHub repo is
published from it. A pull request here is reviewed as usual and, once accepted, is applied upstream and shows up in
the next sync with you credited as the commit's co-author. So don't be surprised if the merge happens as a new
commit rather than through GitHub's merge button.

## Development setup

```bash
pip install -r requirements.txt pytest
python -m pytest tests                  # unit tests, no services needed
cd web && npm ci && npm run dev         # live view at http://localhost:5173/live/?demo
```

For changes that touch the database, recall or MCP tools, run the stack (`docker compose up -d --build`) and the
end-to-end checks: `MC_URL=http://127.0.0.1:8420 MC_TOKEN=<token> python scripts/smoke.py`.

## Pull requests

- Keep one change per PR, and say *why* in the description, not only what.
- Add or update a test in `tests/` for behaviour changes. Ranking changes should come with before/after numbers from
  `scripts/eval_recall.py` on your own question set.
- No new runtime dependency without a good reason: the clients are stdlib-only on purpose, so they run anywhere
  Python does.
- Never commit real tokens, `tokens.json`, `pg_password` or anything under `data/`.

## Reporting a recall miss

The most useful issue is a query that should have found a memory but didn't. Include the query, the memory text
(redacted if needed), what recall returned instead, and the similarity scores if you have them.
