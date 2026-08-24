# Service github-stars : plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** transformer la CLI `starred` en service conteneurisé qui synchronise et analyse les dépôts étoilés quatre fois par jour, et expose une API HTTP de lecture consommable par `mgx-orchestrator`.

**Architecture :** une seule image Docker, deux conteneurs. `api` sert FastAPI en lecture seule sur la base SQLite ; `worker` exécute la CLI existante via `supercronic`. La base passe en mode WAL pour supporter ces deux processus. L'analyse abandonne `claude-agent-sdk` pour le SDK `anthropic`, et son prompt quitte le code pour une table versionnée.

**Tech Stack :** Python 3.11+, `uv`, SQLite, `click`, `httpx`, FastAPI, `uvicorn`, `anthropic`, `tenacity`, pytest, ruff, ty, Docker, supercronic.

**Périmètre :** ce plan couvre le seul dépôt `github-starred-repositories`. Le flow `github_stars_digest` de `mgx-orchestrator` fait l'objet d'un plan distinct dans son propre dépôt, écrit une fois l'API en place : il a besoin du contrat réel pour être testable.

**Spécification de référence :** `docs/superpowers/specs/2026-08-24-github-stars-service-design.md`

## Global Constraints

- Python 3.11 minimum (`requires-python = ">=3.11"`), cible ruff `py311`
- ruff : longueur de ligne 100, règles `E,F,I,UP,B,SIM`, `E501` ignoré
- Pas de docstring qui paraphrase la signature ; seulement pour un comportement non évident
- Toute écriture SQLite passe par `INSERT ... ON CONFLICT DO UPDATE`, jamais par un SELECT suivi d'un INSERT ou d'un UPDATE
- Les migrations de schéma s'appliquent dans `_migrate()` de `starred/db.py`, testées par `PRAGMA table_info`
- Le score est borné par `max(1, min(5, ...))` après lecture de la réponse du modèle
- Modèle utilisé : `claude-haiku-4-5-20251001`
- Fuseau du conteneur worker : `Europe/Brussels`
- Aucun secret dans le dépôt : `.env` est ignoré, `.env.example` ne contient que des valeurs factices
- Après chaque tâche : `uv run pytest tests/ -v`, `uv run ruff check --fix .`, `uv run ruff format .`, `uv run ty check`

## Structure des fichiers

| Fichier | Responsabilité | Tâche |
|---|---|---|
| `starred/paths.py` | création : résolution des chemins base et READMEs depuis l'environnement | 1 |
| `starred/db.py` | modification : WAL, table `prompts`, colonne `prompt_id`, requêtes de lecture pour l'API | 1, 3, 6 |
| `starred/client.py` | modification : `GITHUB_TOKEN` devient la seule source du jeton | 2 |
| `starred/default_prompt.py` | création : texte du prompt historique, utilisé pour l'amorçage de la table | 3 |
| `starred/analyze.py` | modification : SDK `anthropic`, prompt reçu en paramètre | 4 |
| `starred/cli.py` | modification : chemins par environnement, groupe `prompt`, ré-analyse | 1, 5 |
| `starred/api.py` | création : FastAPI, `/health` et `/repos` | 6 |
| `Dockerfile` | création : image unique pour les deux conteneurs | 7 |
| `docker-compose.yml` | création : services `api` et `worker`, volumes, réseau externe | 7 |
| `crontab` | création : planification supercronic | 7 |

---

### Task 1 : chemins configurables et mode WAL

Deux conteneurs partagent le fichier SQLite. Sans WAL, chaque écriture du worker bloque
les lectures de l'API. Et les chemins relatifs actuels (`starred.db`, `readmes/`)
dépendent du répertoire de travail, ce qui ne survit pas à la conteneurisation.

**Files:**
- Create: `starred/paths.py`
- Modify: `starred/db.py` (constante `DEFAULT_DB`, fonction `open_db`)
- Modify: `starred/cli.py:38-39` (constantes `DB_PATH` et `README_DIR`)
- Test: `tests/test_paths.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: rien
- Produces: `starred.paths.default_db_path() -> Path`, `starred.paths.default_readme_dir() -> Path`

- [ ] **Step 1 : écrire les tests qui échouent**

Créer `tests/test_paths.py` :

```python
from pathlib import Path

from starred.paths import default_db_path, default_readme_dir


def test_default_db_path_falls_back_to_cwd(monkeypatch):
    monkeypatch.delenv("STARRED_DB", raising=False)
    assert default_db_path() == Path("starred.db")


def test_default_db_path_reads_environment(monkeypatch):
    monkeypatch.setenv("STARRED_DB", "/data/starred.db")
    assert default_db_path() == Path("/data/starred.db")


def test_default_readme_dir_falls_back_to_cwd(monkeypatch):
    monkeypatch.delenv("STARRED_README_DIR", raising=False)
    assert default_readme_dir() == Path("readmes")


def test_default_readme_dir_reads_environment(monkeypatch):
    monkeypatch.setenv("STARRED_README_DIR", "/data/readmes")
    assert default_readme_dir() == Path("/data/readmes")
```

Ajouter à la fin de `tests/test_db.py` :

```python
def test_open_db_enables_wal(tmp_db):
    mode = tmp_db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
```

- [ ] **Step 2 : vérifier l'échec**

Run: `uv run pytest tests/test_paths.py tests/test_db.py::test_open_db_enables_wal -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'starred.paths'` pour les quatre
premiers tests, et `assert 'delete' == 'wal'` pour le cinquième.

- [ ] **Step 3 : créer `starred/paths.py`**

```python
import os
from pathlib import Path


