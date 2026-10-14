# syntax=docker/dockerfile:1
# The first line selects Docker's current stable Dockerfile syntax.
# It enables features such as the embedded Python file used below.
# Keep this directive at the very top of the file.

# HOW TO USE THIS FILE
# Put it in the project root, next to run_demo.py and requirements.txt.
# Build the image: docker build --pull -t fts-monitoring-demo:local .
# The final dot means: use the current directory as the build context.
# COPY instructions read project files from that context, not from GitHub.
# See DOCKER.md for the complete container start command.
#
# An IMAGE is the packaged application and its dependencies.
# A CONTAINER is a running instance of that image.
# RUN executes while building the image; ENTRYPOINT executes on container start.
# This file uses two build stages. Only the last stage becomes the final image.

# STEP 1: Choose the Linux distribution and Python version.
# ARG defines a build-time variable; it is not a container environment variable.
# python:3.14-slim-trixie is an official Python image containing:
#   - Debian 13 "Trixie" Linux user-space files and libraries;
#   - a reduced ("slim") set of operating-system packages;
#   - Python 3.14.
# The container shares the Docker host's Linux kernel. On Windows, Docker
# Desktop supplies that kernel through its Linux environment (e.g. WSL2).
# Debian uses glibc, which works with the project's scientific Python wheels.
# Example override: docker build --build-arg PYTHON_IMAGE=python:3.14-slim-trixie .
# The tag can receive updates; --pull requests the latest image for that tag.
ARG PYTHON_IMAGE=python:3.14-slim-trixie

# STEP 2: Start the dependency build stage.
# FROM selects the base image and therefore the operating-system user space.
# AS assigns a name to this stage so we can copy its results later.
FROM ${PYTHON_IMAGE} AS dependencies

# WORKDIR sets the directory for subsequent RUN and COPY instructions.
# Docker creates the directory if it does not already exist.
WORKDIR /build

# Copy the dependency list first. Docker can reuse the cached installation
# layer when application code changes but requirements.txt stays the same.
COPY requirements.txt ./

# STEP 3: Install the application's Python dependencies.
# A virtual environment keeps the packages together under /opt/venv.
# sed creates a runtime list without pytest, which is only needed for testing.
# pip installs the packages from that list:
#   --no-cache-dir: do not store pip's download cache in the image;
#   --only-binary=:all:: accept prebuilt wheels, never compile source packages.
# A platform without suitable wheels will fail here with a pip error.
# pip check verifies that the installed package requirements are consistent.
# Backslashes continue one instruction onto the next line.
# && runs the next command only if the preceding command succeeded.
RUN python -m venv /opt/venv \
    && sed '/^[[:space:]]*pytest[[:space:]]*$/d' requirements.txt > runtime-requirements.txt \
    && /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r runtime-requirements.txt \
    && /opt/venv/bin/pip check

# STEP 4: Start the final runtime stage from the same Linux/Python base.
# This is a fresh stage. Files from the previous stage are not included unless
# explicitly copied. The temporary build files therefore stay out of the image.
FROM ${PYTHON_IMAGE} AS runtime

# STEP 5: Set environment variables for the running application.
# PATH: use the virtual environment's python and installed tools by default.
# PYTHONUNBUFFERED: show Python logs immediately in docker logs.
# PYTHONDONTWRITEBYTECODE: do not create .pyc files next to application code.
# MPLBACKEND=Agg: render Matplotlib plots without a graphical desktop.
# MPLCONFIGDIR: place Matplotlib's configuration/cache in writable /tmp.
# HOME: direct software that uses the home directory to writable /tmp.
# These variables remain available when the container starts.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/tmp
# All application paths below are relative to /app unless absolute paths are used.
WORKDIR /app

# STEP 6: Create a dedicated Linux user and a writable data directory.
# Build instructions run as root until the USER instruction below.
# The group and user both have fixed numeric IDs of 10001.
# The user has no home directory and no interactive login shell.
# mkdir creates the location for the database, reports and simulator state.
# chown allows the application user to write to that directory.
# This affects the image directory; a host bind mount needs its own permissions.
RUN groupadd --gid 10001 imf \
    && useradd --uid 10001 --gid imf --no-create-home --no-log-init \
       --shell /usr/sbin/nologin imf \
    && mkdir -p /app/data \
    && chown imf:imf /app/data

# STEP 7: Copy the installed packages and application into the final image.
# --from=dependencies reads from the earlier stage rather than the host.
COPY --from=dependencies /opt/venv /opt/venv
# Copy only the application folders and files needed to run the demo.
# Application code and dependencies stay root-owned, so the application user
# cannot modify them. Templates and static files are included in these folders.
COPY imf/ ./imf/
COPY pdgs_sim/ ./pdgs_sim/
COPY config/ ./config/
COPY run_demo.py ./

