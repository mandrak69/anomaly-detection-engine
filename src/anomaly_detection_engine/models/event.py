from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Team:
    id: str
    canonical_name: str


@dataclass(frozen=True)
class Event:
    id: str
    sport: str
    league: str
    home_team: Team
    away_team: Team
    start_time: datetime

    @property
    def display_name(self) -> str:
        return f"{self.home_team.canonical_name} vs {self.away_team.canonical_name}"
