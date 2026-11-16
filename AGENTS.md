# Agent entry — Marketplace Card Generator

This is `bouncemonster/marketplace-card-generator`, the standalone `cardgen.py`
pipeline. Read [README.md](README.md), starting with the current continuation section,
then [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/CONFIG.md](docs/CONFIG.md)
for the part being changed. README owns the current next card and acceptance criteria;
this adapter makes that existing context visible when Codex opens the repository.

The Tilda catalog and published image library belong to separate `pm.market` and
`pm-market-cards` repositories. Do not import a store, overwrite their files or push
their histories from this project. Python 3.9+ stdlib is enough for offline entry.

```sh
python -B -m unittest discover -s tests -p test_qa_factuality.py
```

This existing test uses mocked transport; no OpenRouter key or Pillow installation
is needed. Preserve the published no-invented-facts hard-fail policy. The next scoped
result is one complete source-backed card, not another implementation of the guard.
Exact product/label/claims/size and visual acceptance remain separate evidence.

`brief`, `send`, `qa` and `fix` call external models. A README command or existing
budget flag does not authorize new spend or real provider traffic for entry checks.
Keep profile data, keys and generated output out of source commits as documented.
Inspect current origin/branch/local work, claim a bounded file scope, stage intended
paths only and verify the exact remote commit after authorized publication.
