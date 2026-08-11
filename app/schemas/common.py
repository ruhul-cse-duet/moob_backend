from datetime import datetime
from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ORMBase(BaseModel):
    model_config = {"populate_by_name": True, "from_attributes": True}


class Message(BaseModel):
    """Simple action result — always includes success for mobile clients."""
    success: bool = True
    message: str | None = None
    detail: str

    def model_post_init(self, __context) -> None:
        # Keep message and detail in sync when only one is provided.
        if self.message is None:
            object.__setattr__(self, "message", self.detail)


class IdResponse(BaseModel):
    success: bool = True
    message: str = "Created"
    id: str


class Page(BaseModel, Generic[T]):
    success: bool = True
    message: str = "OK"
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
