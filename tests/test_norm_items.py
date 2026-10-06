"""Разбор текстов приказов в справочник пунктов.

Проверяется не «работает ли pypdf», а две конкретные порчи вёрстки, из-за которых
сверка врала: разорванный пробелом номер и номер, склеенный с названием.
"""

from norms.items import NormItem, load, parse_838, parse_1057

# Так выглядит выдача pdf для приказа 838: заголовки разделов отдельными
# строками, пункт — номер с точкой и название.
TEXT_838 = """
Раздел 2. Комплекс оснащения предметных кабинетов
Подраздел 4. Кабинет учителя-логопеда (учителя-дефектолога)
Основное оборудование
2.4.35. Дидактические пособия и обучающие игры для формирования словарного запаса
2.4.40. Набор дидактических картинок с изображением предметов, действий, понятий
Подраздел 20. Кабинет труда (технологии)
2.20.63. Фрезерно-гравировальный станок с числовым программным управлением
"""

# А так — приказ 1057: таблица, из которой номер приезжает то склеенным с
# названием, то разорванным переносом строки.
TEXT_1057 = """
1.13.4.3.1.9Интерактивная панель (доска с
потолочным проектором)
Шт. 1  +
1.13.4.3.1.1
0
Комплект интерактивно-цифровых
комплексов
Шт. 1  +
1.3.4.1 Барабан с палочками Шт. 10 +
"""


def test_838_keeps_section_of_item():
    items = {item.code: item for item in parse_838(TEXT_838)}
    assert items["2.4.35"].title.startswith("Дидактические пособия")
    assert items["2.4.35"].section == "Кабинет учителя-логопеда (учителя-дефектолога)"
    assert items["2.20.63"].section == "Кабинет труда (технологии)"


def test_838_ignores_headings():
    codes = {item.code for item in parse_838(TEXT_838)}
    assert codes == {"2.4.35", "2.4.40", "2.20.63"}


# pypdf выдаёт текст страницы не по порядку: пункты приезжают раньше своих
# заголовков, а «Подраздел 4» — после чужих подразделов. Прежнее «липкое»
# наследование подписывало весь раздел 2 «Кабинетом учителя-логопеда».
TEXT_838_SCRAMBLED = """
Раздел 2. Комплекс оснащения предметных кабинетов
Подраздел 4. Кабинет учителя-логопеда (учителя-дефектолога)
2.14.47. Микроскоп демонстрационный
Подраздел 14. Кабинет физики
2.15.36. Эвдиометр
Подраздел 15. Кабинет химии
2.15. Конторка
Подраздел 1. Кабинет начальных классов
2.1. а) рельсовая система с классной доской
"""


def test_838_section_follows_code_not_line_order():
    items = {item.code: item for item in parse_838(TEXT_838_SCRAMBLED)}
    assert items["2.14.47"].section == "Кабинет физики"
    assert items["2.15.36"].section == "Кабинет химии"
    assert items["2.1"].section is None


def test_838_position_twin_does_not_steal_subsection_name():
    """Шаг 3.4: «2.15. Конторка» — общая позиция, а не «Кабинет химии».

    У приказа 838 совпадают номера позиции и подраздела: пункт 2.15.36 — это
    кабинет химии, а позиция 2.15 «Конторка» покупается в несколько кабинетов.
    Прежний разбор подписывал позицию чужим подразделом по цифрам.
    """
    items = {item.code: item for item in parse_838(TEXT_838_SCRAMBLED)}
    assert items["2.15"].section is None
    assert items["2.15"].title == "Конторка"


# Общие позиции: фраза «является общей…» + перечень кабинетов строками
# «Подраздел N. Название», как в самом приказе.
TEXT_838_COMMON = """
Раздел 2. Комплекс оснащения предметных кабинетов
Позиции 2.1-2.3 являются общими для следующих подразделов (предметных кабинетов) и приобретаются в каждый из них:
Подраздел 15. Кабинет химии
Подраздел 4. Кабинет учителя-логопеда (учителя-дефектолога)
2.1. Доска магнитно-маркерная
Раздел 3. Комплекс лабораторий и студий для внеурочной деятельности
Позиции 3.1-3.2 являются общими для следующих подразделов (предметных кабинетов):
Подраздел 1. Студия искусства и дизайна
3.1. Стол с ящиками для хранения/тумбой
2.15. Конторка
"""


