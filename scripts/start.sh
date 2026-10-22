#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

show_diagnostics() {
    docker compose ps -a
    docker compose logs --tail=80 mysql redis migrate policy-rag-db-init backend policy-rag frontend
}

docker compose config -q
if ! docker compose up -d --build; then
    echo "Compose startup failed. Recent service logs:" >&2
    show_diagnostics
    exit 1
fi

docker compose ps

# The RAG service loads two models before its HTTP server becomes ready.
deadline=$((SECONDS + 600))
while ((SECONDS < deadline)); do
    ready=true
    failed=false

    for service in mysql redis backend policy-rag frontend; do
        container_id=$(docker compose ps -a -q "$service")
        if [[ -z "$container_id" ]]; then
            ready=false
            continue
        fi

        state=$(docker inspect --format '{{.State.Status}}' "$container_id")
        health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")
        if [[ "$state" == "exited" || "$state" == "dead" ]]; then
            echo "$service stopped unexpectedly ($state)." >&2
            failed=true
        elif [[ "$state" != "running" || "$health" != "healthy" ]]; then
            ready=false
        fi
    done

    for service in migrate policy-rag-db-init; do
        container_id=$(docker compose ps -a -q "$service")
        if [[ -z "$container_id" ]]; then
            ready=false
            continue
        fi

        state=$(docker inspect --format '{{.State.Status}}' "$container_id")
        exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$container_id")
        if [[ "$state" == "exited" && "$exit_code" != "0" ]]; then
            echo "$service failed with exit code $exit_code." >&2
            failed=true
        elif [[ "$state" != "exited" ]]; then
            ready=false
        fi
    done

    if [[ "$failed" == true ]]; then
        show_diagnostics
        exit 1
    fi
    if [[ "$ready" == true ]]; then
        docker compose ps
        echo "All long-running services are healthy; one-time tasks completed successfully."
        exit 0
    fi
    sleep 10
done

echo "Timed out waiting for Compose services to become healthy." >&2
show_diagnostics
exit 1
