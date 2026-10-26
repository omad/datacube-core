#!/usr/bin/env bash
#
# Start/stop a local PostgreSQL + PostGIS container for running the
# datacube-core integration tests directly on the host (outside of
# docker-compose).
#
# This is handy for fast local iteration: the test suite runs on the host
# Python interpreter and talks to a throwaway, in-memory (tmpfs) database.
#
# The default image is an arm64-compatible PostGIS build, because the official
# `postgis/postgis` images do not (yet) publish arm64 manifests. Override it
# with ODC_TEST_PG_IMAGE if you want a different build (e.g. on amd64 you may
# prefer `postgis/postgis:18-3.6`).
#
# Usage:
#   ./docker/local-test-db.sh up        # pull + start the container, wait for ready
#   ./docker/local-test-db.sh down      # stop and remove the container
#   ./docker/local-test-db.sh env       # print the ODC_*_DB_URL exports
#   ./docker/local-test-db.sh psql      # open a psql shell in the container
#
# Typical workflow:
#   ./docker/local-test-db.sh up
#   eval "$(./docker/local-test-db.sh env)"
#   uv run pytest integration_tests -o addopts=""
#   ./docker/local-test-db.sh down
#
# Host prerequisites for building the psycopg2 wheel against Python 3.14:
#   - libpq development headers (Debian/Ubuntu: `libpq-dev`)
#   - Python development headers matching your interpreter
#     (Debian/Ubuntu: e.g. `python3.14-dev`)

set -euo pipefail

# ----------------------------------------------------------------------------
# Configuration (override via environment variables).
# ----------------------------------------------------------------------------
ODC_TEST_PG_IMAGE="${ODC_TEST_PG_IMAGE:-docker.io/imresamu/postgis:18-3.6-alpine3.23}"
ODC_TEST_PG_CONTAINER="${ODC_TEST_PG_CONTAINER:-odc-test-pg}"
ODC_TEST_PG_PORT="${ODC_TEST_PG_PORT:-35432}"
ODC_TEST_PG_USER="${ODC_TEST_PG_USER:-opendatacube}"
ODC_TEST_PG_PASSWORD="${ODC_TEST_PG_PASSWORD:-opendatacubepassword}"
ODC_TEST_PG_HOST="${ODC_TEST_PG_HOST:-localhost}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INIT_SCRIPT="${SCRIPT_DIR}/assets/create-pg-databases.sh"

_base_url() {
    # $1 = sqlalchemy scheme (postgresql or postgresql+psycopg), $2 = db name
    echo "$1://${ODC_TEST_PG_USER}:${ODC_TEST_PG_PASSWORD}@${ODC_TEST_PG_HOST}:${ODC_TEST_PG_PORT}/$2"
}

cmd_env() {
    # psycopg2 (v2) for the `datacube`/`postgis` environments, psycopg (v3) for
    # the `datacube3`/`postgis3` environments. These mirror docker-compose.yml.
    echo "export ODC_DATACUBE_DB_URL=\"$(_base_url postgresql pgintegration)\""
    echo "export ODC_POSTGIS_DB_URL=\"$(_base_url postgresql pgisintegration)\""
    echo "export ODC_DATACUBE3_DB_URL=\"$(_base_url postgresql+psycopg pgintegration)\""
    echo "export ODC_POSTGIS3_DB_URL=\"$(_base_url postgresql+psycopg pgisintegration)\""
}

cmd_up() {
    echo "Pulling ${ODC_TEST_PG_IMAGE} ..." >&2
    docker pull "${ODC_TEST_PG_IMAGE}" >/dev/null

    docker rm -f "${ODC_TEST_PG_CONTAINER}" >/dev/null 2>&1 || true

    echo "Starting container ${ODC_TEST_PG_CONTAINER} on port ${ODC_TEST_PG_PORT} ..." >&2
    # NB: PostgreSQL 18 expects a single mount at /var/lib/postgresql (the data
    # lives in a versioned subdirectory). Using a tmpfs keeps the throwaway test
    # data in RAM, and the tuning flags below trade durability/background work
    # for speed (fine for tests): no fsync/WAL safety, minimal WAL, no autovacuum,
    # and more memory for the DDL-heavy schema setup the tests perform.
    docker run -d \
        --name "${ODC_TEST_PG_CONTAINER}" \
        -e POSTGRES_DB=opendatacube \
        -e PGPORT="${ODC_TEST_PG_PORT}" \
        -e POSTGRES_USER="${ODC_TEST_PG_USER}" \
        -e POSTGRES_PASSWORD="${ODC_TEST_PG_PASSWORD}" \
        -p "${ODC_TEST_PG_PORT}:${ODC_TEST_PG_PORT}" \
        -v "${INIT_SCRIPT}:/docker-entrypoint-initdb.d/create-pg-databases.sh:ro" \
        --tmpfs /var/lib/postgresql \
        "${ODC_TEST_PG_IMAGE}" \
        -c fsync=off \
        -c full_page_writes=off \
        -c synchronous_commit=off \
        -c wal_level=minimal \
        -c max_wal_senders=0 \
        -c autovacuum=off \
        -c shared_buffers=256MB \
        -c work_mem=16MB \
        -c maintenance_work_mem=256MB >/dev/null

    echo -n "Waiting for PostgreSQL to accept connections " >&2
    for _ in $(seq 1 60); do
        if docker exec "${ODC_TEST_PG_CONTAINER}" \
            pg_isready -q -p "${ODC_TEST_PG_PORT}" -U "${ODC_TEST_PG_USER}" -d pgintegration >/dev/null 2>&1; then
            echo " ready." >&2
            echo >&2
            echo "Run the integration tests with:" >&2
            echo "  eval \"\$(${BASH_SOURCE[0]} env)\"" >&2
            echo "  uv run pytest integration_tests -o addopts=\"\"" >&2
            return 0
        fi
        echo -n "." >&2
        sleep 1
    done

    echo >&2
    echo "ERROR: PostgreSQL did not become ready in time. Logs:" >&2
    docker logs "${ODC_TEST_PG_CONTAINER}" >&2
    return 1
}

cmd_down() {
    echo "Removing container ${ODC_TEST_PG_CONTAINER} ..." >&2
    docker rm -f "${ODC_TEST_PG_CONTAINER}" >/dev/null 2>&1 || true
}

cmd_psql() {
    exec docker exec -it "${ODC_TEST_PG_CONTAINER}" \
        psql -U "${ODC_TEST_PG_USER}" -p "${ODC_TEST_PG_PORT}" -d pgintegration
}

case "${1:-}" in
    up)   cmd_up ;;
    down) cmd_down ;;
    env)  cmd_env ;;
    psql) cmd_psql ;;
    *)
        echo "Usage: $0 {up|down|env|psql}" >&2
        exit 2
        ;;
esac
