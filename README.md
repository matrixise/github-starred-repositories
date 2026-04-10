# github-starred-repositories

A CLI tool to sync, analyze, and curate your GitHub starred repositories. It fetches starred repos via the GitHub GraphQL API, stores them in a local SQLite database, downloads their READMEs asynchronously, scores each repo with Claude (1-5), and can export the best ones to an Obsidian vault as lightweight notes.

## Features

- **Sync** starred repositories incrementally or in full via the GitHub GraphQL API
- **Fetch READMEs** asynchronously and concurrently via the GitHub REST API
- **Analyze** repositories with Claude (claude-code-sdk), producing a 1-5 interest score and a one-sentence summary
- **List** repos with filters on language, topic, score, and archived status
- **Export** top-scored repos to Obsidian as Markdown notes with YAML frontmatter

## Requirements

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (package manager)
- [gh CLI](https://cli.github.com/) (optional — used as token fallback if `GITHUB_TOKEN` is not set)
- A GitHub personal access token with `read:user` scope, **or** an active `gh auth login` session
- Claude Code CLI installed and authenticated (for `starred analyze`)

## Installation

```bash
git clone https://github.com/matrixise/github-starred-repositories
cd github-starred-repositories

# Install dependencies
uv sync

# Configure authentication (choose one)
cp .env.example .env
# Edit .env and set GITHUB_TOKEN=ghp_...
# OR just run `gh auth login` and the tool will use gh auth token as fallback
```

## Usage

### `starred sync` — Fetch starred repositories

Fetches your starred repos from GitHub and stores them in `starred.db`. By default, runs incrementally (stops at the most recently seen `starred_at` date). Use `--full` to re-sync everything.

```bash
# Incremental sync (default)
starred sync

# Full refresh — fetch all starred repositories
starred sync --full

# Use a custom database path
starred sync --db /path/to/custom.db
```

### `starred fetch-readme` — Download READMEs

Downloads README files from GitHub for each repository (async, concurrent). Files are saved to `readmes/<owner>/<repo>/README.md`.

```bash
# Fetch READMEs for all repos that don't have one yet
starred fetch-readme

# Limit to 100 repos, 20 concurrent requests
starred fetch-readme --limit 100 --concurrency 20

# Re-fetch already downloaded READMEs
starred fetch-readme --force

# Custom output directory
starred fetch-readme --output-dir /path/to/readmes
```

### `starred analyze` — Score repositories with Claude

Analyzes repositories using Claude (claude-code-sdk). Each repo gets an integer score from 1 to 5 and a one-sentence summary. Repos with a README are analyzed with the full content (first 3000 characters).

```bash
# Analyze 20 repositories (default)
starred analyze

# Analyze up to 50 repositories
starred analyze --limit 50
```

Score guide:

| Score | Meaning |
|-------|---------|
| 1 | Not interesting (abandoned, trivial, superseded) |
| 2 | Low interest |
| 3 | Moderate interest |
| 4 | High interest |
| 5 | Excellent (actively maintained, widely useful, innovative) |

### `starred list` — Browse repositories

Displays repositories in a rich terminal table. Supports multiple filters.

```bash
# List the 50 most recently starred repos (default)
starred list

# Filter by language
starred list --lang Python

# Filter by topic
starred list --topic machine-learning

# Show only repos with score >= 4
starred list --min-score 4

# Show only archived repos
starred list --archived

# Show the description column
starred list --description

# Combine filters, increase limit
starred list --lang Rust --min-score 3 --limit 100
```

### `starred export-obsidian` — Export to Obsidian

Exports high-scoring repositories as Markdown notes to your Obsidian vault. Notes are created under `<vault>/Sources/GitHub Stars/` with the filename format `Owner - Repo.md`.

```bash
# Export repos with score >= 4 (default) to your vault
starred export-obsidian --vault ~/Documents/MyVault

# Export repos with score >= 3
starred export-obsidian --vault ~/Documents/MyVault --min-score 3

# Custom Obsidian tag
starred export-obsidian --vault ~/Documents/MyVault --tag "github/stars"

# Delete notes for repos that no longer meet the criteria
starred export-obsidian --vault ~/Documents/MyVault --prune
```

Each note includes YAML frontmatter (`title`, `url`, `language`, `topics`, `score`, `summary`, `starred_at`, `tags`) and a brief Markdown body with score, stars, last push date, topics, and a link to GitHub.

## Recommended Workflow

```bash
# 1. Sync your starred repos (incremental by default)
starred sync

# 2. Download READMEs for richer analysis
starred fetch-readme --concurrency 20

# 3. Analyze with Claude (run multiple times to cover all repos)
starred analyze --limit 50

# 4. Browse the results
starred list --min-score 4 --description

# 5. Export the best ones to Obsidian
starred export-obsidian --vault ~/Documents/MyVault --min-score 4 --prune
```

Run `starred analyze` repeatedly until all repos with READMEs have been scored. A 5-second delay between requests is applied automatically to respect rate limits.

## Database Schema

The local SQLite database (`starred.db`) contains four tables:

### `repositories`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment primary key |
| `name_with_owner` | TEXT UNIQUE | e.g. `torvalds/linux` |
| `description` | TEXT | Repository description |
| `url` | TEXT | GitHub URL |
| `is_archived` | INTEGER | 0 or 1 |
| `pushed_at` | TEXT | ISO 8601 datetime of last push |
| `starred_at` | TEXT | ISO 8601 datetime when starred |
| `primary_language` | TEXT | Primary programming language |
| `stargazer_count` | INTEGER | Number of GitHub stars |
| `synced_at` | TEXT | ISO 8601 datetime of last sync |
| `readme_path` | TEXT | Local path to downloaded README |

### `topics`

| Column | Type | Description |
|--------|------|-------------|
| `repo_id` | INTEGER FK | References `repositories(id)` |
| `topic_name` | TEXT | Topic label |

### `analysis`

| Column | Type | Description |
|--------|------|-------------|
| `repo_id` | INTEGER FK | References `repositories(id)` |
| `score` | INTEGER | Interest score 1-5 |
| `summary` | TEXT | One-sentence summary from Claude |
| `analyzed_at` | TEXT | ISO 8601 datetime of analysis |

### `meta`

| Column | Type | Description |
|--------|------|-------------|
| `key` | TEXT PK | Key name (e.g. `last_cursor`) |
| `value` | TEXT | Value |

Stores internal state such as the last GraphQL pagination cursor used for incremental sync.
