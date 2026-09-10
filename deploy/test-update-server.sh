#!/usr/bin/env bash

set -Eeuo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

mock_bin="${test_root}/bin"
deploy_dir="${test_root}/release"
mkdir -p "${mock_bin}" "${deploy_dir}"
cp "${project_root}/docker-compose.server.yml" "${deploy_dir}/docker-compose.server.yml"

cat >"${mock_bin}/flock" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

cat >"${mock_bin}/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"${MOCK_DOCKER_LOG}"

if [[ "$*" == "image inspect "* ]]; then
    printf '%s\n' "${MOCK_REVISION}"
    exit 0
fi
if [[ "$1" == "inspect" ]]; then
    printf 'healthy\n'
    exit 0
fi
if [[ "$1" != "compose" ]]; then
    exit 0
fi

for ((index = 1; index <= $#; index++)); do
    argument="${!index}"
    if [[ "${argument}" == "config" && "${MOCK_CONFIG_FAIL:-0}" == "1" ]]; then
        exit 1
    fi
    if [[ "${argument}" == "ps" ]]; then
        remaining="${*:index}"
        if [[ "${remaining}" == "ps -q web" ]]; then
            printf 'web-container\n'
        elif [[ "${remaining}" == "ps --status running --services" ]]; then
            printf 'web\nworker\nscheduler\n'
        else
            printf 'mock services healthy\n'
        fi
        exit 0
    fi
done
exit 0
EOF

chmod +x "${mock_bin}/docker" "${mock_bin}/flock"

old_revision="1111111111111111111111111111111111111111"
new_revision="2222222222222222222222222222222222222222"
printf 'IMAGE_TAG=sha-%s\nPUBLIC_APP_URL=https://example.com\n' \
    "${old_revision}" >"${deploy_dir}/.env"
chmod 600 "${deploy_dir}/.env"

docker_log="${test_root}/docker.log"
PATH="${mock_bin}:${PATH}" \
MOCK_DOCKER_LOG="${docker_log}" \
MOCK_REVISION="${new_revision}" \
SUB2IMAGE_DEPLOY_DIR="${deploy_dir}" \
    bash "${project_root}/deploy/update-server.sh"

grep -qx "IMAGE_TAG=sha-${new_revision}" "${deploy_dir}/.env"
grep -q 'pull ghcr.io/zfh970745204/sub2api-image-studio:latest' "${docker_log}"
grep -q 'up -d --no-build web worker scheduler' "${docker_log}"

printf 'IMAGE_TAG=sha-%s\nPUBLIC_APP_URL=https://example.com\n' \
    "${old_revision}" >"${deploy_dir}/.env"
: >"${docker_log}"
if PATH="${mock_bin}:${PATH}" \
    MOCK_DOCKER_LOG="${docker_log}" \
    MOCK_REVISION="${new_revision}" \
    MOCK_CONFIG_FAIL=1 \
    SUB2IMAGE_DEPLOY_DIR="${deploy_dir}" \
    bash "${project_root}/deploy/update-server.sh"; then
    echo "配置检查失败时更新器应返回失败。" >&2
    exit 1
fi

grep -qx "IMAGE_TAG=sha-${old_revision}" "${deploy_dir}/.env"
grep -q 'up -d --no-build web worker scheduler' "${docker_log}"
echo "server updater tests passed"
