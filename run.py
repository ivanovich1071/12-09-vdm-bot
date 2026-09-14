"""Точка запуска без установки пакета.

    python run.py ingest --source data/raw/Pricelist20260826.xlsx
    python run.py import-1c --file data/raw/Pricelist20260826.xlsx  # на проверку, бот не меняется
    python run.py llm                      # проверить провайдеров модели
    python run.py site                      # каталог с сайта → книга в формате выгрузки 1С
    python run.py media                     # фотографии с сайта → в базу знаний
    python run.py widget
    python run.py telegram
    python run.py search "мячи для спортивного зала"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

# Имена фильтров diff — для подсказки argparse без импорта модулей каталога.
DIFF_FILTER_NAMES = (
    "price-up", "price-down", "new", "removed", "updated", "stock", "review", "recoding", "errors",
)


def _utf8_output() -> None:
    """Вывод в UTF-8 при любом перенаправлении.

    Консоль Windows берёт cp1251, и в неё не влезают ни «✓», ни «₽». Пока
    вывод идёт в окно, Python это скрывает, но стоит написать
    `run.py llm | Tee-Object -FilePath ...` — и команда падает с
    UnicodeEncodeError вместо отчёта. Ровно это лежит в
    data/llm_check_20260902_095514.log.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _utf8_output()
    parser = argparse.ArgumentParser(description="Бот-магазин ЭЛТИ-КУДИЦ")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="собрать базу знаний из выгрузки 1С")
    ingest.add_argument("--source", default="data/raw/Pricelist20260826.xlsx")
    ingest.add_argument("--out", default="data/kb")
    ingest.add_argument("--legacy", action="store_true",
                        help="для разработки: пересобрать legacy products.jsonl, даже если "
                             "каталог ведётся версиями (бот этот файл тогда не читает)")

    import_1c = sub.add_parser(
        "import-1c",
        help="загрузить выгрузку 1С на проверку: разбор и предпросмотр, каталог бота не меняется",
    )
    what = import_1c.add_mutually_exclusive_group(required=True)
    what.add_argument("--file", help="выгрузка .xlsx")
    what.add_argument("--list", action="store_true", help="последние импорты")
    what.add_argument("--show", metavar="ID", help="предпросмотр импорта по номеру")
    what.add_argument("--diff", metavar="ID", help="изменения импорта по позициям")
    what.add_argument("--rediff", metavar="ID",
                      help="пересчитать diff против текущего каталога")
    import_1c.add_argument("--issues", type=int, default=10,
                           help="сколько проблем строк показать")
    import_1c.add_argument("--filter", choices=sorted(DIFF_FILTER_NAMES),
                           help="какие позиции diff показать")
    import_1c.add_argument("--limit", type=int, default=30,
                           help="сколько позиций diff показать")

    catalog = sub.add_parser("catalog", help="версии каталога: baseline, утверждение, откат")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sub.add_parser("init", help="создать baseline из текущего products.jsonl")
    approve = catalog_sub.add_parser("approve", help="утвердить импорт 1С и применить версию")
    approve.add_argument("import_id")
    approve.add_argument("--force", action="store_true",
                         help="разрешить превышение порогов исчезновения и смены цен")
    approve.add_argument("--by", help="кто утверждает (по умолчанию — пользователь ОС)")
    versions = catalog_sub.add_parser("versions", help="версии каталога")
    versions.add_argument("--limit", type=int, default=20)
    rollback = catalog_sub.add_parser("rollback", help="откат: новая версия — копия выбранной")
    rollback.add_argument("version")
    rollback.add_argument("--force", action="store_true",
                          help="разрешить превышение порогов безопасности")
    rollback.add_argument("--by")
    catalog_sub.add_parser("check", help="диагностика указателя, базы и снимков без изменений")
    catalog_sub.add_parser("recover", help="завершить или отменить прерванное применение")
    history = catalog_sub.add_parser("history", help="история товара по версиям")
    history.add_argument("sku")

    norms = sub.add_parser("norms", help="разобрать реестр «пункт приказа 1057 → код 1С»")
    norms.add_argument("--source", default="Baza-Ivan-25-11-25.pdf")
    norms.add_argument("--show-unmatched", type=int, default=10,
                       help="сколько ненайденных кодов показать")

    acts = sub.add_parser("acts", help="разобрать тексты приказов 838 и 1057 в справочник пунктов")
    acts.add_argument("--check", action="store_true",
                      help="сверить пункты базы знаний с текстами приказов")

    sub.add_parser("widget", help="поднять веб-виджет и демо-страницу")
    telegram = sub.add_parser("telegram", help="запустить Telegram-бота")
    telegram.add_argument("--check", action="store_true",
                          help="проверить токен, webhook и Mini App, не запуская бота")
    sub.add_parser("llm", help="проверить провайдеров модели по шагам")

    search = sub.add_parser("search", help="проверить поиск из консоли")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)

    site = sub.add_parser("site", help="собрать каталог с сайта vdm.ru в формате выгрузки 1С")
    site.add_argument("--out", help="куда записать книгу (по умолчанию data/raw/site-ДАТА.xlsx)")
    site.add_argument("--root", action="append", default=[],
                      help="адрес корневого раздела (можно несколько; по умолчанию — из каталога)")
    site.add_argument("--limit-sections", type=int,
                      help="проба: обойти столько конечных разделов; книга — только с --out")
    site.add_argument("--fresh", action="store_true",
                      help="начать обход заново, не продолжая прерванный")
    site.add_argument("--cache", default="data/site/crawl.json",
                      help="файл обхода, из которого продолжается прерванный запуск")

    media = sub.add_parser("media", help="набрать фотографии товаров с сайта")
    media.add_argument("--listing", action="append", default=[],
                       help="адрес страницы списка (можно указать несколько)")
    media.add_argument("--cards", default="all",
                       help="сколько карточек обойти: число или all (по умолчанию all), "
                            "0 — не обходить")
    media.add_argument("--in-stock", action="store_true",
                       help="только то, что есть в наличии")
    media.add_argument("--sync", action="store_true",
                       help="только перелить накопленное в базу знаний, без обращений к сайту")
    media.add_argument("--no-files", action="store_true",
                       help="не скачивать файлы снимков, собрать только адреса")
    media.add_argument("--dedupe", action="store_true",
                       help="убрать одинаковые снимки внутри папки одного товара")

    dialogs = sub.add_parser("dialogs", help="показать записанные диалоги")
    dialogs.add_argument("--last", type=int, default=10, help="сколько последних диалогов")
    dialogs.add_argument("--channel", help="telegram | web | max")
    dialogs.add_argument("--export", help="выгрузить в файл .md для работы над промптами")

    args = parser.parse_args()

    if args.command == "ingest":
        _ingest(args)

    elif args.command == "import-1c":
        _import_1c(args)

    elif args.command == "site":
        _site(args)

    elif args.command == "catalog":
        _catalog(args)

    elif args.command == "norms":
        from catalog.current import read_pointer, resolve_catalog
        from core.config import Settings
        from ingest.norm_registry import DEFAULT_REGISTRY, build

        settings = Settings.from_env()
        kb_path = Path(settings.kb_path)
        registry_path = kb_path.parent / DEFAULT_REGISTRY.name
        known = {record["sku_1c"] for record in resolve_catalog(kb_path).records()}
        report = build(Path(args.source), known, registry_path)
        print(
            f"строк с кодом 1С: {report.lines_with_code}\n"
            f"пунктов приказа:  {report.item_codes}\n"
            f"кодов 1С:         {report.sku_codes}\n"
            f"сошлось с выгрузкой: {report.matched} ({report.match_rate:.1%})\n"
            f"нет в выгрузке:      {len(report.unmatched)}"
        )
        if report.unmatched and args.show_unmatched:
            print("\nнет в текущей выгрузке 1С (снято с продажи или переименовано):")
            for line in report.unmatched[: args.show_unmatched]:
                print("   ", line)
            if len(report.unmatched) > args.show_unmatched:
                print(f"    … ещё {len(report.unmatched) - args.show_unmatched}")
        print(f"\nреестр сохранён: {registry_path}")
        if read_pointer(kb_path.parent) is None:
            print("Дальше: python run.py ingest --source <выгрузка>.xlsx")
        else:
            # Каталог ведётся версиями: реестр применяется к текущему снимку версией
            # `registry`, без новой выгрузки 1С (D11, J).
            from catalog_versions.service import CatalogVersionError, build_version_service

            try:
                result = build_version_service(settings).publish_registry(_user())
            except CatalogVersionError as exc:
                sys.exit(str(exc))
            print(result.message)

    elif args.command == "acts":
        _parse_acts(args)

    elif args.command == "widget":
        from web.app import main as run_widget

        run_widget()

    elif args.command == "telegram":
        import asyncio

        from adapters.telegram.bot import main as run_bot
        from adapters.telegram.bot import use_compatible_event_loop

        use_compatible_event_loop()
        if args.check:
            from adapters.telegram.preflight import main as preflight

            sys.exit(asyncio.run(preflight()))
        asyncio.run(run_bot())

    elif args.command == "llm":
        from agent.diagnostics import report
        from agent.providers import build_router
        from core.config import Settings

        settings = Settings.from_env()
        router = build_router(settings)
        print(f"LLM_PROVIDER={settings.llm_provider}")
        print(report(router.clients, router))

    elif args.command == "media":
        _collect_media(args)

    elif args.command == "dialogs":
        from core.config import Settings
        from observability.dialog_log import read_dialogs

        settings = Settings.from_env()
        sessions = read_dialogs(Path(settings.dialog_log_path), limit_sessions=args.last)
        if not sessions:
            print(f"Диалогов пока нет: {settings.dialog_log_path}")
            return

        lines = _format_dialogs(sessions, channel=args.channel)
        text = "\n".join(lines)
        if args.export:
            Path(args.export).write_text(text, encoding="utf-8")
            print(f"Выгружено {len(sessions)} диалогов в {args.export}")
        else:
            print(text)

    elif args.command == "search":
        from catalog.runtime import CatalogRuntime
        from catalog.search import SearchQuery
        from core.config import Settings
        from core.ui import price_text, stock_text

        state = CatalogRuntime.open(Settings.from_env().kb_path).state
        print(f"каталог: версия {state.version or 'legacy'}, товаров {len(state.index.products)}")
        for hit in state.index.search(SearchQuery(text=args.query, limit=args.limit)):
            product = hit.product
            print(f"[{hit.reason}] {product.name}")
            print(f"    {price_text(product.price)} · {stock_text(product)}")
            if hit.citation():
                print(f"    {hit.citation()}")


