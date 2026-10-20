from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NAME = r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"


class AlertInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Literal["firing", "resolved"]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{16,64}$")
    startsAt: datetime
    endsAt: datetime | None = None
    labels: dict[str, str] = Field(min_length=2, max_length=32)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=32)
    generatorURL: str = Field(default="", max_length=2048)

    @field_validator("labels", "annotations")
    @classmethod
    def bounded_map(cls, values):
        if any(len(k) > 128 or len(v.encode()) > 2048 for k, v in values.items()):
            raise ValueError("Oversized alert fields")
        return values

    @model_validator(mode="after")
    def valid_episode(self):
        import re

        for key in ["namespace", "pod"]:
            if not re.fullmatch(NAME, self.labels.get(key, "")):
                raise ValueError("Namespace and Pod labels required")
        if not self.labels.get("alertname") or len(self.labels["alertname"]) > 128:
            raise ValueError("Alert name required")
        if self.startsAt.tzinfo is None or (self.endsAt and self.endsAt.tzinfo is None):
            raise ValueError("Timezone required")
        if self.status == "resolved" and (not self.endsAt or self.endsAt <= self.startsAt):
            raise ValueError("Resolved alert requires an end after its start")
        return self


class Webhook(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: Literal["4"] = "4"
    status: Literal["firing", "resolved"]
    alerts: list[AlertInput] = Field(min_length=1, max_length=32)


class TransitionInput(BaseModel):
    state: Literal["ESCALATED", "CANCELLED"]
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
