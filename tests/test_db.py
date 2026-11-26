import sqlite3

import pytest
from django.db import connection

from records.db import apply_pragmas


def test_a_file_database_switches_to_write_ahead_logging(tmp_path):
    with sqlite3.connect(tmp_path / "synthetic.sqlite3") as raw:
        apply_pragmas(raw.cursor())
        journal = raw.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = raw.execute("PRAGMA synchronous").fetchone()[0]

    assert journal.lower() == "wal"
    assert synchronous == 1


@pytest.mark.django_db
def test_the_connection_waits_for_a_concurrent_import_instead_of_failing():
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA busy_timeout")
        assert cursor.fetchone()[0] == 20000


@pytest.mark.django_db
def test_an_in_memory_database_is_left_alone():
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode")
        assert cursor.fetchone()[0].lower() == "memory"
