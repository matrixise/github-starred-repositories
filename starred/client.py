import os
import subprocess
from collections.abc import Generator
from datetime import datetime

import httpx

from .models import StarredRepo

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"

STARRED_QUERY = """
query StarredRepos($cursor: String) {
  viewer {
    starredRepositories(first: 100, after: $cursor, orderBy: {field: STARRED_AT, direction: DESC}) {
      edges {
        starredAt
        node {
          nameWithOwner
          description
          url
          isArchived
          pushedAt
          stargazerCount
          primaryLanguage { name }
          repositoryTopics(first: 10) {
            nodes { topic { name } }
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""


def _get_token() -> str:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
        )
        token = result.stdout.strip()
        if token:
            return token
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    raise RuntimeError("No GitHub token found. Set GITHUB_TOKEN or run `gh auth login`.")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.rstrip("Z") + "+00:00")


def _parse_dt_optional(value: str | None) -> datetime | None:
    if not value:
        return None
    return _parse_dt(value)


def _parse_edge(edge: dict) -> StarredRepo:
    node = edge["node"]
    topics = [t["topic"]["name"] for t in node.get("repositoryTopics", {}).get("nodes", [])]
    lang = node.get("primaryLanguage")
    return StarredRepo(
        starred_at=_parse_dt(edge["starredAt"]),
        name_with_owner=node["nameWithOwner"],
        description=node.get("description"),
        topics=topics,
        is_archived=node["isArchived"],
        pushed_at=_parse_dt_optional(node.get("pushedAt")),
        url=node["url"],
        primary_language=lang["name"] if lang else None,
        stargazer_count=node["stargazerCount"],
    )


def fetch_stargazer_counts(
    repos: list[tuple[int, str]],
    batch_size: int = 100,
) -> Generator[tuple[int, int | None], None, None]:
    """
    Yield (repo_id, stargazer_count) for each repo in `repos`.

    Uses batched GraphQL aliases (one request per `batch_size` repos).
    Yields `None` for repos that no longer exist (renamed/deleted).
    """
    token = _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30) as client:
        for start in range(0, len(repos), batch_size):
            batch = repos[start : start + batch_size]
            aliases = []
            var_defs = []
            variables: dict[str, str] = {}
            for idx, (_repo_id, name_with_owner) in enumerate(batch):
                owner, name = name_with_owner.split("/", 1)
                aliases.append(
                    f"r{idx}: repository(owner: $o{idx}, name: $n{idx}) {{ stargazerCount }}"
                )
                var_defs.append(f"$o{idx}: String!, $n{idx}: String!")
                variables[f"o{idx}"] = owner
                variables[f"n{idx}"] = name
            query = "query(" + ", ".join(var_defs) + ") { " + " ".join(aliases) + " }"

            resp = client.post(
                GITHUB_GRAPHQL_URL,
                headers=headers,
                json={"query": query, "variables": variables},
            )
            resp.raise_for_status()
            data = resp.json()

            if "data" not in data or data["data"] is None:
                raise RuntimeError(f"GraphQL error: {data.get('errors')}")

            payload = data["data"]
            for idx, (repo_id, _name) in enumerate(batch):
                node = payload.get(f"r{idx}")
                if node is None:
                    yield repo_id, None
                else:
                    yield repo_id, node["stargazerCount"]


def fetch_starred(
    stop_at: datetime | None = None,
    cursor: str | None = None,
) -> Generator[tuple[StarredRepo, str], None, None]:
    """
    Yield (StarredRepo, endCursor) pairs for each page.

    If stop_at is provided, stops when starredAt <= stop_at
    (used for incremental sync).
    """
    token = _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30) as client:
        while True:
            resp = client.post(
                GITHUB_GRAPHQL_URL,
                headers=headers,
                json={"query": STARRED_QUERY, "variables": {"cursor": cursor}},
            )
            resp.raise_for_status()
            data = resp.json()

            if "errors" in data:
                raise RuntimeError(f"GraphQL error: {data['errors']}")

            starred = data["data"]["viewer"]["starredRepositories"]
            edges = starred["edges"]
            page_info = starred["pageInfo"]
            end_cursor = page_info["endCursor"]

            stop_reached = False
            for edge in edges:
                repo = _parse_edge(edge)
                if stop_at and repo.starred_at < stop_at:
                    stop_reached = True
                    break
                yield repo, end_cursor

            if stop_reached or not page_info["hasNextPage"]:
                break

            cursor = end_cursor
