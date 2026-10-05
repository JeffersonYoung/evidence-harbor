"""SQLAlchemy unit-of-work and database setup."""

from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url or url in ("sqlite://", "sqlite+pysqlite://"):
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def sqlite_pragmas(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=10000")

    return engine


engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind=None):
    from . import models  # noqa: F401 -- register mappings

    try:
        from . import auth, continuous, extensions, scheduling  # noqa: F401 -- register mappings
    except ImportError:
        pass
    Base.metadata.create_all(bind or engine)


def get_session(request: Request = None):
    factory = request.app.state.session_factory if request is not None else SessionLocal
    with factory() as session:
        if request is not None:
            session.info["settings"] = request.app.state.settings
        try:
            yield session
        except Exception:
            session.rollback()
            raise