# STEP 8: Create a small Python launcher directly inside the image.
# COPY <<'PY' is a here-document: everything until the standalone PY line
# becomes the contents of /app/container_start.py. The quoted delimiter keeps
# Docker from expanding variables inside this Python code.
# Docker writes this file during the build. Python runs it on container start.
# It adapts the configuration without changing the project's Python files.
COPY <<'PY' /app/container_start.py
import os
from pathlib import Path
import secrets
import sys

import yaml

# New files should be accessible only to their owner (e.g. the session key).
os.umask(0o077)
# Read an explicitly supplied configuration, or the bundled YAML by default.
source = Path(os.environ.get("IMF_CONFIG", "/app/config/imf.yaml"))
config = yaml.safe_load(source.read_text(encoding="utf-8"))
web = config.setdefault("web", {})
# Listen on all container network interfaces. With 127.0.0.1 here, Docker's
# port forwarding could not reach the portal from outside the container.
# Host access is still controlled separately by docker run -p.
web["host"] = "0.0.0.0"
web["port"] = 8000

# Generate a random key for signing login sessions on first container start.
# Keep it with persistent data so restarts can reuse it.
# The key is created at runtime, never baked into a shared image.
key_file = Path("/app/data/session.key")
if not key_file.exists():
    # "x" creates a new file exclusively instead of overwriting an existing key.
    with key_file.open("x", encoding="utf-8") as handle:
        handle.write(secrets.token_urlsafe(48))
web["secret_key"] = key_file.read_text(encoding="utf-8").strip()

# Write the adapted configuration to /tmp, leaving the bundled YAML untouched.
# IMF_CONFIG tells the application's configuration loader to use this file.
runtime_config = Path("/tmp/imf-container.yaml")
runtime_config.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
os.environ["IMF_CONFIG"] = str(runtime_config)

# Replace this launcher process with the project's existing demo launcher.
# run_demo.py starts BOTH the PDGS simulator and the IMF portal.
# Extra container arguments, such as --days 3, are forwarded unchanged.
# run_demo.py catches KeyboardInterrupt and stops both child processes.
os.execv(sys.executable, [sys.executable, "/app/run_demo.py", *sys.argv[1:]])
PY

# STEP 9: Drop root privileges for subsequent instructions and container startup.
# USER specifies the numeric Linux user ID and group ID (UID:GID).
USER 10001:10001

# Document the portal's container port. EXPOSE does not publish a host port.
# Use docker run -p 127.0.0.1:8000:8000 to make it reachable locally.
# The simulator's port 8100 is used internally and does not need publication.
EXPOSE 8000

# Mark /app/data as a data-volume location rather than disposable container data.
# Use a named volume in docker run so it can easily be reused after recreating
# the container. Without an explicit mount, Docker creates an anonymous volume.
# Do not use run_demo.py --reset on this mount: it tries to delete the mountpoint.
VOLUME ["/app/data"]

# docker stop sends SIGINT, equivalent to Ctrl+C for the Python launcher.
# This activates its existing cleanup logic. Use --init when starting the
# container to forward signals and reap exited child processes.
# --stop-timeout 30 gives the launcher time to stop both child services.
STOPSIGNAL SIGINT

# STEP 10: Check that both HTTP services respond.
# Docker runs this command every 30 seconds with a 5-second overall timeout.
# Each HTTP request has its own 2-second timeout.
# Startup failures receive a 120-second grace period; 3 consecutive counted
# failures mark the container unhealthy. Success marks it healthy.
# This checks service availability, not completion of the initial ingestion.
# Docker does not automatically restart a container just because it is unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=2); urllib.request.urlopen('http://127.0.0.1:8100/status', timeout=2)"]

# STEP 11: Define what runs when a container starts.
# The JSON array is the exec form: Docker starts Python directly, without a shell.
# Arguments after the image name in docker run are passed to this launcher.
ENTRYPOINT ["python", "/app/container_start.py"]
# No additional default arguments; run_demo.py uses its own defaults.
CMD []

# Runtime protections are supplied by the start command in DOCKER.md:
#   --read-only: protect the container's root filesystem;
#   --tmpfs /tmp: provide temporary writable storage;
#   --cap-drop ALL: remove additional Linux capabilities;
#   --security-opt no-new-privileges:true: prevent gaining higher privileges.
# Dockerfile instructions alone do not enforce these docker run options.
# The demo's default account passwords remain unchanged for local testing.
