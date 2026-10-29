#!/bin/sh
# Колёса для офлайн-сборки образа. Список зависимостей — из pyproject.toml
# (scripts/docker_requirements.py), поэтому монтируется корень репозитория:
#
#   docker run --rm -v "$PWD:/repo" -w /repo python:3.12-slim sh docker-wheels/fetch.sh
#
# На Windows — из PowerShell с полным путём (-v D:\путь\до\ariadna:/repo):
# Git Bash искажает пути в -v.
#
# Колёса скачиваются во временный каталог и заменяют содержимое
# docker-wheels/ только после успешной загрузки всего набора: старые версии
# удаляются, а при сбое сети прежний набор остаётся на месте.
set -e
cd "$(dirname "$0")/.."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/wheels"

python scripts/docker_requirements.py download > "$tmp/download.txt"
python scripts/docker_requirements.py git > "$tmp/git.txt"

pip download --dest "$tmp/wheels" --quiet --retries 5 --timeout 120 -r "$tmp/download.txt"

# Зависимости по прямой ссылке (git+https://…) — собрать колесо без зависимостей.
if [ -s "$tmp/git.txt" ]; then
  apt-get update -qq >/dev/null
  apt-get install -y -qq git >/dev/null
  while read -r url; do
    pip wheel --no-deps -w "$tmp/wheels" --quiet "$url"
  done < "$tmp/git.txt"
fi

# Всё скачано — заменить прежний набор.
find docker-wheels -maxdepth 1 -type f \
  \( -name '*.whl' -o -name '*.tar.gz' -o -name '*.zip' \) -delete
mv "$tmp"/wheels/* docker-wheels/
echo "docker-wheels: $(ls docker-wheels | grep -vc '^fetch.sh$') файлов"