def _import_1c(args) -> None:  # noqa: ANN001 — argparse.Namespace
    """Импорт выгрузки на проверку (EPIC 2) и diff (EPIC 4). Базу знаний бота не трогает."""
    from catalog_import.diff import format_diff
    from catalog_import.files import UploadRejected
    from catalog_import.service import (
        ImportStateError,
        build_service,
        format_imports,
        format_preview,
    )
    from core.config import Settings

    service = build_service(Settings.from_env())
    if args.list:
        print(format_imports(service.list_imports()))
        return
    if args.diff or args.rediff:
        import_id = args.diff or args.rediff
        try:
            record = service.rediff(import_id) if args.rediff else service.require(import_id)
        except ImportStateError as exc:
            sys.exit(str(exc))
        print(format_diff(record, service.diff_rows(import_id), args.filter, args.limit))
        return
    if args.show:
        record = service.get(args.show)
        if record is None:
            sys.exit(f"Импорта {args.show} нет.")
    else:
        try:
            record = service.upload(Path(args.file), uploaded_by=_user())
        except UploadRejected as exc:
            sys.exit(f"Файл не принят: {exc}")
    print(format_preview(record, service.issues(record.id, limit=args.issues)))


def _user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except OSError:
        return "cli"


def _catalog(args) -> None:  # noqa: ANN001 — argparse.Namespace
    """Версии каталога (EPIC 4): init, approve, versions, rollback, check, recover, history."""
    from catalog.current import CatalogPointerError
    from catalog_import.service import ImportStateError
    from catalog_versions.lock import CatalogLockTimeout
    from catalog_versions.service import (
        CatalogVersionError,
        build_version_service,
        format_version,
    )
    from core.config import Settings

    service = build_version_service(Settings.from_env())
    command = args.catalog_command
    by = getattr(args, "by", None) or _user()
    try:
        if command == "init":
            version = service.init(by)
            print(f"Baseline создан, указатель {service.kb_dir / 'current'} → {version.version}.")
            print(format_version(version, version.version))
        elif command == "approve":
            version = service.approve(args.import_id, by, force=args.force)
            print(f"Импорт {args.import_id} утверждён и применён: версия {version.version}.")
            print(format_version(version, version.version))
        elif command == "rollback":
            version = service.rollback(args.version, by, force=args.force)
            print(f"Откат выполнен новой версией {version.version} (копия {args.version}).")
            print(format_version(version, version.version))
        elif command == "versions":
            current = service.current_version()
            listed = service.list_versions(args.limit)
            print("\n".join(format_version(v, current) for v in listed) or "Версий пока нет.")
        elif command == "check":
            print("\n".join(service.check()))
        elif command == "recover":
            messages = service.recover()
            print("\n".join(messages) if messages else "Восстанавливать нечего: каталог согласован.")
        elif command == "history":
            rows = service.versions.product_history(args.sku)
            if not rows:
                print(f"Истории товара {args.sku} нет.")
            for row in rows:
                fields = ", ".join(row["changed_fields"])
                print(
                    f"{row['version']:<15} {row['change_status']:<8} {row['valid_from']} → "
                    f"{row['valid_to'] or 'сейчас'} · {row['name']} · цена {row['price']} · "
                    f"остаток {row['in_stock']}" + (f" · поля: {fields}" if fields else "")
                )
    except (CatalogVersionError, CatalogPointerError, CatalogLockTimeout, ImportStateError) as exc:
        sys.exit(str(exc))


