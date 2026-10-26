Docker for running test suite
=============================

- Pushed to docker hub as `opendatacube/datacube-tests`

Example Use:

```shell
git clone https://github.com/opendatacube/datacube-core.git
cd datacube-core
docker run --rm \
  -v $(pwd):/code \
  opendatacube/datacube-tests:latest \
  ./check-code.sh integration_tests
```

## Running the integration tests on the host (fast local iteration)

If you want to run the integration test suite with your host Python interpreter
(e.g. via `uv run pytest`) rather than inside the full test image, use the
`local-test-db.sh` helper to start a throwaway PostgreSQL + PostGIS database:

```shell
# Start an in-memory (tmpfs) Postgres+PostGIS container and wait for it.
./docker/local-test-db.sh up

# Export the ODC_*_DB_URL variables the tests expect.
eval "$(./docker/local-test-db.sh env)"

# Run the integration tests (-o addopts="" skips the --doctest-modules default).
uv run pytest integration_tests -o addopts=""

# Tear the database down when finished.
./docker/local-test-db.sh down
```

The helper defaults to the arm64-compatible `imresamu/postgis:18-3.6-alpine3.23`
image, since the official `postgis/postgis` images do not publish arm64
manifests yet. Override the image with `ODC_TEST_PG_IMAGE` (on amd64 you may
prefer `postgis/postgis:18-3.6`), and the port/credentials with the other
`ODC_TEST_PG_*` variables documented at the top of the script.

Building the `psycopg2` wheel against Python 3.14 requires the libpq and Python
development headers on the host (Debian/Ubuntu: `libpq-dev` and the matching
`pythonX.Y-dev`, e.g. `python3.14-dev`).

## Updating Dependencies

Run the command:

```shell
docker run --rm \
  -v $(pwd):/code -w /code \
  -it opendatacube/datacube-tests \
  bash -c "uv pip compile --all-extras --group dev --group doc --output-file=docker/constraints.txt pyproject.toml"
```
