#!/usr/bin/env bash
# Emit sanitized diagnostics for the disposable #487 PostgreSQL service.
set -uo pipefail

container="${1:?usage: diagnose-test-postgres.sh CONTAINER [DATABASE]}"
requested_database="${2:-}"
if [[ ! "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "invalid PostgreSQL test container" >&2
  exit 2
fi
if [ -n "$requested_database" ] \
  && [[ ! "$requested_database" =~ ^comet_test_[0-9a-f]{32}$ ]]; then
  echo "invalid retained PostgreSQL test database" >&2
  exit 2
fi
for variable in COMET_POSTGRES_USER COMET_POSTGRES_DB COMET_POSTGRES_PASSWORD; do
  if [ -z "${!variable:-}" ]; then
    echo "missing PostgreSQL diagnostic variable: $variable" >&2
    exit 2
  fi
done
if [[ ! "$COMET_POSTGRES_USER" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] \
  || [[ ! "$COMET_POSTGRES_DB" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] \
  || [[ "$COMET_POSTGRES_PASSWORD" == *$'\n'* ]] \
  || [[ "$COMET_POSTGRES_PASSWORD" == *$'\r'* ]]; then
  echo "invalid PostgreSQL diagnostic identity" >&2
  exit 2
fi

if ! runtime_output=$(python3 "$(dirname "${BASH_SOURCE[0]}")/ci/postgres_test_runtime.py"); then
  exit 2
fi
mapfile -t runtime <<<"$runtime_output"
status=0
"${runtime[@]}" inspect --format \
  'image={{.Config.Image}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} ports={{json .NetworkSettings.Ports}}' \
  "$container" || status=1
if image_id=$("${runtime[@]}" inspect --format '{{.Image}}' "$container" 2>/dev/null) \
  && [[ "$image_id" =~ ^(sha256:)?[0-9a-f]{64}$ ]]; then
  if platform=$("${runtime[@]}" image inspect --format '{{.Os}}/{{.Architecture}}' \
      "$image_id" 2>/dev/null) \
    && [[ "$platform" =~ ^[a-z0-9][a-z0-9_.-]{0,63}/[a-z0-9][a-z0-9_.-]{0,63}$ ]]; then
    printf 'platform=%s\n' "$platform"
  else
    echo "Cannot read the PostgreSQL image platform." >&2
    status=1
  fi
else
  echo "Cannot read the PostgreSQL image identity." >&2
  status=1
fi
"${runtime[@]}" logs --tail 500 "$container" 2>&1 || status=1

export PGPASSWORD="$COMET_POSTGRES_PASSWORD"
psql=(
  "${runtime[@]}" exec --env PGPASSWORD "$container"
  psql --username "$COMET_POSTGRES_USER" --dbname "$COMET_POSTGRES_DB"
  --set ON_ERROR_STOP=1 --tuples-only --no-align
)
if [ -n "$requested_database" ]; then
  databases=("$requested_database")
else
  databases=()
  if database_output=$("${psql[@]}" --command \
      "SELECT datname FROM pg_database WHERE datname ~ '^comet_test_[0-9a-f]{32}$' ORDER BY datname"); then
    if [ -n "$database_output" ]; then
      mapfile -t databases <<<"$database_output"
    fi
  else
    status=1
  fi
fi

for database in "${databases[@]}"; do
  if [[ ! "$database" =~ ^comet_test_[0-9a-f]{32}$ ]]; then
    echo "unsafe database returned by PostgreSQL catalog" >&2
    status=1
    continue
  fi
  echo "database=$database"
  if ledger_exists=$("${runtime[@]}" exec --env PGPASSWORD "$container" \
      psql --username "$COMET_POSTGRES_USER" --dbname "$database" \
      --set ON_ERROR_STOP=1 --tuples-only --no-align --command \
      "SELECT to_regclass('public.schema_migrations') IS NOT NULL"); then
    case "$ledger_exists" in
      t)
        "${runtime[@]}" exec --env PGPASSWORD "$container" \
          psql --username "$COMET_POSTGRES_USER" --dbname "$database" \
          --set ON_ERROR_STOP=1 --command \
          "SELECT version, applied_at FROM public.schema_migrations ORDER BY version" || status=1
        ;;
      f) echo "Migration ledger is missing." ;;
      *)
        echo "The migration ledger query result is incorrect." >&2
        status=1
        ;;
    esac
  else
    status=1
  fi
  "${runtime[@]}" exec --env PGPASSWORD "$container" \
    psql --username "$COMET_POSTGRES_USER" --dbname "$database" \
    --set ON_ERROR_STOP=1 --command \
    "SELECT n.nspname, c.relkind, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' ORDER BY 1,2,3" || status=1
  "${runtime[@]}" exec --env PGPASSWORD "$container" \
    pg_dump --schema-only --no-owner --no-privileges \
    --username "$COMET_POSTGRES_USER" --dbname "$database" || status=1
done
exit "$status"
