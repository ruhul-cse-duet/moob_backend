"""
Database-per-tenant Mongo layer.

  platform DB  -> tenants, global user directory, signups, subscriptions,
                  plans, otp, platform admins, audit log
  tenant DB    -> users, clients, requests, documents, cases, tasks,
                  partners, notifications, messages, activities

The tenant DB name is derived from the tenant document, never from user input.
"""
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings

_client: Optional[AsyncIOMotorClient] = None


def connect() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(
            settings.MONGODB_URI,
            uuidRepresentation="standard",
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


def get_client() -> AsyncIOMotorClient:
    return connect()


def platform_db() -> AsyncIOMotorDatabase:
    return get_client()[settings.PLATFORM_DB_NAME]


def tenant_db_name(tenant_id: str) -> str:
    return f"{settings.TENANT_DB_PREFIX}{tenant_id}"


def tenant_db(tenant_id: str) -> AsyncIOMotorDatabase:
    return get_client()[tenant_db_name(tenant_id)]


async def drop_tenant_db(tenant_id: str) -> None:
    await get_client().drop_database(tenant_db_name(tenant_id))
