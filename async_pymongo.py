"""Compatibility shim for legacy async_pymongo imports.

The bots in this repository import `AsyncClient` from `async_pymongo`.
On newer pymongo releases, the third-party async_pymongo package can fail
while constructing cursors used by Pyrogram's Mongo storage.

This local module keeps the same import path but routes `AsyncClient` to
Motor's actively maintained async MongoDB client.
"""

from motor.motor_asyncio import AsyncIOMotorClient


class AsyncClient(AsyncIOMotorClient):
    """Drop-in replacement used by existing bot scripts."""
