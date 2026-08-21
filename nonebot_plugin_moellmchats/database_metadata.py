from __future__ import annotations

from sqlalchemy import MetaData

_DATABASE_NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def database_naming_convention() -> dict[str, str]:
    """Return a fresh deterministic naming convention for database objects."""

    return dict(_DATABASE_NAMING_CONVENTION)


database_metadata = MetaData(naming_convention=database_naming_convention())
"""Shared empty metadata root; business tables begin in F-04."""