def _parse_acts(args) -> None:  # noqa: ANN001 — argparse.Namespace
    """Справочник пунктов из текстов приказов и сверка с ним базы знаний.

    Сами PDF в репозитории не лежат — как и любые документы заказчика, — поэтому
    команду запускает тот, у кого файлы рядом с проектом. Без справочника бот
    работает по-прежнему, называя номер пункта без формулировки.
    """
    from catalog.current import resolve_catalog
    from catalog.models import Product
    from core.config import Settings
    from norms import documents as docs
    from norms import items as norm_items

    sources = {
        doc.id: Path(doc.pdf_name)
        for doc in docs.DOCUMENTS.values()
        if doc.pdf_name
    }
    missing = [str(path) for path in sources.values() if not path.exists()]
    if missing:
        print("Не найдены файлы приказов рядом с проектом:")
        for name in missing:
            print("   ", name)
        if len(missing) == len(sources):
            return

    counts = norm_items.build(sources)
    for doc_id, count in counts.items():
        print(f"{docs.get(doc_id).short_name}: разобрано пунктов — {count}")
    print(f"справочник сохранён: {norm_items.DEFAULT_ITEMS}")

    if not args.check:
        return

    known = norm_items.load()
    ours: dict[tuple[str, str], str] = {}
    for record in resolve_catalog(Settings.from_env().kb_path).records():
        product = Product.from_dict(record)
        for ref in product.norms:
            if ref.item_code:
                ours.setdefault((ref.doc_id, ref.item_code), product.name)

    missing_codes = [key for key in ours if key[1] not in known.get(key[0], {})]
    print(f"\nпунктов в базе знаний: {len(ours)} · не нашлось в приказах: {len(missing_codes)}")
    for doc_id, code in sorted(missing_codes)[:20]:
        print(f"    {docs.get(doc_id).short_name} п. {code} — {ours[(doc_id, code)][:50]}")
    if len(missing_codes) > 20:
        print(f"    … ещё {len(missing_codes) - 20}")


