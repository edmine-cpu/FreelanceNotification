from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Project:
    id: str
    url: str
    title: str
    budget: str
    description: str
    relative_time: str
    absolute_time: str
    published_ts: int
    skill_id: int = 0
    category_name: str = ""
    category_url: str = ""
    client_name: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(
            id=str(data["id"]),
            url=data.get("url", ""),
            title=data.get("title", ""),
            budget=data.get("budget", ""),
            description=data.get("description", ""),
            relative_time=data.get("relative_time", ""),
            absolute_time=data.get("absolute_time", ""),
            published_ts=int(data.get("published_ts", 0)),
            skill_id=int(data.get("skill_id", 0)),
            category_name=data.get("category_name", ""),
            category_url=data.get("category_url", ""),
            client_name=data.get("client_name", ""),
        )
