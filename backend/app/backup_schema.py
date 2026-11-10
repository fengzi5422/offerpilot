"""Validate SQLite structure before allowing trusted local migration code to run."""
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.domain import AppError, uid
from app.persistence import SCHEMA_REVISION


def config():
    root = Path(sys._MEIPASS)/"backend" if getattr(sys,"frozen",False) else Path(__file__).parent.parent
    result=Config()
    result.set_main_option('script_location',str(root/'migrations'))
    return result


def signature(path):
    uri=f'file:{path.as_posix()}?mode=ro'
    with closing(sqlite3.connect(uri,uri=True)) as connection:
        connection.execute('PRAGMA trusted_schema=OFF')
        objects=list(connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if len(objects)>200 or any(t not in ('table','index') or not re.fullmatch(r'[a-z_][a-z_0-9]*',n)
                                   or len(sql or '')>65536 for t,n,sql in objects):
            raise ValueError('Unsupported schema objects')
        if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Invalid database')
        result={}
        for kind,name,_ in objects:
            if kind!='table':
                continue
            columns=sorted(tuple(row[1:]) for row in connection.execute(f'PRAGMA table_xinfo("{name}")'))
            foreign=sorted(tuple(row[2:]) for row in connection.execute(f'PRAGMA foreign_key_list("{name}")'))
            indexes=[]
            for row in connection.execute(f'PRAGMA index_list("{name}")'):
                index_name=row[1]
                if not re.fullmatch(r'[a-z_][a-z_0-9]*',index_name):
                    raise ValueError('Invalid index name')
                index_rows=list(connection.execute(f'PRAGMA index_xinfo("{index_name}")'))
                keys=tuple(r[2] for r in index_rows if r[5])
                # Expression and partial indexes are not part of these schema revisions.
                if any(key is None for key in keys) or row[4]:
                    raise ValueError('Unsupported index')
                # Column names/order, sort direction and collation are semantic;
                # numeric column IDs vary across equivalent historical schemas.
                indexes.append((bool(row[2]),tuple(tuple(r[2:]) for r in index_rows)))
            result[name]=(columns,foreign,sorted(indexes))
    engine=create_engine('sqlite://',creator=lambda:sqlite3.connect(uri,uri=True))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA trusted_schema=OFF')
            inspector=inspect(connection)
            checks={name:sorted(re.sub(r'\s+','',c['sqltext']).lower() for c in inspector.get_check_constraints(name)) for name in result}
        return result,checks
    finally:
        engine.dispose()


def validate_and_upgrade(path, revision):
    reference=path.parent/(uid()+'.reference')
    engine=None
    try:
        with closing(sqlite3.connect(f'file:{path.as_posix()}?mode=ro',uri=True)) as connection:
            if connection.execute('SELECT version_num FROM alembic_version').fetchall()!=[(revision,)]:
                raise ValueError('Schema revision mismatch')
        actual=signature(path)
        engine=create_engine(f'sqlite:///{reference.as_posix()}')
        cfg=config()
        with engine.connect() as connection:
            cfg.attributes['connection']=connection
            command.upgrade(cfg,revision)
        engine.dispose()
        if actual!=signature(reference):
            raise ValueError('Schema mismatch')
        if revision==SCHEMA_REVISION:
            return None
        engine=create_engine(f'sqlite:///{path.as_posix()}')
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            connection.commit()
            cfg.attributes['connection']=connection
            command.upgrade(cfg,SCHEMA_REVISION)
        return revision
    except (ValueError,sqlite3.DatabaseError):
        raise AppError('BACKUP_SCHEMA_INVALID','备份结构不受支持或已损坏',422) from None
    finally:
        if engine:
            engine.dispose()
        for suffix in ('','-journal','-wal','-shm'):
            Path(str(reference)+suffix).unlink(missing_ok=True)
