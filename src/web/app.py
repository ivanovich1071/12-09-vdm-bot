"""HTTP-слой: виджет для сайта и служебные ручки.

Виджет подключается к сайту одним тегом:

    <script src="https://<хост>/widget.js" defer></script>

Пока доступа к шаблону vdm.ru нет, прототип показывается на своей демо-странице
по адресу `/demo`.

    python -m web.app
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response as HttpResponse
from pydantic import BaseModel, Field

from core.app import build_engine
from core.config import Settings
from core.errors import DomainError
from core.ui import Button, Keyboard, Message, Response
from web.render import to_json

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
CHANNEL = "web"
# Сколько реплик показываем вернувшемуся. Больше окно виджета всё равно не вмещает,
# а тянуть весь разговор в браузер незачем.
HISTORY_SHOWN = 30
CONTINUED = "С возвращением. Переписка и корзина на месте — продолжим?"

# Идентификатор посетителя выдаёт сервер — `uuid4().hex`, — и формат сверяется на
# каждом запросе, а не только при открытии сессии. Корзина, согласие и удаление
# данных привязаны к идентификатору без канала: пока `/widget/message` и
# `/widget/action` принимали любую строку от восьми символов, числовым ID
# пользователя Telegram можно было очистить его корзину, дать за него согласие
# или стереть его данные.
SESSION_ID = r"^[0-9a-f]{32}$"

# --- Файлы в виджете ---------------------------------------------------------
# Спецификацию и список разговора виджет отдаёт одноразовой ссылкой, как Mini App
# через Core API (`facade.export_link`): анонимной сессии выдавать ключи нельзя,
# а токен-адрес сам себе ключ — живёт DOWNLOAD_TTL секунд и срабатывает один раз.
DOWNLOAD_TTL = 300
_FILE_MEDIA = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
}
# token -> (истекает, имя файла, содержимое). В памяти процесса: файл и так
# собран ядром заново при каждом экспорте, хранить его дольше ссылки незачем.
_downloads: dict[str, tuple[float, str, bytes]] = {}
_downloads_lock = threading.Lock()


def _remember_download(filename: str, content: bytes) -> str:
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _downloads_lock:
        for key in [k for k, (expires, _, _) in _downloads.items() if expires <= now]:
            del _downloads[key]
        _downloads[token] = (now + DOWNLOAD_TTL, filename, content)
    return token


class SessionIn(BaseModel):
    # Посетитель, вернувшийся на сайт, присылает свой прежний идентификатор,
    # чтобы не потерять корзину. Проверяем только формат: он анонимный.
    session_id: str | None = Field(default=None, pattern=SESSION_ID)


class MessageIn(BaseModel):
    session_id: str = Field(pattern=SESSION_ID)
    text: str = Field(default="", max_length=2000)


class ActionIn(BaseModel):
    session_id: str = Field(pattern=SESSION_ID)
    action: str = Field(min_length=1, max_length=128)


def create_app(
    settings: Settings | None = None,
    warm_llm: bool = False,
    *,
    engine=None,  # noqa: ANN001 — core.dialog.DialogEngine, для тестов и встраивания
    core=None,  # noqa: ANN001 — core_api.facade.CoreApi
    verifiers=None,  # noqa: ANN001 — способы входа публичных клиентов Core API
) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = engine or build_engine(settings, warm_llm=warm_llm)
    app = FastAPI(title="ЭЛТИ-КУДИЦ · бот-консультант", docs_url=None, redoc_url=None)
    get_core = _install_core_api(app, settings, engine, core, verifiers)

    # Виджет ставится на сайт заказчика, поэтому список источников задаётся явно:
    # открывать его всему интернету незачем.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.widget_allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        # Одно состояние на запрос: число товаров и версия — из одного снимка.
        state = engine.runtime.state
        return {
            "status": "ok",
            "products": len(state.index.products),
            "catalog_version": state.version or "legacy",
            "catalog_sha256": state.sha256,
            "llm": settings.llm_enabled,
            "order_sink": getattr(engine.orders.sink, "name", "?"),
        }

    @app.post("/widget/session")
    def start_session(payload: SessionIn | None = None) -> JSONResponse:
        """Анонимный идентификатор посетителя: до согласия никаких персональных данных.

        Прежний идентификатор переиспользуется — иначе вернувшийся посетитель теряет
        собранную корзину и переписку. Вернувшемуся отдаётся его разговор, и вместо
        приветствия он видит короткую строку о продолжении: поздороваться второй раз
        с тем, кто уже полчаса выбирает мячи, — худшее, что может сделать виджет.
        """
        session_id = (payload.session_id if payload else None) or uuid.uuid4().hex
        responses = engine.start(session_id, CHANNEL)
        history = _history(engine.session(session_id, CHANNEL))
        if history and isinstance(responses[0], Message):
            responses[0] = Message(CONTINUED, keyboard=responses[0].keyboard)
        cart = engine.storage.load_cart(session_id)
        if not cart.is_empty:
            responses += engine.handle_action(session_id, CHANNEL, "cart")
        # Кнопка «Продолжить в Telegram»: /start с этим идентификатором переносит
        # корзину из виджета в чат бота (`TelegramGateway._take_widget_cart`).
        telegram_url = ""
        if settings.telegram_bot_url:
            separator = "&" if "?" in settings.telegram_bot_url else "?"
            telegram_url = f"{settings.telegram_bot_url}{separator}start={session_id}"
        return JSONResponse(
            {
                "session_id": session_id,
                "history": history,
                "responses": to_json(responses),
                "telegram_url": telegram_url,
            }
        )

    @app.post("/widget/message")
    def message(payload: MessageIn) -> JSONResponse:
        responses = engine.handle_text(payload.session_id, CHANNEL, payload.text)
        return JSONResponse({"responses": to_json(responses)})

    @app.post("/widget/action")
    def action(payload: ActionIn) -> JSONResponse:
        verb, _, arg = payload.action.partition(":")
        if verb == "export" and arg in {"xlsx", "docx"}:
            # Раньше сюда приходила заглушка ядра «пришлю в Telegram-боте» — анонимной
            # сессии боту писать некуда. Теперь файл уходит ссылкой прямо в браузер.
            responses = _export_file(engine, payload.session_id, arg)
        else:
            responses = engine.handle_action(payload.session_id, CHANNEL, payload.action)
        return JSONResponse({"responses": to_json(responses)})

    @app.get("/widget/download/{token}")
    def widget_download(token: str) -> HttpResponse:
        """Одноразовая выдача файла: токен гасится первым же запросом."""
        with _downloads_lock:
            entry = _downloads.pop(token, None)
        if entry is None or entry[0] < time.monotonic():
            raise HTTPException(status_code=404, detail="Ссылка недействительна или истекла.")
        _, filename, content = entry
        media_type = _FILE_MEDIA.get(Path(filename).suffix.lower(), "application/octet-stream")
        return HttpResponse(
            content,
            media_type=media_type,
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
        )

    @app.post("/widget/upload")
    async def widget_upload(
        session_id: str = Form(..., pattern=SESSION_ID),
        file: UploadFile = File(...),
    ) -> JSONResponse:
        """Заказ файлом прямо в виджете — тот же разбор, что в Telegram и Mini App.

        Проверку делает ядро (`CoreApi.upload_order` + оценка), отчёт — тот же, что
        в чате бота. Сессия создаётся в хранилище сессий при первом файле: user_ref
        совпадает с идентификатором виджета, корзина и профиль общие.
        """
        content = await file.read()
        filename = (file.filename or "").strip() or "файл"
        limit = settings.order_upload_max_mb * 1024 * 1024
        if not content:
            raise HTTPException(status_code=400, detail="Файл пустой.")
        if len(content) > limit:
            raise HTTPException(status_code=413, detail=f"Файл больше {settings.order_upload_max_mb} МБ.")
        try:
            responses = _check_uploaded_order(get_core(), session_id, filename, content)
        except DomainError as exc:
            # Ядро отказывает по делу (не тот файл, слишком большой) — говорим прямо.
            raise HTTPException(status_code=400, detail=exc.message) from exc
        return JSONResponse({"responses": to_json(responses)})

    @app.get("/media/{sku_1c}")
    def product_photo(sku_1c: str) -> FileResponse:
        """Снимок товара из нашего хранилища.

        Отдаём файл сами, а не ссылаемся на vdm.ru: с части сетей сайт заказчика
        не открывается, и виджет тогда показывает битую картинку вместо товара.
        """
        product = engine.runtime.state.index.get(sku_1c)
        path = engine.photo_path(product) if product is not None else None
        if path is None:
            raise HTTPException(status_code=404, detail="Снимок не собран")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/widget.js")
    def widget_js() -> FileResponse:
        # no-cache, а не запрет кэша: браузер держит копию, но каждый раз
        # сверяется с сервером. Иначе обновление виджета доедет до посетителей
        # сайта только после того, как у них истечёт кэш.
        return FileResponse(
            STATIC / "widget.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/miniapp", response_class=HTMLResponse)
    def miniapp() -> FileResponse:
        """Telegram Mini App: интерфейс поверх `/api`, бизнес-логики в нём нет."""
        return FileResponse(STATIC / "miniapp.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/demo", response_class=HTMLResponse)
    def demo(request: Request) -> HTMLResponse:
        html = (STATIC / "demo.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__BASE_URL__", str(request.base_url).rstrip("/")))

    return app


def _export_file(engine, session_id: str, fmt: str) -> list[Response]:  # noqa: ANN001 — core.dialog.DialogEngine
    """Файл списка разговора или комплектации одноразовой ссылкой — как в Telegram."""
    from core import exports

    file = exports.build(engine, engine.session(session_id, CHANNEL), fmt)
    if file is None:
        return [
            Message(
                "Сохранять пока нечего: сначала соберём комплектацию или подберём позиции.",
                keyboard=Keyboard().row(Button("Меню", "menu")),
            )
        ]
    token = _remember_download(file.filename, file.content)
    # Действие-заполнитель — по образцу «Открыть на сайте»: файл живёт только в
    # веб-канале, ссылка открывается браузером.
    keyboard = Keyboard().row(Button("Скачать файл", "noop", url=f"/widget/download/{token}"))
    return [Message(file.caption, keyboard=keyboard)]


def _check_uploaded_order(core, session_id: str, filename: str, content: bytes) -> list[Response]:
    """Проверка файла заказа: те же слова, что в Telegram (`TelegramGateway._upload`).

    Кнопки согласованы с каналом: предзаказ по загруженному файлу в виджете идёт
    через корзину и обычное оформление, файлы скачиваются здесь же.
    """
    from adapters.telegram.gateway import MATCHED, evaluation_text, preorder_preview
    from core_api import dto

    opened = core.open_session(channel=CHANNEL, user_ref=session_id, trusted=True)
    session = core.session(opened.session_id or "")
    order = core.upload_order(session, filename, content, core.order_context(session)).data
    assert isinstance(order, dto.OrderOut)
    menu = Keyboard().row(Button("Меню", "menu"))
    if order.status == "FAILED":
        return [Message(f"Файл «{order.source_file['filename']}» не удалось прочитать: {order.error}", keyboard=menu)]
    evaluation = core.evaluate_order(session, order.id).data
    assert isinstance(evaluation, dto.EvaluationOut)
    text = evaluation_text(order, evaluation)
    keyboard = Keyboard()
    if evaluation.status != "REJECTED":
        matched = [item for item in evaluation.items if item.get("match_status") in MATCHED]
        if all(item.get("quantity") is not None for item in matched):
            text += preorder_preview(matched)
        else:
            # Как в Telegram (15.09): файл без количества ушёл менеджеру предзаказом на 0 ₽ —
            # здесь сначала количество, потом корзина.
            text += (
                "\n\nКоличество указано не у всех позиций. Напишите, например, «все по 2», "
                "или добавьте найденное в корзину по 1 шт."
            )
        keyboard.row(Button("Найденные в корзину по 1 шт.", "order_cart:1"))
        keyboard.row(Button("Скачать Excel", "export:xlsx"), Button("Скачать Word", "export:docx"))
    keyboard.row(Button("Меню", "menu"))
    if not evaluation.summary.get("checked") and order.warnings:
        text = f"{order.warnings[0].message}\n\n{text}"
    # Итог проверки — в разговор: иначе «подбери по этому заказу» ни к чему не привязано.
    core.note_dialog(session, text, order, evaluation)
    return [Message(text, keyboard=keyboard)]


def _history(session) -> list[dict[str, str]]:  # noqa: ANN001 — core.dialog.Session
    """Переписка для окна виджета: только реплики человека и бота.

    В истории лежат ещё служебные записи (просьбы к модели, следы инструментов) —
    их посетителю показывать нечего. Хранится история маскированной, поэтому перед
    показом метки раскрываются тем же `Masker`, что и ответы модели; после
    перезапуска сервера соответствие меток потеряно, и нераскрытая метка
    заменяется нейтральным словом, а не телефоном.
    """
    kept = [
        item
        for item in session.history
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    return [
        {"role": item["role"], "text": session.masker.unmask(item["content"])}
        for item in kept[-HISTORY_SHOWN:]
    ]


def _install_core_api(app: FastAPI, settings: Settings, engine, core, verifiers):  # noqa: ANN001
    """Core API на `/api` в том же процессе, что виджет: одна версия каталога, одно хранилище.

    Ядро собирается при первом обращении к `/api`: запуск виджета и существующие
    ручки от этого не зависят и базу ядра не трогают. Возвращает фабрику ядра —
    ею пользуется и приём файлов в виджете (`/widget/upload`).
    """
    """Core API на `/api` в том же процессе, что виджет: одна версия каталога, одно хранилище.

    Ядро собирается при первом обращении к `/api`: запуск виджета и существующие
    ручки от этого не зависят и базу ядра не трогают.
    """
    import threading

    from core_api.composition import build_core
    from core_api.facade import CoreApi
    from core_api.http import install

    holder: dict[str, CoreApi] = {"core": core} if core is not None else {}
    lock = threading.Lock()
    verifiers = dict(verifiers or {})
    if settings.telegram_token:
        # Mini App входит подписанными данными Telegram: пользователь тот же, что у бота.
        from adapters.telegram.miniapp_auth import CREDENTIALS_TYPE, TelegramInitDataVerifier

        verifiers.setdefault(CREDENTIALS_TYPE, TelegramInitDataVerifier(settings.telegram_token))
        if core is not None:
            core.verifiers.setdefault(CREDENTIALS_TYPE, verifiers[CREDENTIALS_TYPE])

    def get_core() -> CoreApi:
        if "core" not in holder:
            with lock:
                if "core" not in holder:
                    holder["core"] = CoreApi(build_core(settings, engine), verifiers)
        return holder["core"]

    if getattr(engine, "procurement", None) is None:
        # Подбор в диалоге идёт через Procurement Core: ядро соберётся при первом подборе.
        engine.procurement_provider = get_core

    install(app, get_core, settings)
    return get_core


app = create_app() if __name__ != "__main__" else None


def main() -> None:
    import uvicorn

    from observability import redact

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    # Токен бота и ключи — вне журнала, включая записи uvicorn.
    redact.install(settings.secret_values)
    for name in settings.ignored_env:
        log.warning("%s больше не читается — см. .env.example", name)
    uvicorn.run(
        # Прогрев провайдера при запуске: отказ Cloud.ru должен стоить времени
        # старта, а не первого сообщения пользователя.
        create_app(settings, warm_llm=True),
        host=settings.widget_host,
        port=settings.widget_port,
        # За nginx приложение видит только адрес контейнера, а не схему страницы.
        # Без доверия к X-Forwarded-Proto `request.base_url` остаётся http, и на
        # https-странице браузер блокирует и скрипт виджета, и ссылки на файлы.
        # Список адресов открыт: до порта приложения снаружи не достучаться —
        # наружу смотрит только nginx.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
