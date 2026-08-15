from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PATH = PROJECT_ROOT / "data" / "isojam.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

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