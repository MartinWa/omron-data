from django.db.backends.signals import connection_created


def apply_pragmas(cursor):
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")


def configure_sqlite(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    if connection.settings_dict.get("NAME") in (None, "", ":memory:"):
        return
    with connection.cursor() as cursor:
        apply_pragmas(cursor)


def connect(dispatch_uid="records.configure_sqlite"):
    connection_created.connect(configure_sqlite, dispatch_uid=dispatch_uid)
