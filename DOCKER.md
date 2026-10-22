# Running the IMF demonstrator in Docker

The `Dockerfile` packages the complete demonstrator (PDGS simulator + IMF portal) into one Linux
image based on `python:3.14-slim-trixie`. The container runs as the non-root user `10001` and
keeps all its state in the volume `/app/data`.

## Build

From the project root (the directory that contains `Dockerfile` and `run_demo.py`):

```sh
docker build --pull -t fts-monitoring-demo:local .
```

BuildKit is required, because the Dockerfile embeds its launcher as a here-document. It is the
default in current Docker Desktop and Docker Engine.

## Run

```sh
docker volume create imf-data

docker run -d --name imf \
  --init \
  -p 127.0.0.1:8000:8000 \
  -v imf-data:/app/data \
  --read-only --tmpfs /tmp \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --stop-timeout 30 \
  fts-monitoring-demo:local
```

On Windows PowerShell, replace the trailing `\` with a backtick (`` ` ``) or put the command on one line.

| Option | Purpose |
|---|---|
| `--init` | Forwards signals and reaps the two child processes (simulator, portal) |
| `-p 127.0.0.1:8000:8000` | Publishes the portal on this machine only. The demo accounts have well-known passwords |
| `-v imf-data:/app/data` | Named volume for the database, reports, notification log, simulator clock and session key |
| `--read-only --tmpfs /tmp` | Read-only root filesystem. The application writes only to `/app/data` and `/tmp` |
| `--cap-drop ALL`, `no-new-privileges` | Removes the remaining Linux privileges |
| `--stop-timeout 30` | Gives the launcher time to stop both services cleanly after `SIGINT` |

Then open <http://127.0.0.1:8000> and log in as `admin/admin`, `expert/expert` or `guest/guest`.

Arguments after the image name are passed to `run_demo.py`. For example, to start with a shorter
mission history:

```sh
docker run ... fts-monitoring-demo:local --days 10 --orbit-every 15
```

`--days` only takes effect on the first start of a fresh volume.

## First start

* The simulator creates a mission history of `--days` days (default 90). The IMF then ingests this
  backlog (L0 headers and L1 products) in the background, which takes about 2–3 minutes.
  After that, a new orbit arrives every 30 seconds.
* The database schema is created or migrated automatically at start-up.
* Daily and weekly reports are generated once their period is complete. They appear under
  *Reports*.
* `docker ps` shows the container as `healthy` once both the portal and the simulator respond. This
  does not mean the backfill is finished.

## Operation

```sh
docker logs -f imf       # follow the logs
docker stop imf          # stop (state is kept in the volume)
docker start imf         # continue where it stopped
```

**Reset.** `--reset` is **not supported** in the container, because it tries to delete the volume's
mount point. Remove the volume instead:

```sh
docker rm -f imf && docker volume rm imf-data
```

**Operations CLI.** Backups, checks, exports and campaigns can be run inside the running container.
Files are written into the volume:

```sh
docker exec imf python -m imf.cli db backup          # -> /app/data/backups
docker exec imf python -m imf.cli db check
docker exec imf python -m imf.cli db export --out /app/data/exports/mdb.zip
```

`db restore` replaces the database. Stop the portal first, then run the restore in a one-off
container on the same volume:
`docker run --rm -v imf-data:/app/data --entrypoint python fts-monitoring-demo:local -m imf.cli db restore /app/data/backups/<file> --yes`.

**Volume type.** Use a named volume, not a bind mount to a Windows folder. SQLite in WAL mode can
run into file-locking problems on bind mounts under Docker Desktop.

**Custom configuration.** Mount a YAML file and point `IMF_CONFIG` to it. The launcher still sets
the web host and port and the session key:

```sh
docker run ... -v ./my-imf.yaml:/config/imf.yaml:ro -e IMF_CONFIG=/config/imf.yaml fts-monitoring-demo:local
```

Note that the image always starts the bundled simulator through `run_demo.py`, even if the
configuration points `pdgs.base_url` to another PDGS. Running against a real PDGS needs a separate
deployment without the simulator (see `docs/feature_gap_analysis.md`, section 8, D4).

## Smoke test

1. Build the image and run the container as above.
2. `docker ps`: the status becomes `healthy` within about 2 minutes.
3. Open <http://127.0.0.1:8000> and log in as `admin`. The dashboard fills with products as the
   backfill runs.
4. After the backfill, *Reports* lists daily reports, and their PDFs download correctly.