def default_db_path() -> Path:
    return Path(os.environ.get("STARRED_DB") or "starred.db")


def default_readme_dir() -> Path:
    return Path(os.environ.get("STARRED_README_DIR") or "readmes")
```

- [ ] **Step 4 : activer WAL dans `open_db`**

Dans `starred/db.py`, remplacer l'import et la constante :

```python
from .paths import default_db_path
```

Supprimer la ligne `DEFAULT_DB = Path("starred.db")` et changer la signature de
`open_db` ainsi que le corps, juste après `conn.row_factory` :

```python
@contextmanager
def open_db(path: Path | None = None):
    conn = sqlite3.connect(path if path is not None else default_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
```

Le défaut devient `None` plutôt que `DEFAULT_DB` : une valeur par défaut évaluée à
l'import figerait le chemin avant que l'environnement du conteneur soit lu.

- [ ] **Step 5 : brancher la CLI sur les mêmes fonctions**

Dans `starred/cli.py`, remplacer les deux constantes :

```python
from .paths import default_db_path, default_readme_dir

DB_PATH = default_db_path()
README_DIR = default_readme_dir()
```

`load_dotenv()` est déjà appelé au-dessus, donc `.env` est lu avant la résolution.

- [ ] **Step 6 : vérifier le succès**

Run: `uv run pytest tests/ -v`
Expected: PASS, toute la suite existante comprise.

- [ ] **Step 7 : commit**

```bash
git add starred/paths.py starred/db.py starred/cli.py tests/test_paths.py tests/test_db.py
git commit -m "feat(db): chemins configurables par environnement et mode WAL"
```

---

### Task 2 : GITHUB_TOKEN devient la seule source du jeton

Le repli sur `gh auth token` ne peut pas fonctionner dans l'image : le CLI `gh` n'y est
pas installé. Le garder produirait un `FileNotFoundError` intercepté suivi d'un message
d'erreur trompeur qui suggère `gh auth login`.

**Files:**
- Modify: `starred/client.py:41-57` (`_get_token`)
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: rien
- Produces: `starred.client._get_token() -> str`, signature inchangée, comportement restreint

- [ ] **Step 1 : écrire les tests qui échouent**

Ajouter à `tests/test_client.py` :

```python
import pytest

from starred.client import _get_token


def test_get_token_reads_environment(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
    assert _get_token() == "ghp_fake"


def test_get_token_raises_without_environment(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        _get_token()


def test_get_token_never_shells_out(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    def fail(*args, **kwargs):
        raise AssertionError("subprocess must not be called")

    monkeypatch.setattr("subprocess.run", fail)
    with pytest.raises(RuntimeError):
        _get_token()
```

- [ ] **Step 2 : vérifier l'échec**

Run: `uv run pytest tests/test_client.py -v`
Expected: FAIL sur `test_get_token_never_shells_out`, `AssertionError: subprocess must
not be called`.

- [ ] **Step 3 : simplifier `_get_token`**

Remplacer entièrement la fonction dans `starred/client.py`, et retirer l'import
`subprocess` devenu inutile :

```python
def _get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set.")
    return token
```

- [ ] **Step 4 : vérifier le succès**

Run: `uv run pytest tests/ -v && uv run ruff check .`
Expected: PASS, et aucun avertissement d'import inutilisé.

- [ ] **Step 5 : mettre à jour la documentation**

Dans `.env.example`, remplacer les deux lignes de commentaire du jeton par :

```
# GitHub personal access token (required scopes: read:user)
GITHUB_TOKEN=ghp_your_token_here
```

Dans `README.md`, retirer la ligne mentionnant `gh CLI` comme repli dans la section
« Requirements », et le membre de phrase « **or** an active `gh auth login` session ».

- [ ] **Step 6 : commit**

```bash
git add starred/client.py tests/test_client.py .env.example README.md
git commit -m "feat(client): GITHUB_TOKEN obligatoire, retrait du repli gh"
```

---

### Task 3 : table `prompts` versionnée

Le prompt quitte le code pour la base, avec une seule version active à la fois et une
traçabilité par analyse. L'index unique partiel fait respecter l'unicité de la version
active par le moteur, sans code applicatif de vérification.

**Files:**
- Create: `starred/default_prompt.py`
- Modify: `starred/db.py` (constante `SCHEMA`, fonction `_migrate`, ajout de cinq fonctions)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `starred.db.open_db`
- Produces:
  - `starred.db.Prompt` : dataclass gelée `(id: int, name: str, system_prompt: str, template: str)`
  - `starred.db.get_active_prompt(conn) -> Prompt`
  - `starred.db.list_prompts(conn) -> list[sqlite3.Row]`
  - `starred.db.add_prompt(conn, name: str, system_prompt: str, template: str, activate: bool = False) -> int`
  - `starred.db.activate_prompt(conn, prompt_id: int) -> None`
  - `starred.db.upsert_analysis(conn, repo_id: int, score: int, summary: str, prompt_id: int) -> None`
  - `starred.db.get_repos_analyzed_below_prompt(conn, prompt_id: int, limit: int) -> list[sqlite3.Row]`
  - `starred.default_prompt.LEGACY_SYSTEM_PROMPT`, `starred.default_prompt.LEGACY_TEMPLATE`

- [ ] **Step 1 : écrire les tests qui échouent**

Ajouter à `tests/test_db.py` :

```python
import pytest

from starred.db import (
    activate_prompt,
    add_prompt,
    get_active_prompt,
    get_repos_analyzed_below_prompt,
    list_prompts,
    upsert_analysis,
    upsert_repo,
)


def test_migration_seeds_the_legacy_prompt(tmp_db):
    prompt = get_active_prompt(tmp_db)
    assert prompt.name == "legacy"
    assert "senior software developer" in prompt.system_prompt


def test_only_one_prompt_can_be_active(tmp_db):
    add_prompt(tmp_db, "v2", "sys", "tpl", activate=True)
    active = [p for p in list_prompts(tmp_db) if p["is_active"]]
    assert len(active) == 1
    assert active[0]["name"] == "v2"


def test_added_prompt_is_inactive_by_default(tmp_db):
    add_prompt(tmp_db, "v2", "sys", "tpl")
    assert get_active_prompt(tmp_db).name == "legacy"


def test_activate_prompt_switches_the_active_version(tmp_db):
    new_id = add_prompt(tmp_db, "v2", "sys", "tpl")
    activate_prompt(tmp_db, new_id)
    assert get_active_prompt(tmp_db).id == new_id


def test_upsert_analysis_records_the_prompt_version(tmp_db, sample_repo):
    repo_id = upsert_repo(tmp_db, sample_repo)
    prompt_id = get_active_prompt(tmp_db).id
    upsert_analysis(tmp_db, repo_id, 4, "Useful", prompt_id)
    row = tmp_db.execute("SELECT prompt_id FROM analysis WHERE repo_id = ?", (repo_id,)).fetchone()
    assert row["prompt_id"] == prompt_id


def test_get_repos_analyzed_below_prompt_selects_stale_analyses(tmp_db, sample_repo):
    repo_id = upsert_repo(tmp_db, sample_repo)
    old_id = get_active_prompt(tmp_db).id
    upsert_analysis(tmp_db, repo_id, 4, "Useful", old_id)
    new_id = add_prompt(tmp_db, "v2", "sys", "tpl", activate=True)

    stale = get_repos_analyzed_below_prompt(tmp_db, new_id, limit=10)
    assert [r["name_with_owner"] for r in stale] == ["octocat/hello-world"]

    upsert_analysis(tmp_db, repo_id, 5, "Better", new_id)
    assert get_repos_analyzed_below_prompt(tmp_db, new_id, limit=10) == []
```

- [ ] **Step 2 : vérifier l'échec**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL, `ImportError: cannot import name 'activate_prompt' from 'starred.db'`.

- [ ] **Step 3 : extraire le prompt historique**

Créer `starred/default_prompt.py` en y déplaçant tel quel le contenu des constantes
`SYSTEM_PROMPT` et `PROMPT_TEMPLATE` de `starred/analyze.py`, renommées :

```python
LEGACY_SYSTEM_PROMPT = (
    "You are a senior software developer evaluating GitHub repositories. "
    "You respond ONLY with valid JSON, no markdown, no extra text."
)

LEGACY_TEMPLATE = """\
Rate the interest of this GitHub repository for an active software developer.

Repository: {name}
Description: {description}
Language: {language}
Topics: {topics}
Last push: {pushed_at}
Stars: {stars}
Archived: {archived}
{readme_section}
Respond with ONLY a JSON object (no markdown):
{{"score": <integer 1-5>, "summary": "<one sentence in English>"}}

Score guide:
1 = Not interesting (abandoned, trivial, superseded)
2 = Low interest
3 = Moderate interest
4 = High interest
5 = Excellent (actively maintained, widely useful, innovative)
"""
```

Ne pas encore toucher à `analyze.py` : c'est la tâche 4.

- [ ] **Step 4 : étendre le schéma**

Dans `starred/db.py`, ajouter à la fin de la constante `SCHEMA` :

```sql
CREATE TABLE IF NOT EXISTS prompts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    system_prompt TEXT    NOT NULL,
    template      TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_prompt
    ON prompts(is_active) WHERE is_active = 1;
```

- [ ] **Step 5 : compléter `_migrate`**

```python
def _migrate(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(repositories)")}
    if "readme_path" not in columns:
        conn.execute("ALTER TABLE repositories ADD COLUMN readme_path TEXT")

    analysis_columns = {row[1] for row in conn.execute("PRAGMA table_info(analysis)")}
    if "prompt_id" not in analysis_columns:
        conn.execute("ALTER TABLE analysis ADD COLUMN prompt_id INTEGER REFERENCES prompts(id)")

    seeded = conn.execute("SELECT id FROM prompts LIMIT 1").fetchone()
    if seeded is None:
        prompt_id = _insert_prompt(
            conn, "legacy", LEGACY_SYSTEM_PROMPT, LEGACY_TEMPLATE, activate=True
        )
        # Les analyses antérieures à la table `prompts` ont bien été produites par
        # ce texte : les y rattacher évite de les faire passer pour périmées à la
        # première ré-analyse ciblée.
        conn.execute("UPDATE analysis SET prompt_id = ? WHERE prompt_id IS NULL", (prompt_id,))
```

Importer les deux constantes en tête de `db.py` :

```python
from .default_prompt import LEGACY_SYSTEM_PROMPT, LEGACY_TEMPLATE
```

- [ ] **Step 6 : ajouter les fonctions de lecture et d'écriture**

```python
@dataclass(frozen=True)
class Prompt:
    id: int
    name: str
    system_prompt: str
    template: str


def _insert_prompt(
    conn: sqlite3.Connection, name: str, system_prompt: str, template: str, activate: bool
) -> int:
    now = datetime.now(UTC).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO prompts (name, system_prompt, template, is_active, created_at)
        VALUES (?, ?, ?, 0, ?)
        """,
        (name, system_prompt, template, now),
    )
    prompt_id = int(cursor.lastrowid)
    if activate:
        activate_prompt(conn, prompt_id)
    return prompt_id


def add_prompt(
    conn: sqlite3.Connection,
    name: str,
    system_prompt: str,
    template: str,
    activate: bool = False,
) -> int:
    return _insert_prompt(conn, name, system_prompt, template, activate)


def activate_prompt(conn: sqlite3.Connection, prompt_id: int) -> None:
    # Deux instructions plutôt qu'une : l'index unique partiel refuserait un
    # état transitoire à deux versions actives, donc la désactivation précède.
    conn.execute("UPDATE prompts SET is_active = 0 WHERE is_active = 1")
    conn.execute("UPDATE prompts SET is_active = 1 WHERE id = ?", (prompt_id,))


def get_active_prompt(conn: sqlite3.Connection) -> Prompt:
    row = conn.execute(
        "SELECT id, name, system_prompt, template FROM prompts WHERE is_active = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("No active prompt. Run `starred prompt activate <id>`.")
    return Prompt(
        id=row["id"],
        name=row["name"],
        system_prompt=row["system_prompt"],
        template=row["template"],
    )


def list_prompts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, name, is_active, created_at FROM prompts ORDER BY id"
    ).fetchall()


def get_repos_analyzed_below_prompt(
    conn: sqlite3.Connection, prompt_id: int, limit: int
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT r.id, r.name_with_owner, r.description, r.primary_language,
               r.is_archived, r.pushed_at, r.stargazer_count, r.readme_path,
               GROUP_CONCAT(t.topic_name, ', ') AS topics
        FROM repositories r
        JOIN analysis a ON a.repo_id = r.id
        LEFT JOIN topics t ON t.repo_id = r.id
        WHERE a.prompt_id IS NULL OR a.prompt_id != ?
        GROUP BY r.id
        ORDER BY r.starred_at DESC
        LIMIT ?
        """,
        (prompt_id, limit),
    ).fetchall()
```

Ajouter `from dataclasses import dataclass` aux imports.

- [ ] **Step 7 : faire porter `prompt_id` par `upsert_analysis`**

```python
def upsert_analysis(
    conn: sqlite3.Connection, repo_id: int, score: int, summary: str, prompt_id: int
) -> None:
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO analysis (repo_id, score, summary, analyzed_at, prompt_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(repo_id) DO UPDATE SET
            score       = excluded.score,
            summary     = excluded.summary,
            analyzed_at = excluded.analyzed_at,
            prompt_id   = excluded.prompt_id
        """,
        (repo_id, score, summary, now, prompt_id),
    )
```

- [ ] **Step 8 : vérifier le succès**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS. Les tests existants de `test_db.py` appelant `upsert_analysis` avec
quatre arguments échoueront : leur ajouter `get_active_prompt(tmp_db).id` en cinquième
argument.

- [ ] **Step 9 : commit**

```bash
git add starred/db.py starred/default_prompt.py tests/test_db.py
git commit -m "feat(db): table prompts versionnée et tracabilité par analyse"
```

---

### Task 4 : analyse par le SDK anthropic

`claude-agent-sdk` lance le CLI Claude Code en sous-processus. Dans un conteneur, cela
supposerait d'embarquer Node et le CLI, puis de monter un répertoire d'authentification
dont le jeton expire sans moyen de le renouveler sans interaction.

**Files:**
- Modify: `pyproject.toml` (dépendances)
- Modify: `starred/analyze.py` (réécriture complète)
- Test: `tests/test_analyze.py`

**Interfaces:**
- Consumes: `starred.db.Prompt`
- Produces: `starred.analyze.analyze_repo(row: sqlite3.Row, prompt: Prompt) -> tuple[int, int, str]`, renvoyant `(repo_id, score, summary)`

- [ ] **Step 1 : échanger les dépendances**

```bash
uv remove claude-agent-sdk
uv add anthropic
```

- [ ] **Step 2 : écrire les tests qui échouent**

Remplacer le contenu de `tests/test_analyze.py` par :

```python
import pytest

from starred.analyze import _build_prompt, _extract_json, analyze_repo
from starred.db import Prompt

PROMPT = Prompt(
    id=1,
    name="test",
    system_prompt="You respond only with JSON.",
    template="Repo: {name} / {description} / {language} / {topics} / "
    "{pushed_at} / {stars} / {archived}\n{readme_section}",
)


class FakeMessage:
    def __init__(self, text: str):
        self.content = [type("Block", (), {"text": text})()]


class FakeMessages:
    def __init__(self, text: str):
        self._text = text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeMessage(self._text)


class FakeClient:
    def __init__(self, text: str):
        self.messages = FakeMessages(text)


def test_build_prompt_uses_the_supplied_template(sample_row_dict):
    rendered = _build_prompt(sample_row_dict, PROMPT)
    assert "octocat/hello-world" in rendered
    assert "Python" in rendered


def test_build_prompt_inlines_the_readme(sample_row_dict, tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Hello", encoding="utf-8")
    sample_row_dict["readme_path"] = str(readme)
    assert "# Hello" in _build_prompt(sample_row_dict, PROMPT)


def test_build_prompt_survives_a_missing_readme_file(sample_row_dict):
    sample_row_dict["readme_path"] = "/nonexistent/README.md"
    assert "octocat/hello-world" in _build_prompt(sample_row_dict, PROMPT)


def test_extract_json_strips_code_fences():
    assert _extract_json('```json\n{"score": 4}\n```') == {"score": 4}


def test_analyze_repo_returns_score_and_summary(sample_row_dict, monkeypatch):
    client = FakeClient('{"score": 4, "summary": "Useful."}')
    monkeypatch.setattr("starred.analyze._client", lambda: client)
    assert analyze_repo(sample_row_dict, PROMPT) == (1, 4, "Useful.")


def test_analyze_repo_passes_the_system_prompt(sample_row_dict, monkeypatch):
    client = FakeClient('{"score": 3, "summary": "Fine."}')
    monkeypatch.setattr("starred.analyze._client", lambda: client)
    analyze_repo(sample_row_dict, PROMPT)
    assert client.messages.calls[0]["system"] == PROMPT.system_prompt


def test_analyze_repo_clamps_an_out_of_range_score(sample_row_dict, monkeypatch):
    client = FakeClient('{"score": 9, "summary": "Too high."}')
    monkeypatch.setattr("starred.analyze._client", lambda: client)
    assert analyze_repo(sample_row_dict, PROMPT)[1] == 5


def test_analyze_repo_rejects_a_malformed_response(sample_row_dict, monkeypatch):
    client = FakeClient('{"summary": "no score"}')
    monkeypatch.setattr("starred.analyze._client", lambda: client)
    with pytest.raises(ValueError, match="Unexpected"):
        analyze_repo(sample_row_dict, PROMPT)
```

- [ ] **Step 3 : vérifier l'échec**

Run: `uv run pytest tests/test_analyze.py -v`
Expected: FAIL, `TypeError: _build_prompt() takes 1 positional argument but 2 were given`.

- [ ] **Step 4 : réécrire `starred/analyze.py`**

```python
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import anthropic
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .db import Prompt

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 512
README_MAX_CHARS = 3000


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def _build_prompt(row: sqlite3.Row, prompt: Prompt) -> str:
    pushed = row["pushed_at"][:10] if row["pushed_at"] else "unknown"

    readme_section = ""
    readme_path = row["readme_path"]
    if readme_path and Path(readme_path).exists():
        content = Path(readme_path).read_text(encoding="utf-8", errors="replace")
        if len(content) > README_MAX_CHARS:
            content = content[:README_MAX_CHARS] + "\n...(truncated)"
        readme_section = f"\nREADME (excerpt):\n{content}\n"

    return prompt.template.format(
        name=row["name_with_owner"],
        description=row["description"] or "(no description)",
        language=row["primary_language"] or "unknown",
        topics=row["topics"] or "(none)",
        pushed_at=pushed,
        stars=row["stargazer_count"],
        archived="yes" if row["is_archived"] else "no",
        readme_section=readme_section,
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"```(?:json)?\s*", "", text).strip()
    return json.loads(text)


@retry(
    retry=retry_if_exception_type(anthropic.RateLimitError),
    wait=wait_exponential(multiplier=2, min=30, max=120),
    stop=stop_after_attempt(4),
    reraise=True,
)
def analyze_repo(row: sqlite3.Row, prompt: Prompt) -> tuple[int, int, str]:
    message = _client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=prompt.system_prompt,
        messages=[{"role": "user", "content": _build_prompt(row, prompt)}],
    )
    raw = message.content[0].text  # ty: ignore[unresolved-attribute]
    data = _extract_json(raw)
    if not isinstance(data.get("score"), int | float) or "summary" not in data:
        raise ValueError(f"Unexpected Claude response format: {raw!r}")
    score = max(1, min(5, int(data["score"])))
    return row["id"], score, str(data["summary"])
```

La clé provient de `ANTHROPIC_API_KEY`, lue par le constructeur du client. `asyncio` et
`ClaudeAgentOptions` disparaissent : l'appel est synchrone de bout en bout.

- [ ] **Step 5 : adapter l'appelant**

Dans `starred/cli.py`, commande `analyze`, importer `get_active_prompt` depuis `.db` et
remplacer les lignes de la boucle :

```python
        prompt = get_active_prompt(conn)
        ...
                repo_id, score, summary = analyze_repo(row, prompt)
                upsert_analysis(conn, repo_id, score, summary, prompt.id)
```

`prompt` se lit une seule fois, avant la boucle, juste après le calcul de `rows`.

- [ ] **Step 6 : vérifier le succès**

Run: `uv run pytest tests/ -v && uv run ruff check . && uv run ty check`
Expected: PASS.

- [ ] **Step 7 : commit**

```bash
git add pyproject.toml uv.lock starred/analyze.py starred/cli.py tests/test_analyze.py
git commit -m "feat(analyze): passage au SDK anthropic et prompt depuis la base"
```

---

### Task 5 : commandes CLI de gestion du prompt

Garder l'écriture en ligne de commande laisse l'API sans aucun endpoint d'écriture, ce
qui rend l'absence d'authentification tenable.

**Files:**
- Modify: `starred/cli.py` (nouveau groupe `prompt`, option de ré-analyse)
- Test: `tests/test_cli_prompt.py`

**Interfaces:**
- Consumes: `starred.db.add_prompt`, `activate_prompt`, `list_prompts`, `get_active_prompt`, `get_repos_analyzed_below_prompt`
- Produces: commandes `starred prompt list|add|activate` et option `starred analyze --restale`

- [ ] **Step 1 : écrire les tests qui échouent**

Créer `tests/test_cli_prompt.py` :

```python
from click.testing import CliRunner

from starred.cli import main
from starred.db import get_active_prompt, open_db


def test_prompt_list_shows_the_seeded_version(tmp_path):
    db = tmp_path / "test.db"
    with open_db(db):
        pass
    result = CliRunner().invoke(main, ["prompt", "list", "--db", str(db)])
    assert result.exit_code == 0
    assert "legacy" in result.output


def test_prompt_add_then_activate(tmp_path):
    db = tmp_path / "test.db"
    with open_db(db):
        pass
    system = tmp_path / "system.txt"
    system.write_text("Be terse.", encoding="utf-8")
    template = tmp_path / "template.txt"
    template.write_text("Repo: {name}", encoding="utf-8")

    runner = CliRunner()
    added = runner.invoke(
        main,
        ["prompt", "add", "--name", "v2", "--system-file", str(system),
         "--template-file", str(template), "--db", str(db)],
    )
    assert added.exit_code == 0

    activated = runner.invoke(main, ["prompt", "activate", "2", "--db", str(db)])
    assert activated.exit_code == 0

    with open_db(db) as conn:
        assert get_active_prompt(conn).name == "v2"


def test_prompt_activate_rejects_an_unknown_id(tmp_path):
    db = tmp_path / "test.db"
    with open_db(db):
        pass
    result = CliRunner().invoke(main, ["prompt", "activate", "99", "--db", str(db)])
    assert result.exit_code == 1
    assert "99" in result.output
```

- [ ] **Step 2 : vérifier l'échec**

Run: `uv run pytest tests/test_cli_prompt.py -v`
Expected: FAIL, code de sortie 2 et « No such command 'prompt' ».

- [ ] **Step 3 : ajouter le groupe de commandes**

Dans `starred/cli.py`, après la commande `analyze` :

```python
@main.group()
def prompt():
    """Manage analysis prompt versions."""


@prompt.command("list")
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def prompt_list(db_path: Path):
    """List prompt versions."""
    with open_db(db_path) as conn:
        rows = list_prompts(conn)

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("ID", justify="right")
    table.add_column("Name")
    table.add_column("Active")
    table.add_column("Created")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["name"],
            "[green]yes[/green]" if row["is_active"] else "",
            row["created_at"][:10],
        )
    console.print(table)


@prompt.command("add")
@click.option("--name", required=True, help="Version name, e.g. v2-concise")
@click.option("--system-file", required=True, type=Path, help="File holding the system prompt")
@click.option("--template-file", required=True, type=Path, help="File holding the user template")
@click.option("--activate", is_flag=True, default=False, help="Activate the new version")
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def prompt_add(name: str, system_file: Path, template_file: Path, activate: bool, db_path: Path):
    """Add a prompt version from two text files."""
    with open_db(db_path) as conn:
        prompt_id = add_prompt(
            conn,
            name,
            system_file.read_text(encoding="utf-8"),
            template_file.read_text(encoding="utf-8"),
            activate=activate,
        )
    state = "active" if activate else "inactive"
    console.print(f"[green]✓[/green] Prompt [cyan]{name}[/cyan] added as id {prompt_id} ({state}).")


@prompt.command("activate")
@click.argument("prompt_id", type=int)
@click.option("--db", "db_path", default=DB_PATH, type=Path, show_default=True)
def prompt_activate(prompt_id: int, db_path: Path):
    """Make a prompt version the active one."""
    with open_db(db_path) as conn:
        known = {row["id"] for row in list_prompts(conn)}
        if prompt_id not in known:
            console.print(f"[red]Unknown prompt id:[/red] {prompt_id}")
            raise SystemExit(1)
        activate_prompt(conn, prompt_id)
    console.print(f"[green]✓[/green] Prompt {prompt_id} is now active.")
```

Compléter l'import depuis `.db` avec `activate_prompt`, `add_prompt`, `get_active_prompt`,
`get_repos_analyzed_below_prompt` et `list_prompts`.

- [ ] **Step 4 : ajouter la ré-analyse ciblée**

Dans la commande `analyze`, ajouter une option et remplacer la sélection des lignes :

```python
@click.option(
    "--restale",
    is_flag=True,
    default=False,
    help="Re-analyze repos scored under an older prompt version",
)
```

```python
        prompt = get_active_prompt(conn)
        if restale:
            rows = get_repos_analyzed_below_prompt(conn, prompt.id, limit)
        else:
            rows = get_repos_without_analysis_with_readme(conn, limit)
```

Ajuster le message affiché quand `rows` est vide : « All repositories are up to date with
the active prompt. » sous `--restale`, le message actuel sinon.

- [ ] **Step 5 : vérifier le succès**

Run: `uv run pytest tests/ -v`
Expected: PASS.

- [ ] **Step 6 : commit**

```bash
git add starred/cli.py tests/test_cli_prompt.py
git commit -m "feat(cli): gestion des versions de prompt et ré-analyse ciblée"
```

---

### Task 6 : API FastAPI de lecture

Le tri croissant sur le champ filtré rend l'avancée du curseur monotone : une réponse
tronquée par `limit` ne saute jamais un dépôt.

**Files:**
- Create: `starred/api.py`
- Modify: `starred/db.py` (fonction `get_repos_since`)
- Modify: `pyproject.toml` (dépendances)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `starred.db.open_db`, `starred.paths.default_db_path`
- Produces: `starred.api.app` (application FastAPI), `starred.db.get_repos_since(conn, since: str | None, min_score: int, limit: int) -> list[sqlite3.Row]`

- [ ] **Step 1 : ajouter les dépendances**

```bash
uv add fastapi uvicorn
```

- [ ] **Step 2 : écrire les tests qui échouent**

Créer `tests/test_api.py` :

```python
import pytest
from fastapi.testclient import TestClient

from starred.api import app
from starred.db import get_active_prompt, open_db, upsert_analysis, upsert_repo
from starred.models import StarredRepo
from datetime import UTC, datetime


def _repo(name: str) -> StarredRepo:
    return StarredRepo(
        starred_at=datetime(2024, 3, 15, tzinfo=UTC),
        name_with_owner=name,
        description="A test repository",
        topics=["python"],
        is_archived=False,
        pushed_at=datetime(2024, 2, 1, tzinfo=UTC),
        url=f"https://github.com/{name}",
        primary_language="Python",
        stargazer_count=42,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("STARRED_DB", str(db))
    with open_db(db) as conn:
        prompt_id = get_active_prompt(conn).id
        for index, name in enumerate(["octocat/one", "octocat/two", "octocat/three"]):
            repo_id = upsert_repo(conn, _repo(name))
            upsert_analysis(conn, repo_id, index + 2, f"Summary {index}", prompt_id)
            conn.execute(
                "UPDATE analysis SET analyzed_at = ? WHERE repo_id = ?",
                (f"2026-08-2{index + 1}T04:00:00+00:00", repo_id),
            )
    return TestClient(app)


def test_health_answers_without_touching_the_database(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_repos_returns_every_analyzed_repository(client):
    items = client.get("/repos").json()["items"]
    assert len(items) == 3


def test_repos_orders_by_analyzed_at_ascending(client):
    items = client.get("/repos").json()["items"]
    assert [i["name_with_owner"] for i in items] == [
        "octocat/one",
        "octocat/two",
        "octocat/three",
    ]


def test_repos_filters_on_analyzed_at(client):
    items = client.get("/repos", params={"since": "2026-08-21T04:00:00+00:00"}).json()["items"]
    assert [i["name_with_owner"] for i in items] == ["octocat/two", "octocat/three"]


def test_repos_filters_on_min_score(client):
    items = client.get("/repos", params={"min_score": 3}).json()["items"]
    assert [i["name_with_owner"] for i in items] == ["octocat/two", "octocat/three"]


def test_repos_honours_the_limit(client):
    items = client.get("/repos", params={"limit": 2}).json()["items"]
    assert len(items) == 2


def test_repos_exposes_topics_as_a_list(client):
    assert client.get("/repos").json()["items"][0]["topics"] == ["python"]


def test_repos_never_exposes_the_readme(client):
    assert "readme_path" not in client.get("/repos").json()["items"][0]
```

- [ ] **Step 3 : vérifier l'échec**

Run: `uv run pytest tests/test_api.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'starred.api'`.

- [ ] **Step 4 : ajouter la requête de lecture**

Dans `starred/db.py` :

```python
def get_repos_since(
    conn: sqlite3.Connection, since: str | None, min_score: int, limit: int
) -> list[sqlite3.Row]:
    """Repos analysés après `since`, du plus ancien au plus récent.

    La comparaison sur `analyzed_at` est lexicographique : la colonne ne contient
    que des ISO 8601 UTC produits par `datetime.now(UTC).isoformat()`, de longueur
    et d'offset constants, donc l'ordre des chaînes est celui des instants.
    """
    return conn.execute(
        """
        SELECT r.name_with_owner, r.url, r.description, r.primary_language,
               r.stargazer_count, r.is_archived, r.starred_at,
               a.score, a.summary, a.analyzed_at,
               GROUP_CONCAT(DISTINCT t.topic_name) AS topics
        FROM repositories r
        JOIN analysis a ON a.repo_id = r.id
        LEFT JOIN topics t ON t.repo_id = r.id
        WHERE a.score >= ? AND (? IS NULL OR a.analyzed_at > ?)
        GROUP BY r.id
        ORDER BY a.analyzed_at ASC
        LIMIT ?
        """,
        (min_score, since, since, limit),
    ).fetchall()
```

- [ ] **Step 5 : écrire `starred/api.py`**

```python
from fastapi import FastAPI, Query
from pydantic import BaseModel

from .db import get_repos_since, open_db

app = FastAPI(title="github-starred-repositories")


class Repo(BaseModel):
    name_with_owner: str
    url: str
    description: str | None
    primary_language: str | None
    topics: list[str]
    stargazer_count: int
    is_archived: bool
    starred_at: str
    score: int
    summary: str
    analyzed_at: str


class RepoPage(BaseModel):
    items: list[Repo]


@app.get("/health")
def health() -> dict[str, str]:
    # Répond depuis l'état du processus seul. Interroger la base ici sortirait le
    # service pour une lenteur qui ne l'empêche pas de servir.
    return {"status": "ok"}


@app.get("/repos", response_model=RepoPage)
def repos(
    since: str | None = Query(default=None, description="ISO 8601 UTC, exclusive lower bound"),
    min_score: int = Query(default=1, ge=1, le=5),
    limit: int = Query(default=100, ge=1, le=500),
) -> RepoPage:
    with open_db() as conn:
        rows = get_repos_since(conn, since, min_score, limit)

    return RepoPage(
        items=[
            Repo(
                name_with_owner=row["name_with_owner"],
                url=row["url"],
                description=row["description"],
                primary_language=row["primary_language"],
                topics=sorted(row["topics"].split(",")) if row["topics"] else [],
                stargazer_count=row["stargazer_count"],
                is_archived=bool(row["is_archived"]),
                starred_at=row["starred_at"],
                score=row["score"],
                summary=row["summary"],
                analyzed_at=row["analyzed_at"],
            )
            for row in rows
        ]
    )
```

`open_db()` sans argument résout le chemin par `default_db_path()` à chaque requête,
donc `STARRED_DB` est lu dans le conteneur comme dans les tests.

- [ ] **Step 6 : vérifier le succès**

Run: `uv run pytest tests/ -v && uv run ruff check . && uv run ty check`
Expected: PASS.

- [ ] **Step 7 : vérifier à la main**

```bash
STARRED_DB=starred.db uv run uvicorn starred.api:app --port 8000
curl -s 'http://127.0.0.1:8000/repos?min_score=4&limit=3' | head -40
```

Expected: trois dépôts, triés par `analyzed_at` croissant, sans champ de README.

- [ ] **Step 8 : commit**

```bash
git add pyproject.toml uv.lock starred/api.py starred/db.py tests/test_api.py
git commit -m "feat(api): endpoints /health et /repos en lecture seule"
```

---

### Task 7 : conteneurisation

Une seule image pour deux conteneurs qui ne diffèrent que par leur commande. Le fuseau
du worker est explicite : le digest partant à 5 h locale, un cron en UTC passerait après
lui une moitié de l'année.

**Files:**
- Create: `Dockerfile`, `docker-compose.yml`, `crontab`, `.dockerignore`
- Modify: `.env.example`, `README.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: `starred.api:app`, la CLI `starred`
- Produces: services `api` et `worker`, réseau externe `mgx`, volumes `starred-db` et `starred-readmes`

- [ ] **Step 1 : écrire le `Dockerfile`**

```dockerfile
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    STARRED_DB=/data/starred.db \
    STARRED_README_DIR=/data/readmes

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ADD https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-arm64 \
    /usr/local/bin/supercronic
RUN chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY starred ./starred
RUN uv sync --frozen --no-dev

COPY crontab /app/crontab

ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "starred.api:app", "--host", "0.0.0.0", "--port", "8000"]
```

Adapter le suffixe du binaire supercronic à l'architecture du VPS : `-linux-amd64` sur
un hôte x86, `-linux-arm64` sur ARM. Vérifier avec `uname -m` sur la cible avant de
construire.

- [ ] **Step 2 : écrire le `crontab`**

```
0 4,10,16,22 * * * cd /app && starred sync && starred fetch-readme --concurrency 10 && starred analyze --limit 30
```

Une seule ligne, les trois étapes chaînées par `&&` : une synchronisation en échec doit
empêcher l'analyse plutôt que la lancer sur des données incomplètes.

- [ ] **Step 3 : écrire le `docker-compose.yml`**

```yaml
services:
  api:
    build: .
    restart: unless-stopped
    environment:
      STARRED_DB: /data/starred.db
    volumes:
      - starred-db:/data
    networks:
      - mgx
    healthcheck:
      test: ["CMD", "python", "-c", "import httpx; httpx.get('http://127.0.0.1:8000/health').raise_for_status()"]
      interval: 30s
      timeout: 5s
      retries: 3

  worker:
    build: .
    restart: unless-stopped
    command: ["supercronic", "/app/crontab"]
    environment:
      TZ: Europe/Brussels
      STARRED_DB: /data/starred.db
      STARRED_README_DIR: /data/readmes
      GITHUB_TOKEN: ${GITHUB_TOKEN:?GITHUB_TOKEN is required}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY is required}
    volumes:
      - starred-db:/data
      - starred-readmes:/data/readmes

volumes:
  starred-db:
  starred-readmes:

networks:
  mgx:
    external: true
```

Le service `api` ne publie aucun port et ne reçoit ni jeton GitHub ni clé Anthropic : il
ne fait que lire. Le `worker` n'est sur aucun réseau : il n'a rien à servir. La syntaxe
`${VAR:?message}` arrête le démarrage plutôt que de laisser un conteneur tourner sans
jeton et échouer quatre fois par jour en silence.

- [ ] **Step 4 : écrire le `.dockerignore`**

```
.git
.venv
__pycache__
*.pyc
starred.db
starred.db-wal
starred.db-shm
readmes/
tests/
docs/
.env
```

- [ ] **Step 5 : compléter `.env.example`**

Ajouter :

```
# Anthropic API key, used by `starred analyze`
ANTHROPIC_API_KEY=sk-ant-your-key-here
```

- [ ] **Step 6 : construire et vérifier**

```bash
docker compose build
docker network create mgx 2>/dev/null || true
docker compose up -d
docker compose exec api python -c "import httpx; print(httpx.get('http://127.0.0.1:8000/health').json())"
docker compose run --rm worker starred prompt list
```

Expected: `{'status': 'ok'}`, puis un tableau contenant la version `legacy` marquée
active. La base est vide au premier démarrage : `docker compose run --rm worker starred
sync` la remplit.

- [ ] **Step 7 : documenter**

Dans `README.md`, ajouter une section « Docker » décrivant les deux services, les deux
volumes, les variables requises et le rythme du cron.

Dans `CLAUDE.md`, mettre à jour : l'arborescence (ajout de `api.py`, `paths.py`,
`default_prompt.py`), la mention de `claude-agent-sdk` remplacée par `anthropic`, la
liste des commandes CLI (groupe `prompt`, option `--restale`), et les variables de
configuration (`STARRED_DB`, `STARRED_README_DIR`, `ANTHROPIC_API_KEY`, et
`GITHUB_TOKEN` désormais obligatoire).

- [ ] **Step 8 : commit**

```bash
git add Dockerfile docker-compose.yml crontab .dockerignore .env.example README.md CLAUDE.md
git commit -m "feat(docker): image unique, service api et worker supercronic"
```

---

## Ce qui reste après ce plan

Le flow `github_stars_digest` dans `mgx-orchestrator` : client httpx sur `/repos`, table
`service_cursors`, envoi email et Telegram, cron `0 5 * * *` en `Europe/Brussels`. Il
fera l'objet de son propre plan, dans son propre dépôt, une fois cette API déployée.
