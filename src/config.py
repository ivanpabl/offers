from pathlib import Path
from pydantic import BaseModel, Field
import yaml


class SearchConfig(BaseModel):
    text: str = ""
    area: list[int] = Field(default_factory=list)
    salary_from: int | None = None
    employment: str | None = None
    schedule: str | None = None
    experience: str | None = None
    remote: bool | None = None
    auto_queries: bool = False
    max_queries: int = 5
    max_pages: int = 20
    max_rounds: int = 5
    round_delay_seconds: dict = Field(default_factory=lambda: {"min": 60, "max": 180})


class ApplyConfig(BaseModel):
    message: str = ""
    cover_letter: str = ""
    resume_text: str = ""
    max_per_day: int = 50
    batch_size: int = 10
    delay_seconds: dict = Field(default_factory=lambda: {"min": 2, "max": 5})


class DeepSeekConfig(BaseModel):
    api_key: str = ""
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    enabled: bool = False
    language: str = "ru"
    max_tokens: int = 1000
    temperature: float = 0.7


class MatchConfig(BaseModel):
    enabled: bool = False
    model_url: str = "http://localhost:11434"
    model: str = "qwen2.5:7b"
    threshold: int = 60
    max_chars: int = 2500
    workers: int = 4
    backlog: int = 10
    max_retries: int = 2
    timeout: int = 60


class Config(BaseModel):
    search: SearchConfig = Field(default_factory=SearchConfig)
    apply: ApplyConfig = Field(default_factory=ApplyConfig)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    match: MatchConfig = Field(default_factory=MatchConfig)


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        return Config()
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Config(**data)
