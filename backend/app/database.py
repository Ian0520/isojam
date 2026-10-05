from collections.abc import Generator

from fastapi import Request
from sqlalchemy import URL, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_database_path

DATABASE_PATH = get_database_path()
DATABASE_URL = URL.create("sqlite", database=str(DATABASE_PATH))


def _set_sqlite_foreign_keys(dbapi_connection, connection_record):
    previous_autocommit = dbapi_connection.autocommit
    dbapi_connection.autocommit = True

    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

    dbapi_connection.autocommit = previous_autocommit


def enable_sqlite_foreign_keys(engine):
    event.listen(engine, "connect", _set_sqlite_foreign_keys)


DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL)
enable_sqlite_foreign_keys(engine)

SessionLocal = sessionmaker(engine)


def get_db(request: Request) -> Generator[Session, None, None]:
    with request.app.state.db_session_factory() as session:
        yield session


def commit_session(session: Session) -> None:
    """Commit, clearing driver transaction state if the commit fails.

    SQLite can retain a transaction after a failed COMMIT while SQLAlchemy has
    marked its transaction inactive. Roll back the owned driver before releasing
    the session connection, so subsequent requests cannot reuse uncommitted work.
    """
    driver = session.connection().connection.dbapi_connection
    try:
        session.commit()
    except BaseException:
        try:
            driver.rollback()
        finally:
            session.rollback()
        raise
