#!/usr/bin/env bash
# Configure the disposable #487 PostgreSQL container for encrypted TLS.
set -euo pipefail

container="${1:?usage: configure-test-postgres-tls.sh CONTAINER CA_OUTPUT}"
ca_output="${2:?usage: configure-test-postgres-tls.sh CONTAINER CA_OUTPUT}"
if [[ ! "$container" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "invalid PostgreSQL test container" >&2
  exit 2
fi
for variable in \
  COMET_POSTGRES_IMAGE COMET_POSTGRES_USER COMET_POSTGRES_DB COMET_POSTGRES_PASSWORD; do
  if [ -z "${!variable:-}" ]; then
    echo "missing PostgreSQL test variable: $variable" >&2
    exit 2
  fi
done
if [[ ! "$COMET_POSTGRES_USER" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] \
  || [[ ! "$COMET_POSTGRES_DB" =~ ^[a-z_][a-z0-9_]{0,62}$ ]]; then
  echo "invalid PostgreSQL test user or database" >&2
  exit 2
fi
if [[ "$COMET_POSTGRES_PASSWORD" == *$'\n'* || "$COMET_POSTGRES_PASSWORD" == *$'\r'* ]]; then
  echo "invalid PostgreSQL test password" >&2
  exit 2
fi
if [ -e "$ca_output" ] || [ ! -d "$(dirname "$ca_output")" ]; then
  echo "CA output must be a new file in an existing directory" >&2
  exit 2
fi
reviewed_image="postgres:17.11-alpine3.24@sha256:7456ef82e5f5bc43d997f4781bbd7c0d6389bff397564649a356e206ba473aee"
canonical_image="postgres@sha256:7456ef82e5f5bc43d997f4781bbd7c0d6389bff397564649a356e206ba473aee"
if [ "$COMET_POSTGRES_IMAGE" != "$reviewed_image" ]; then
  echo "PostgreSQL test image does not match the reviewed digest" >&2
  exit 2
fi
if ! runtime_output=$(python3 "$(dirname "${BASH_SOURCE[0]}")/ci/postgres_test_runtime.py"); then
  exit 2
fi
mapfile -t runtime <<<"$runtime_output"
actual_image=$("${runtime[@]}" inspect --type container --format '{{.Config.Image}}' "$container")
normalized_image="${actual_image#docker.io/library/}"
if [ "$normalized_image" != "$reviewed_image" ] && [ "$normalized_image" != "$canonical_image" ]; then
  echo "PostgreSQL test container image or platform does not match the reviewed pin" >&2
  exit 2
fi
actual_platform=$("${runtime[@]}" image inspect --format '{{.Os}}/{{.Architecture}}' "$actual_image")
if [ "$actual_platform" != "linux/amd64" ]; then
  echo "PostgreSQL test container image or platform does not match the reviewed pin" >&2
  exit 2
fi

work=$(mktemp -d)
cleanup() {
  rm -rf -- "$work"
}
trap cleanup EXIT

openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout "$work/ca.key" -out "$work/ca.crt" \
  -subj '/CN=Comet PostgreSQL Test CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' >/dev/null 2>&1
openssl req -new -newkey rsa:2048 -nodes \
  -keyout "$work/server.key" -out "$work/server.csr" \
  -subj '/CN=localhost' \
  -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
  -addext 'extendedKeyUsage=serverAuth' >/dev/null 2>&1
openssl x509 -req -days 1 -in "$work/server.csr" \
  -CA "$work/ca.crt" -CAkey "$work/ca.key" -CAcreateserial \
  -copy_extensions copy -out "$work/server.crt" >/dev/null 2>&1
install -m 0644 "$work/ca.crt" "$ca_output"
"${runtime[@]}" cp "$work/server.crt" "$container:/var/lib/postgresql/data/server.crt"
"${runtime[@]}" cp "$work/server.key" "$container:/var/lib/postgresql/data/server.key"
"${runtime[@]}" exec --user root "$container" \
  chown postgres:postgres /var/lib/postgresql/data/server.crt /var/lib/postgresql/data/server.key
"${runtime[@]}" exec --user root "$container" chmod 0644 /var/lib/postgresql/data/server.crt
"${runtime[@]}" exec --user root "$container" chmod 0600 /var/lib/postgresql/data/server.key

export PGPASSWORD="$COMET_POSTGRES_PASSWORD"
psql=(
  "${runtime[@]}" exec --env PGPASSWORD "$container"
  psql --username "$COMET_POSTGRES_USER" --dbname "$COMET_POSTGRES_DB"
  --set ON_ERROR_STOP=1
)
"${psql[@]}" --command "ALTER SYSTEM SET ssl = 'on'" >/dev/null
"${psql[@]}" --command "ALTER SYSTEM SET ssl_cert_file = '/var/lib/postgresql/data/server.crt'" >/dev/null
"${psql[@]}" --command "ALTER SYSTEM SET ssl_key_file = '/var/lib/postgresql/data/server.key'" >/dev/null
"${runtime[@]}" restart "$container" >/dev/null

for _attempt in $(seq 1 60); do
  if "${runtime[@]}" exec --env PGPASSWORD --env PGSSLMODE=require \
      "$container" psql --host 127.0.0.1 --username "$COMET_POSTGRES_USER" \
      --dbname "$COMET_POSTGRES_DB" --tuples-only --no-align \
      --command "SELECT current_setting('ssl') || ':' || ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()" \
      2>/dev/null | grep -qx 'on:true'; then
    echo "PostgreSQL TLS is ready"
    exit 0
  fi
  sleep 1
done

echo "PostgreSQL TLS readiness timed out" >&2
"${runtime[@]}" inspect --format '{{.Config.Image}} {{.Platform}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
  "$container" >&2 || true
exit 1