def _collect_media(args) -> None:  # noqa: ANN001 — argparse.Namespace
    """Сбор фотографий и характеристик с сайта и запись их в базу знаний.

    Обход длинный — тысячи карточек по одной в секунду, — поэтому он прерываемый:
    что успели собрать, то и попадает в `products.jsonl`. Повторный запуск
    продолжает с места остановки, потому что уже собранное лежит в кэше.

    Файлы снимков кладутся на диск: Telegram не может забрать картинку с vdm.ru
    сам, а бот заодно перестаёт зависеть от того, отвечает ли сайт в момент показа.
    """
    from core.app import build_engine
    from core.config import Settings

    settings = Settings.from_env()
    engine = build_engine(settings)

    if args.dedupe:
        from media.files import drop_duplicates

        removed = drop_duplicates(settings.media_dir)
        print(f"удалено одинаковых снимков внутри папок товаров: {len(removed)}")
        for name in removed[:20]:
            print("   ", name)
        print(_sync_media(engine, settings))
        return

    if args.sync:
        print(_sync_media(engine, settings))
        return

    engine.media.download_files = not args.no_files

    products = engine.index.products
    if args.in_stock:
        products = [p for p in products if p.available]

    for url in args.listing:
        saved = engine.media.warm_up_from_listing(url, products)
        print(f"{url}: превью получено для {saved} товаров")

    limit = len(products) if args.cards == "all" else int(args.cards)
    queue = products[:limit]
    if queue:
        _walk_cards(engine.media, queue)

    print("в кэше:", engine.storage.media_stats())
    if engine.media.photos is not None:
        print("файлы снимков:", engine.media.photos.stats())
    print(_sync_media(engine, settings))


