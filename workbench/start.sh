#!/bin/sh
# Контейнер ничего не делает сам — только держится живым для SSH.
set -e
mkdir -p /data/claude /data/work
cd /data/work
echo "workbench готов: railway ssh -s workbench --session → claude"
echo "claude: $(claude --version 2>/dev/null || echo 'не найден')"
exec sleep infinity
