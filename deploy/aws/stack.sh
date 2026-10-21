#!/usr/bin/env bash
set -euo pipefail

case ${1:-} in
    build|models|start|stop|status) ;;
    *) printf 'Usage: %s {build|models|start|stop|status}\n' "$0" >&2; exit 2 ;;
esac

# The same wrapper is used by systemd and by the initial build/model setup.
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
env_file=${ISOJAM_ENV_FILE:-/etc/isojam/runtime.env}
cd "$repo_dir"

if [[ ${1:-} == stop ]]; then
    # Compose interpolates required settings even for stop. Shutdown must still
    # work if the real env file is missing or damaged. This value never starts API.
    export ISOJAM_JWT_SECRET_KEY=unused-during-stop
    env_file=/dev/null
elif [[ ! -r $env_file ]]; then
    printf 'Missing readable runtime settings: %s\n' "$env_file" >&2
    exit 1
fi

compose=(docker compose --project-name isojam-aws --project-directory "$repo_dir"
    --env-file "$env_file" -f "$repo_dir/compose.yaml"
    -f "$repo_dir/deploy/aws/compose.aws.yaml")

case ${1:-} in
    build) "${compose[@]}" build ;;
    # Model preparation must not consume the caller's piped deployment script.
    models) "${compose[@]}" --profile setup run --rm --no-deps --interactive=false -T models ;;
    start) "${compose[@]}" up --detach --wait --wait-timeout 180 --no-build ;;
    # No timeout override: preserve the processor's 35-minute drain allowance.
    stop) "${compose[@]}" stop ;;
    status) "${compose[@]}" ps --all ;;
esac
