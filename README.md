# homelab-infra

Merged home infrastructure repositories. Each project keeps its own README,
docs, and tooling inside its subdirectory.

| Directory | Origin repo | What it is |
| --- | --- | --- |
| `memory/` | `HarshitRuwali/open-memory-stack` | Memory service, API service, and MCP server |
| `s3-backup/` | `HarshitRuwali/s3-backup-automation` | S3/Glacier backup and restore automation |
| `monitoring/` | `HarshitRuwali/monitorting-stack` | Prometheus, Loki, Grafana, and Alloy collector |

## History

The full `master` history of all three repositories was merged in with
`git subtree`, so every original commit, author, and date is preserved and the
original commit SHAs still match the archived upstream repositories. Commits
made before the merge refer to paths as they existed in the standalone repos
(for example `prometheus/` rather than `monitoring/prometheus/`); use
`git log --follow <path>` to trace a file across the merge point.
