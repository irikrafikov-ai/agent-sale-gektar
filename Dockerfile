# Агент по продажам на Авито — образ для Railway.
# Nixpacks не определяет язык у репозитория из Markdown, поэтому сборка явная.

FROM python:3.12-slim

# git — для подтягивания базы знаний перед каждым прогоном.
# nodejs — Claude Agent SDK работает поверх CLI Claude Code.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y curl gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY сервис/requirements.txt сервис/requirements.txt
RUN pip install --no-cache-dir -r сервис/requirements.txt

# Регламент и рабочая память. База знаний сюда НЕ копируется —
# она тянется из своего репозитория на старте каждого прогона.
COPY AGENT.md README.md ./
COPY регламент/ регламент/
COPY данные/ данные/
COPY сервис/ сервис/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/сервис \
    KB_PATH=/opt/база-знаний

# Аргумент — вид прогона: утро | вечер.
# Railway передаёт его в команде расписания.
ENTRYPOINT ["python", "сервис/прогон.py"]
CMD ["вечер"]