def _site(args) -> None:  # noqa: ANN001 — argparse.Namespace
    """Каталог с сайта vdm.ru: обход → книга в формате выгрузки 1С → import-1c → approve.

    Выгрузок 1С не будет (заказчик, 14.09). Книга проходит обычный импорт с проверками,
    diff и порогами — поэтому эта команда каталог бота не меняет, а только собирает файл.
    Обход длинный и прерываемый: повторный запуск продолжает с места остановки.
    """
    import time
    from collections import Counter
    from datetime import datetime
    from urllib.parse import urlparse

    from catalog.current import read_pointer
    from core.app import build_engine
    from core.config import Settings
    from media.fetcher import DEFAULT_USER_AGENT, PageFetcher
    from site_catalog.crawl import SiteCrawler
    from site_catalog.export import Known, build_book

    settings = Settings.from_env()
    engine = build_engine(settings)
    products = engine.index.products

    roots = args.root
    if not roots:
        counts: Counter[str] = Counter()
        for product in products:
            parts = urlparse(product.url or "")
            segments = [segment for segment in parts.path.split("/") if segment]
            if len(segments) >= 2 and segments[0] == "catalog":
                counts[f"{parts.scheme}://{parts.netloc}/catalog/{segments[1]}/"] += 1
        roots = [url for url, _ in counts.most_common()]
    if not roots:
        sys.exit("Не из чего взять корневые разделы: укажите --root https://vdm.ru/catalog/<раздел>/")

    cache = Path(args.cache)
    if args.fresh and cache.exists():
        cache.unlink()
    fetcher = PageFetcher(
        user_agent=settings.media_user_agent or DEFAULT_USER_AGENT,
        min_interval=settings.media_min_interval,
        respect_robots=settings.media_respect_robots,
        retries=4,
        timeout=40.0,
    )

    started = last = time.monotonic()

    def progress(result, _url: str) -> None:  # noqa: ANN001 — CrawlResult
        nonlocal last
        now = time.monotonic()
        if now - last >= 15:
            last = now
            print(
                f"  разделов {result.sections} · конечных {result.leaves} · страниц {result.pages} · "
                f"товаров {len(result.products)} · {int(now - started) // 60} мин",
                flush=True,
            )

    crawler = SiteCrawler(fetcher, cache, progress)
    print(f"Обход каталога vdm.ru, корневых разделов: {len(roots)}.", flush=True)
    if crawler.resumed:
        print(f"  продолжаем прерванный обход: страниц уже загружено {len(crawler.pages)}", flush=True)
    print("  Ctrl+C — остановить; повторный запуск продолжит с места остановки.", flush=True)
    try:
        result = crawler.crawl(roots, limit_leaves=args.limit_sections)
    except KeyboardInterrupt:
        sys.exit("\nОстановлено. Загруженные страницы сохранены — запустите команду ещё раз.")

    print(
        f"разделов: {result.sections}, конечных: {result.leaves}, страниц: {result.pages} "
        f"(загружено сейчас {result.fetched}), товаров на сайте: {len(result.products)}"
    )
    if not result.complete:
        print(f"Не загрузились страницы: {len(result.failed)}")
        for url in result.failed[:10]:
            print("   ", url)
        sys.exit(
            "Обход неполный — книгу не собираем: товары пропущенных разделов ушли бы в исчезнувшие.\n"
            "Запустите ту же команду ещё раз: загруженные страницы не повторяются."
        )

    known = {
        product.bitrix_id: Known(product.sku_1c, product.description, tuple(product.kit_contents), product.short_url)
        for product in products
        if product.bitrix_id is not None
    }
    new = [product for bitrix_id, product in result.products.items() if bitrix_id not in known]
    if new:
        print(f"Новых товаров: {len(new)} — открываем их страницы ради кода 1С и описания.", flush=True)

    def card_progress(number: int, total: int) -> None:
        nonlocal last
        now = time.monotonic()
        if now - last >= 15 or number == total:
            last = now
            print(f"  карточек {number}/{total}", flush=True)

    try:
        cards, failed_cards = crawler.fetch_cards(new, card_progress)
    except KeyboardInterrupt:
        sys.exit("\nОстановлено. Загруженное сохранено — запустите команду ещё раз.")
    if failed_cards:
        print(f"Не открылись страницы новых товаров: {len(failed_cards)} — эти товары в книгу не попадут.")
    for card in cards.values():
        # Фото и характеристики новых товаров — в кэш снимков: их перенесёт `media --sync`.
        if card.sku and card.images:
            engine.storage.save_media(card.sku, card.images, source="card")
        if card.sku and card.attributes:
            engine.storage.save_attributes(card.sku, card.attributes)

    missing = sum(1 for bitrix_id in known if bitrix_id not in result.products)
    book, report = build_book(result, known, cards)
    if args.limit_sections and not args.out:
        print(f"Проба: товаров {report.products}, из них уже в каталоге {report.known}, новых {report.new}.")
        print("Книга по пробному обходу не записана: в импорте почти весь каталог ушёл бы в исчезнувшие.")
        return

    out = Path(args.out or f"data/raw/site-{datetime.now():%Y%m%d-%H%M}.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(book)
    print(f"\nКнига: {out}")
    print(f"  товаров: {report.products} (уже в каталоге: {report.known}, новых: {report.new}), разделов: {report.sections}")
    if report.without_code:
        print(f"  пропущено без кода 1С: {len(report.without_code)}")
        for url in report.without_code[:10]:
            print("   ", url)
    if missing:
        print(f"  есть в каталоге, но нет на сайте: {missing} — в diff импорта они будут исчезнувшими")
    print("\nДальше:")
    if read_pointer(Path(settings.kb_path).parent) is None:
        print("  python run.py catalog init          # один раз: каталог начинает вестись версиями")
    print(f"  python run.py import-1c --file {out}")
    print("  python run.py catalog approve <ID из вывода импорта>")


def _sync_media(engine, settings) -> str:  # noqa: ANN001 — DialogEngine, Settings
    """Собранные фото и характеристики — в каталог.

    Без указателя — прежняя переливка в `products.jsonl`. С указателем — версия
    `media` от текущей: меняются только фото и характеристики, без изменений
    версия не создаётся (D11, J).
    """
    from catalog.current import read_pointer
    from media.sync import sync_to_kb

    kb_path = Path(settings.kb_path)
    if read_pointer(kb_path.parent) is None:
        return f"перелито в базу знаний: {sync_to_kb(engine.storage, kb_path)}"

    from catalog_versions.service import CatalogVersionError, build_version_service

    try:
        result = build_version_service(settings).publish_media(
            engine.storage.all_media(), engine.storage.all_attributes(), _user()
        )
    except CatalogVersionError as exc:
        sys.exit(str(exc))
    return result.message


def _ingest(args) -> None:  # noqa: ANN001 — argparse.Namespace
    """Legacy-сборка базы знаний. При версиях каталога бот её не видит (D11, J)."""
    import json
    from dataclasses import asdict

    from catalog.current import read_pointer, resolve_catalog
    from ingest.build_kb import build

    out = Path(args.out)
    kb = out / "products.jsonl"
    pointer = read_pointer(out)
    if pointer is not None and not args.legacy:
        sys.exit(
            f"Каталог ведётся версиями (текущая {pointer.version}): ingest каталог бота не меняет.\n"
            "Новая выгрузка: python run.py import-1c --file <выгрузка>.xlsx → "
            "python run.py import-1c --diff ID → python run.py catalog approve ID.\n"
            "Для разработки: ingest --legacy пересоберёт только legacy products.jsonl."
        )
    # Фото и характеристики переносятся из текущего каталога — по указателю, если он есть.
    previous = resolve_catalog(kb).path if pointer is not None or kb.exists() else None
    report = build(Path(args.source), out, previous=previous)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    if pointer is not None:
        print(f"Пересобран legacy-файл {kb}; бот продолжает работать с версией {pointer.version}.")


def _walk_cards(media, queue: list) -> None:  # noqa: ANN001 — media/service.py
    """Обход карточек с честным прогрессом.

    Скорость плавает на три порядка: то, что уже в кэше, идёт мгновенно, а одна
    недоступная страница стоит минуту. Поэтому прогресс печатается по времени,
    а не по числу товаров — иначе после быстрого куска наступает тишина на час,
    и обход выглядит зависшим.
    """
    import time

    from core.ui import plural
    from media.service import MediaService

    # Сайт может быть недоступен целиком. Молча перебирать оставшиеся тысячи
    # позиций по минуте на каждую — сутки впустую, поэтому останавливаемся.
    give_up_after = 3
    report_every = 15.0  # секунд

    started = last_report = time.monotonic()
    done = failed = 0
    stopped = ""
    announced = False

    try:
        for number, product in enumerate(queue, 1):
            # `collect`, а не `images_for`: при обходе страница нужна ещё и ради
            # характеристик, и с неё же скачивается файл снимка.
            if media.collect(product):
                done += 1
            else:
                failed += 1

            if isinstance(media, MediaService) and media.consecutive_errors >= give_up_after:
                pages = plural(give_up_after, "страница", "страницы", "страниц")
                stopped = (
                    f"\nСайт не отвечает: {give_up_after} {pages} подряд не загрузились.\n"
                    "Обход остановлен, собранное сохранено. Продолжить можно той же\n"
                    "командой оттуда, где vdm.ru открывается."
                )
                break

            now = time.monotonic()
            fetches = getattr(media, "fetches", 0)
            if fetches and not announced:
                # Первое обращение к сайту стоит отметить сразу: до него обход
                # летит по кэшу, и без этой строки переход на медленный режим
                # выглядит как зависание.
                announced = True
                print(
                    f"  {number - 1} карточек взято из кэша, дальше загрузка с сайта",
                    flush=True,
                )
            if now - last_report >= report_every or number == len(queue):
                last_report = now
                line = f"  {number}/{len(queue)} · с фото {done} · без {failed}"
                # Оценку строим по реальным загрузкам: чтение кэша к оставшейся
                # работе отношения не имеет.
                if fetches:
                    per_fetch = (now - started) / fetches
                    left = (len(queue) - number) * per_fetch
                    line += f" · загружено {fetches} · осталось ~{_duration(left)}"
                else:
                    line += " · всё из кэша"
                print(line, flush=True)
    except KeyboardInterrupt:
        stopped = "\nОстановлено. Собранное сохраняем."

    if stopped:
        print(stopped)
    print(f"карточек обработано: {done + failed}, с фото: {done}, без: {failed}")


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} с"
    if seconds < 5400:
        return f"{seconds / 60:.0f} мин"
    return f"{seconds / 3600:.1f} ч"


