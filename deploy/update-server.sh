#!/usr/bin/env bash

set -Eeuo pipefail

readonly IMAGE="ghcr.io/zfh970745204/sub2api-image-studio"
readonly DEPLOY_DIR="${SUB2IMAGE_DEPLOY_DIR:-/opt/sub2api-image-studio-release-344e9d5}"
readonly ENV_FILE="${DEPLOY_DIR}/.env"
readonly COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.server.yml"
readonly HEALTH_TIMEOUT_SECONDS="${SUB2IMAGE_HEALTH_TIMEOUT_SECONDS:-180}"

compose=(docker compose --project-directory "${DEPLOY_DIR}" -f "${COMPOSE_FILE}")
env_backup=""
env_changed=0
previous_tag=""

cleanup() {
    if [[ -n "${env_backup}" && -f "${env_backup}" ]]; then
        rm -f -- "${env_backup}"
    fi
}

rollback() {
    local exit_code=$?
    trap - ERR
    if [[ "${env_changed}" == "1" && -n "${env_backup}" && -f "${env_backup}" ]]; then
        echo "更新失败，正在恢复原镜像标签：${previous_tag:-未设置}"
        cp -p -- "${env_backup}" "${ENV_FILE}"
        "${compose[@]}" up -d --no-build web worker scheduler || true
    fi
    echo "更新未完成，当前业务数据没有被删除。" >&2
    exit "${exit_code}"
}

trap cleanup EXIT
trap rollback ERR

for command in docker grep sed mktemp flock; do
    if ! command -v "${command}" >/dev/null 2>&1; then
        echo "缺少必要命令：${command}" >&2
        exit 1
    fi
done

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "找不到配置文件：${ENV_FILE}" >&2
    exit 1
fi
if [[ ! -f "${COMPOSE_FILE}" ]]; then
    echo "找不到部署文件：${COMPOSE_FILE}" >&2
    exit 1
fi

exec 9>"${DEPLOY_DIR}/.sub2image-update.lock"
if ! flock -n 9; then
    echo "已有更新任务正在运行，请稍后再试。" >&2
    exit 1
fi

echo "[1/5] 下载最新稳定镜像"
docker pull "${IMAGE}:latest"

revision="$({
    docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "${IMAGE}:latest"
} 2>/dev/null)"
if [[ ! "${revision}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "镜像缺少有效的版本标识，已停止更新。" >&2
    exit 1
fi
target_tag="sha-${revision}"
docker tag "${IMAGE}:latest" "${IMAGE}:${target_tag}"

previous_tag="$(grep -m1 '^IMAGE_TAG=' "${ENV_FILE}" | cut -d= -f2- || true)"
if [[ "${previous_tag}" == "${target_tag}" ]]; then
    echo "当前已是最新版本：${target_tag}"
else
    echo "[2/5] 切换版本：${previous_tag:-未设置} -> ${target_tag}"
fi

env_backup="$(mktemp)"
chmod 600 "${env_backup}"
cp -p -- "${ENV_FILE}" "${env_backup}"
if grep -q '^IMAGE_TAG=' "${ENV_FILE}"; then
    sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=${target_tag}/" "${ENV_FILE}"
else
    printf '\nIMAGE_TAG=%s\n' "${target_tag}" >>"${ENV_FILE}"
fi
chmod 600 "${ENV_FILE}"
env_changed=1

echo "[3/5] 检查部署配置"
"${compose[@]}" config --quiet

echo "[4/5] 执行数据库迁移并更新服务"
"${compose[@]}" up -d --no-build web worker scheduler

echo "[5/5] 等待服务健康"
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
    web_id="$("${compose[@]}" ps -q web)"
    if [[ -n "${web_id}" ]]; then
        web_health="$(
            docker inspect \
                --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                "${web_id}" 2>/dev/null || true
        )"
        running_services="$("${compose[@]}" ps --status running --services)"
        if [[ "${web_health}" == "healthy" ]] \
            && grep -qx 'worker' <<<"${running_services}" \
            && grep -qx 'scheduler' <<<"${running_services}"; then
            env_changed=0
            echo "更新成功：${target_tag}"
            "${compose[@]}" ps
            exit 0
        fi
    fi
    sleep 3
done

echo "服务未在 ${HEALTH_TIMEOUT_SECONDS} 秒内全部恢复健康。" >&2
false
