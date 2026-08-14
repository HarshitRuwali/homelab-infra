# Data Sources

The ingestion pipeline collects from these sources:

## Persona files

Reads markdown files from `~/.hermes/memories/`:

- `MEMORY.md` -- agent memory
- `USER.md` -- user profile
- `MEMORY_CAREER.md` -- career context
- `MEMORY_PROJECTS.md` -- project context

Tagged as `type: persona`, `priority: high`.

## Git history

Scans recent commits (last 10) from configured projects. Tagged as
`type: work_progress`, `priority: medium`.

## Obsidian daily notes

Finds markdown files modified today under `~/obsidian-self/` in folders named
`Daily`, `Journal`, or `Log`. Tagged as `type: daily_notes`, `priority: medium`.

## Project READMEs

Reads `README.md` from active projects. Tagged as `type: project_context`,
`priority: low`.

## Tasks

A placeholder collector. It emits a single marker chunk when `~/.hermes/state.db`
exists, tagged `type: tasks`, `priority: high`, and is the intended hook for a
real task-tracking integration. It collects no actual tasks today.

## Adding a source

Each source is an async function that returns a list of chunk dictionaries:

```python
{
    "text": "...",
    "source_file": "category/file.md",
    "type": "category",
    "tags": ["tag1", "tag2"],
    "priority": "medium",
}
```

Add your function to `ingest_all_data()` in `scripts/ingest_daily_data.py`.
