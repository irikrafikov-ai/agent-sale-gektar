# Рабочее место Ирика на Railway: Claude Code в контейнере, вход по SSH.
#
# Зачем отдельный сервис. Контейнеры агента стираются при каждом пуше в main
# (3–4 раза в день), и всё, что поставлено руками, живёт до ближайшего
# деплоя. Здесь Claude Code — часть образа, а его логин и сессии лежат на
# постоянном диске (/data), который переживает любые пересборки.
#
# Публичного домена у сервиса нет и не нужно: вход только через
# `railway ssh -s workbench --session`.

FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git jq ripgrep tmux openssh-client less procps \
      python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Логин, настройки и сессии Claude Code — на томе, а не в контейнере.
ENV CLAUDE_CONFIG_DIR=/data/claude
ENV HOME=/root
ENV TERM=xterm-256color
ENV LANG=C.UTF-8

COPY start.sh /usr/local/bin/start.sh
RUN chmod +x /usr/local/bin/start.sh

WORKDIR /data/work
CMD ["/usr/local/bin/start.sh"]
