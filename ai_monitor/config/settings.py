import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

CONFIG_DIR = Path(__file__).parent
INTERESTS_PATH = CONFIG_DIR / "interests.yaml"


class InterestArea(BaseModel):
    description: str
    keywords: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    # Optional. Unauthenticated GitHub search allows 10 requests/min; a token
    # raises that to 30. The watcher works without one.
    github_token: str = os.environ.get("GITHUB_TOKEN", "")
    interests: dict[str, InterestArea] = Field(default_factory=dict)


def load_interests(path: Path = INTERESTS_PATH) -> dict[str, InterestArea]:
    with open(path) as f:
        data = yaml.safe_load(f)
    return {
        name: InterestArea(**area) for name, area in data["areas"].items()
    }


settings = Settings(interests=load_interests())