def _format_dialogs(sessions: dict, channel: str | None) -> list[str]:
    """Диалоги в читаемом виде: реплика пользователя, что ответил бот, что предложил."""
    lines: list[str] = []
    for session, turns in sessions.items():
        if channel and turns[0]["channel"] != channel:
            continue
        head = f"## {session} · {turns[0]['channel']} · {turns[0]['ts'][:16]} · реплик: {len(turns)}"
        lines += ["", head, ""]
        for turn in turns:
            marker = "→" if turn["kind"] == "text" else "⌨"
            lines.append(f"{marker} {turn['in']}   [{_marks(turn)}]")
            for out in turn["out"]:
                if out["type"] == "text":
                    lines.append(f"   бот: {out['text']}")
                elif out["type"] == "list":
                    lines.append(f"   бот: {out['title']}")
                    for item in out["items"]:
                        norm = f" · {item['norm']}" if item.get("norm") else ""
                        lines.append(f"      - {item['name']} · {item['price']} ₽{norm}")
                elif out["type"] == "card":
                    lines.append(f"   бот: карточка {out['name']} · {out['price']} ₽")
                elif out["type"] == "order":
                    lines.append(f"   бот: заказ на {out['total']} ₽, позиций {out['positions']}")
            lines.append("")
        lines += _totals(turns) + [""]
    return lines


