"""Перехваты событий формы в расширениях: у заимствованной формы расширение
перехватывает не процедуру модуля, а событие — в Form.xml расширения
`<Event name="OnCreateAtServer" callType="After">Расш_ПриСозданииПосле</Event>`.
Аннотаций &После/&Вместо в коде у таких обработчиков нет, поэтому графа
вызовов (рёбер override) они не дают.

Перехваченная процедура — обработчик того же события того же элемента в
Form.xml основной конфигурации. Значения callType в выгрузке английские и в
русских конфигурациях: Before / After / Override.

При индексации сохраняются связи события с обработчиком. По ним сравниваются
формы основной конфигурации и расширений.
"""

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

logger = logging.getLogger('ariadna')

# callType → ключевое слово директивы, как у перехватов в коде (Intercept.keyword):
# в русской конфигурации — &После, в английской — &After.
CALL_TYPES = {'before': 'Перед', 'after': 'После', 'override': 'Вместо'}
CALL_TYPES_EN = {'before': 'Before', 'after': 'After', 'override': 'Around'}
FORM_EVENT_MARK = '(событие формы)'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def form_events(form_xml: str | Path) -> dict[tuple[str, str], tuple[str, str]]:
    """{(владелец, событие): (обработчик, callType)} из Form.xml. Владелец —
    '' для событий самой формы, иначе имя элемента (атрибут name); callType —
    '' у обычного обработчика. Файл не читается — пустой словарь."""
    try:
        root = ET.parse(form_xml).getroot()
    except (OSError, ET.ParseError) as e:
        logger.warning('Перехваты событий формы: не прочитан %s (%s)', form_xml, e)
        return {}
    found: dict[tuple[str, str], tuple[str, str]] = {}

    def walk(el, owner: str):
        for child in el:
            tag = _local(child.tag)
            if tag == 'Events':
                for ev in child:
                    if _local(ev.tag) != 'Event':
                        continue
                    name = ev.get('name') or ''
                    handler = (ev.text or '').strip()
                    if name and handler:
                        found[(owner, name)] = (handler, (ev.get('callType') or '').strip())
            else:
                walk(child, child.get('name') or owner)

    walk(root, '')
    return found
