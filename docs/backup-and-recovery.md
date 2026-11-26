# Backup and recovery

The SQLite database is the whole application state: accounts, measurements, blood-pressure
panels and their components, the import ledger and the token digests. In a container it is
`omron.sqlite3` in the `data` directory next to `compose.yaml`, which the image mounts at
`/app/data`; in a working copy it is `.data/db.sqlite3` unless `OMRON_DB_PATH` says
otherwise. Nothing else needs backing up: staged uploads live in a temporary directory and
are meant to be lost, and the static files are rebuilt from the image.

**A backup is personal health data.** Treat it like the live database: encrypt it, store it
off-host with restricted access, never attach it to an issue or commit it, and delete local
copies when you are done. `.gitignore` excludes `data/`, `.data/` and `*.sqlite3`, and
`tools/check_no_private_data.py` rejects a backup file by name, so a committed copy got
there past both.

## Back up while the service runs

Use SQLite's online backup API so a consistent copy is taken without stopping anything.
Because the database lives on a host directory, the copy appears on the host directly:

```sh
docker compose exec -T web python -c "import sqlite3; s = sqlite3.connect('/app/data/omron.sqlite3'); d = sqlite3.connect('/app/data/backup.sqlite3'); s.backup(d); d.close(); s.close()"
mv data/backup.sqlite3 ./omron-data-$(date +%F).sqlite3
```

The same one-liner works in a working copy, with the path `OMRON_DB_PATH` points at:

```sh
uv run python -c "import sqlite3; s = sqlite3.connect('.data/db.sqlite3'); d = sqlite3.connect('.data/backup.sqlite3'); s.backup(d); d.close(); s.close()"
mv .data/backup.sqlite3 ./omron-data-$(date +%F).sqlite3
```

Move the copy out of the mounted directory; a second copy next to the live database is
still inside the thing you meant to protect.

## Why not copy the file alone

`cp omron.sqlite3` while the app is running can capture a torn or stale file:

- `omron_data/settings.py` sets a 20-second busy timeout and no journal mode, so the
  database runs in SQLite's default rollback-journal mode. A write in flight is in
  `omron.sqlite3-journal` until it commits, and a copy taken in the middle of it is not a
  database.
- If write-ahead logging is ever in use, a `omron.sqlite3-wal` and an `omron.sqlite3-shm`
  appear next to the database. The newest committed rows then live in the `-wal` file, and
  the main file on its own is missing them; the `-shm` is the matching index. Copying the
  database without its sidecars loses the newest data, and copying the sidecars without the
  database is worse.

The online backup API above reads a consistent snapshot and needs neither. If you must
take a filesystem-level snapshot instead, stop the container first:

```sh
docker compose stop web
cp -a data ./omron-data-$(date +%F)
docker compose start web
```

Do not point the `data` directory at NFS or SMB: SQLite locking is not safe on a network
filesystem, and a snapshot taken there is not trustworthy.

## Verify a backup

Check integrity before you need it. A backup file is complete, so open it as an immutable
read-only database: a normal connection would try to create `-wal` and `-shm` sidecars next
to it, which a read-only medium will not allow.

```sh
uv run python -c "import sqlite3; c = sqlite3.connect('file:./omron-data-2026-11-26.sqlite3?mode=ro&immutable=1', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

The result should be `ok`. On a host without a working copy, run the same command in the
image you deployed:

```sh
docker run --rm -v "$PWD/omron-data-2026-11-26.sqlite3:/backup/backup.sqlite3:ro" \
  --entrypoint python ghcr.io/martinwa/omron-data:latest -c \
  "import sqlite3; c = sqlite3.connect('file:/backup/backup.sqlite3?mode=ro&immutable=1', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
```

A row count is a cheap second opinion that the copy is the one you think it is:

```sh
uv run python -c "import sqlite3; c = sqlite3.connect('file:./omron-data-2026-11-26.sqlite3?mode=ro&immutable=1', uri=True); print(c.execute('select count(*) from records_measurement').fetchone()[0]); c.close()"
```

## Restore

Restoring replaces the live database. Take a current backup first, then stop the app so
nothing is writing.

```sh
docker compose stop web
sudo cp ./omron-data-2026-11-26.sqlite3 data/omron.sqlite3
sudo chown 1000:1000 data/omron.sqlite3
sudo rm -f data/omron.sqlite3-journal data/omron.sqlite3-wal data/omron.sqlite3-shm
docker compose start web
```

Replace `1000:1000` with your `PUID` and `PGID`. The `chown` matters because the container
is read-only and unprivileged and cannot fix ownership itself. Removing the stale journal
and sidecar files matters because they belong to the database you just replaced; leaving
them can resurrect rows you meant to drop or make the file the app opens unreadable.

Order matters when you roll back a version as well: restore the database of the release
you are going back to, together with that release's image. A schema migration that already
ran is not undone by starting an older image.

The server applies migrations to the restored database on start. Confirm the restore
landed before anyone relies on it:

```sh
docker compose exec -T web python tools/check_production.py
docker compose exec -T web python tools/healthcheck.py
```

Then log in and check the readings page, the import ledger and the tokens page. If
anything is wrong, stop the app and restore the previous backup the same way.

## Schedule and retention

`data/` is an ordinary host directory, so any host backup tool works: restic, borg, kopia,
Vorta, a NAS snapshot task, or the Proxmox backup client. Because the app must keep running,
prefer the online backup command on a timer, or a snapshot of the `data` directory taken
while the container is stopped. Run backups at least as often as you import, keep several
dated copies, and periodically restore one into a throwaway installation to prove it
works.

Three things travel with a backup and are easy to forget:

- Tokens are rows in this database, so a restored database restores token validity too.
  Revoke anything you no longer trust after a restore.
- Sessions are rows as well. Keep the `OMRON_SECRET_KEY` you used with a backup, or every
  session is invalidated; changing the key deliberately logs everyone out.
- The import ledger's checksums are rows too, so a restored ledger decides which files are
  "already imported" again.

## Purging, and what it does to a backup

Purging an import deletes the measurements that run created and then the ledger row, which
is the supported way to undo a wrong file. It is not a way to remove data from your
backups: every copy taken before the purge still contains those readings, and a restore
brings them back with the ledger row, so the same file then looks imported again and a
plain import of it does nothing.

If a purge is meant to make data go away, purge it, take a fresh backup, and destroy the
older copies; otherwise treat the purge as a correction of the live database only. When a
restore does bring a purged run back and you want the readings it covered to be present,
import the overlapping windows with `--replay`, which re-reads a known checksum without
overwriting a single stored value.