# --- Разметка прогона ---------------------------------------------------------
#
# Ручные прогоны по сценариям с возражениями заказчик делает сам, под ВПН, и
# разбирать их приходится по стенограмме. Поэтому в неё идёт не только текст, но
# и то, чем ход обошёлся и почему бот повёл себя именно так.

_ROLE_NAMES = {"consult": "консультант", "sell": "продавец", "guard": "защита"}


def _marks(turn: dict) -> str:
    """Пометки хода: роль, этап, стоимость, причина по карточкам."""
    parts = [turn["mode"], f"{turn['latency_ms']} мс"]
    route = turn.get("route") or {}
    if route:
        parts.append(_ROLE_NAMES.get(str(route.get("role")), str(route.get("role"))))
        if route.get("stage"):
            parts.append(str(route["stage"]))
        if route.get("objection") and route["objection"] != "none":
            snag = str(route["objection"])
            parts.append(f"возражение {snag}" + ("" if route.get("objection_handled") else " ✗"))
        cards = route.get("cards") or {}
        if cards:
            parts.append(("карточки: да" if cards.get("allowed") else "карточек нет") + f" — {cards.get('reason', '')}")
    usage = turn.get("usage") or {}
    if usage:
        parts.append(
            f"{usage.get('tokens_in', 0)}+{usage.get('tokens_out', 0)} токенов, "
            f"{usage.get('cost_rub', 0)} ₽"
        )
    return ", ".join(parts)


def _totals(turns: list[dict]) -> list[str]:
    """Итог прогона: ходы, обращения к модели, токены, рубли."""
    calls = tokens_in = tokens_out = 0
    cost = 0.0
    for turn in turns:
        usage = turn.get("usage") or {}
        calls += int(usage.get("calls") or 0)
        tokens_in += int(usage.get("tokens_in") or 0)
        tokens_out += int(usage.get("tokens_out") or 0)
        cost += float(usage.get("cost_rub") or 0.0)
    return [
        f"**Итог прогона:** ходов {len(turns)}, обращений к модели {calls}, "
        f"токенов {tokens_in} на входе и {tokens_out} на выходе, "
        f"стоимость {round(cost, 3)} ₽."
    ]


if __name__ == "__main__":
    main()