def test_838_common_positions_list_their_cabinets():
    items = {item.code: item for item in parse_838(TEXT_838_COMMON)}
    assert items["2.1"].section is None
    assert items["2.1"].cabinets == (
        "2.15 Кабинет химии",
        "2.4 Кабинет учителя-логопеда (учителя-дефектолога)",
    )
    # Глава у перечня кабинетов — из самой позиции: «3.1» — это студия
    # из раздела 3, а не тёзка из раздела 2.
    assert items["3.1"].cabinets == ("3.1 Студия искусства и дизайна",)


def test_expand_codes_ranges_and_pairs():
    from norms.items import _expand_codes

    assert _expand_codes("2.1-2.3") == ["2.1", "2.2", "2.3"]
    assert _expand_codes("2.16, 2.17") == ["2.16", "2.17"]
    assert _expand_codes("2.13") == ["2.13"]


def test_item_index_subsection_and_common_children(tmp_path):
    """Шаг 3.4: подраздел по коду и общие позиции в комплектации кабинета."""
    import json

    from norms.items import ItemIndex, load, load_meta

    target = tmp_path / "norm_items.json"
    target.write_text(
        json.dumps(
            {
                "order_838": [
                    {"code": "2.15.36", "title": "Эвдиометр", "section": "Кабинет химии"},
                    {"code": "2.15", "title": "Конторка", "cabinets": ["2.15 Кабинет химии"]},
                    {"code": "2.1", "title": "Доска", "cabinets": ["2.15 Кабинет химии"]},
                ],
                "subsections": {"order_838": {"2.15": "Кабинет химии"}},
                "common_positions": {
                    "order_838": {
                        "2.15": ["2.15 Кабинет химии"],
                        "2.1": ["2.15 Кабинет химии"],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    index = ItemIndex(load(target), load_meta(target))
    assert index.subsection("order_838", "2.15.36") == "Кабинет химии"
    assert index.subsection("order_838", "2.15") == "Кабинет химии"
    assert index.subsection("order_838", "1.1.1") is None
    # Комплектация кабинета = его пункты + общие позиции этого кабинета.
    assert [item.code for item in index.children("order_838", "2.15")] == [
        "2.1",
        "2.15",
        "2.15.36",
    ]


def test_section_conflicts_spots_sticky_sections():
    from norms.items import section_conflicts

    known = {
        "order_838": {
            code: NormItem(doc_id="order_838", code=code, title="x", section="Кабинет учителя-логопеда")
            for code in ("2.1", "2.15.1", "2.16.1")
        }
    }
    conflicts = section_conflicts(known)
    assert conflicts and "2.1" in conflicts[0]

    healthy = {
        "order_838": {
            "2.15.1": NormItem(doc_id="order_838", code="2.15.1", title="x", section="Кабинет химии"),
            "2.15.2": NormItem(doc_id="order_838", code="2.15.2", title="y", section="Кабинет химии"),
        }
    }
    assert section_conflicts(healthy) == []


def test_1057_glues_code_split_by_layout():
    """«1.13.4.3.1.1 0» — это пункт 1.13.4.3.1.10, а не 1.13.4.3.1.1.

    Из-за этой порчи 482 пункта базы знаний «не находились» в приказе, и сверка
    показывала ошибку там, где данные верны.
    """
    codes = {item.code for item in parse_1057(TEXT_1057)}
    assert "1.13.4.3.1.10" in codes
    assert "1.13.4.3.1.1" not in codes


def test_1057_separates_code_glued_to_title():
    items = {item.code: item for item in parse_1057(TEXT_1057)}
    assert items["1.13.4.3.1.9"].title.startswith("Интерактивная панель")


def test_1057_splits_unit_and_quantity():
    items = {item.code: item for item in parse_1057(TEXT_1057)}
    assert items["1.3.4.1"].title == "Барабан с палочками"
    assert items["1.3.4.1"].unit == "Шт."
    assert items["1.3.4.1"].quantity == "10"


def test_missing_file_gives_empty_reference(tmp_path):
    """Приказов рядом с проектом может не быть — бот всё равно должен работать."""
    assert load(tmp_path / "нет-такого.json") == {}


def test_load_reads_written_reference(tmp_path):
    import json

    target = tmp_path / "norm_items.json"
    target.write_text(
        json.dumps(
            {
                "order_838": [
                    {"code": "2.4.35", "title": "Дидактические пособия", "section": "Логопед"}
                ]
            }
        ),
        encoding="utf-8",
    )
    known = load(target)
    assert known["order_838"]["2.4.35"] == NormItem(
        doc_id="order_838", code="2.4.35", title="Дидактические пособия", section="Логопед"
    )
