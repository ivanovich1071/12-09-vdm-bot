FROM python:3.12-slim

# Часовой пояс: журнал диалогов, имена файлов предзаказов и отчёты менеджеру идут
# по московскому времени — иначе ночной прогон выглядит дневным.
ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime && echo "${TZ}" > /etc/timezone

WORKDIR /app

# Зависимости ставятся отдельным слоем: пересборка после правки кода не тянет
# заново весь pip.
#
# Список — то, что рабочий код действительно импортирует:
#   aiogram              — Telegram-канал;
#   aiohttp-socks        — транзит до Telegram (TELEGRAM_PROXY); без него aiogram
#                          с заданным транзитом падает на импорте;
#   fastapi, uvicorn     — виджет, Mini App и Core API;
#   pydantic             — схемы Core API;
#   pypdf                — реестр приказов и заказ клиента, присланный в PDF;
#   gspread, google-auth — только при ORDER_SINK=google_sheets.
# Заявка почтой (ORDER_SINK=smtp) обходится стандартной библиотекой — smtplib.
# Остальное из pyproject.toml (postgres, redis, sqlalchemy, openai) прототип не
# использует: хранилище — sqlite, к модели код ходит через urllib. На ВМ этих
# служб нет, и в образе они значат лишь сборку asyncpg ради мёртвого кода.
RUN pip install --no-cache-dir \
    "aiogram>=3.13,<4" \
    "aiohttp-socks>=0.9,<1" \
    "fastapi>=0.115,<1" \
    "uvicorn[standard]>=0.30,<1" \
    "pydantic>=2.8,<3" \
    "pypdf>=5.0,<7" \
    "gspread>=6.1,<7" \
    "google-auth>=2.34,<3"

COPY run.py ./
COPY src ./src

CMD ["python", "run.py", "widget"]
