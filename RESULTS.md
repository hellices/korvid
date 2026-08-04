# Korvid Evaluation Results

The `eval-results` branch stores generated raw agent-evaluation artifacts
separately from the application source tree.

- Human-readable methodology, scenario catalog, and scoreboard live on the
  development branch under `docs/evals/`.
- Raw JSON, Markdown reports, pull/warm-up logs, and checksums live here.
- Results are append-only by date. Never rewrite an already published run.
- `SHA256SUMS` makes downloaded artifacts independently verifiable.

## Runs

- [`2026-08-04`](results/2026-08-04/) — local-model AKS task smoke, 8GB tier,
  Qwen3-Coder task pack, offline conversation journeys, and live contract-cluster
  journey runs.
- `2026-08-04-r2` — corrected offline conversation matrix using evidence
  fetched during each checkpoint turn only:
  [`archive`](results/2026-08-04-r2-artifacts.tar.gz),
  [`metadata`](results/2026-08-04-r2-metadata.json), and
  [`checksums`](results/2026-08-04-r2-SHA256SUMS).

## Important Validity Note

The initial `live-journeys/qwen3-8b.json` run is retained for debugging but is
invalid for model scoring: a discovery-alias collision selected
`metrics.k8s.io/PodMetrics` instead of core/v1 Pods. The corrected publishable
run is `live-journeys/qwen3-8b-r2.json`; the regression was fixed in source
commit `8e15c52`.
