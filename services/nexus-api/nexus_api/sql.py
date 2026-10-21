"""方言明确的插入与 upsert，不翻译业务 SQL 字符串。"""

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection

from app.models import Base


def insert_record(connection: Connection, table_name: str, values: dict, *, ignore_keys: list[str]):
    table = Base.metadata.tables[table_name]
    if connection.dialect.name == "mysql":
        statement = mysql_insert(table).values(**values)
        key = next(iter(table.primary_key.columns)).name
        statement = statement.on_duplicate_key_update(**{key: table.c[key]})
    else:
        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=ignore_keys)
    return connection.execute(statement)


def upsert_record(
    connection: Connection, table_name: str, values: dict, *, keys: list[str], update: list[str]
):
    table = Base.metadata.tables[table_name]
    if connection.dialect.name == "mysql":
        statement = mysql_insert(table).values(**values)
        statement = statement.on_duplicate_key_update(**{key: statement.inserted[key] for key in update})
    else:
        statement = sqlite_insert(table).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=keys, set_={key: statement.excluded[key] for key in update}
        )
    return connection.execute(statement)
