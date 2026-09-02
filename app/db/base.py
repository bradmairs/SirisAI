"""Declarative base for all ORM models.

Import new models in here (or wherever `Base.metadata.create_all` is called
from) so they get registered before table creation. There's no Alembic yet
-- `create_all` is fine while the schema is empty; add migrations before the
first model lands.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
