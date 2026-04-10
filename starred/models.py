from dataclasses import dataclass
from datetime import datetime


@dataclass
class StarredRepo:
    starred_at: datetime
    name_with_owner: str
    description: str | None
    topics: list[str]
    is_archived: bool
    pushed_at: datetime | None
    url: str
    primary_language: str | None
    stargazer_count: int
