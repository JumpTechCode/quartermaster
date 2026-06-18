"""The async database engine factory.

The only place the SQLAlchemy async engine is constructed. It takes a database
URL (read from settings by the composition root in a later slice) and returns an
``AsyncEngine`` over asyncpg; the command path uses Core ``AsyncConnection``s,
not the ORM. Pool configuration stays at the library defaults for now.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(database_url: str) -> AsyncEngine:
    """Build the async engine for ``database_url`` (a ``postgresql+asyncpg://`` URL)."""
    return create_async_engine(database_url)
