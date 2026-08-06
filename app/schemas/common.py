from datetime import datetime
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ORMBase(BaseModel):
    model_config = {"populate_by_name": True, "from_attributes": True}


class Message(BaseModel):
    detail: str


class IdResponse(BaseModel):
    id: str


class Page(BaseModel, Generic[T]):
    items: List[T]
    total: int
    page: int
    page_size: int
    pages: int


class PageParams(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)

    @property
    def skip(self) -> int:
        return (self.page - 1) * self.page_size


class Timestamped(ORMBase):
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
