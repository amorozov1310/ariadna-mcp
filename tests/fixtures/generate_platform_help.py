"""Миниатюрные книги синтакс-помощника. Страницы и оглавление полностью синтетические."""

import io
import struct
import zipfile
from html import escape

_LAST = 0x7fffffff


def _zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return out.getvalue()


def _block(data, next_addr=_LAST, total=None):
    header = f"\r\n{len(data) if total is None else total:08x} {len(data):08x} {next_addr:08x} \r\n"
    return header.encode('ascii') + data


def _container(items):
    # В первом документе — пары адресов заголовка и тела каждого элемента.
    address = 16 + 31 + 12 * len(items)
    toc, blocks = bytearray(), bytearray()
    for name, payload in items.items():
        header = _block(bytes(20) + (name + '\0').encode('utf-16-le'))
        start = address + len(header)
        toc.extend(struct.pack('<III', address, start if payload else _LAST, _LAST))
        # Две страницы на документ, чтобы проверить переход по цепочке блоков.
        if payload:
            body = _block(payload[:8], start + 31 + 8, len(payload)) + _block(payload[8:])
        else:
            body = b''
        blocks.extend(header + body)
        address = start + len(body)
    return struct.pack('<IIII', _LAST, 512, 0, 0) + _block(toc) + blocks


def _book(pages):
    nodes = ['{1,0,0,{0,0,{0,1,{"ru","Учебный раздел"}},""}}']
    files = {}
    for i, (path, name, page) in enumerate(pages, start=2):
        nodes.append('{%d,1,0,{0,0,{0,1,{"ru","%s"}},"/%s"}}' % (i, name, path))
        files[path] = page
    toc = '{' + ','.join([str(len(nodes)), *nodes]) + '}'
    return _container({'Book': b'', 'FileStorage': _zip(files), 'PackBlock': _zip({'0': toc})})


def context_book(description='Учебное описание.'):
    pages = []

    def page(path, ru, en, owner='', owner_en=''):
        name = f'{ru} ({en})'
        title = f'{owner} ({owner_en})' if owner else name
        html = (f'<h1>{escape(name)}</h1><p class="V8SH_title">{escape(title)}</p>'
                f'<p class="V8SH_heading">{escape(name)}</p>'
                f'<p class="V8SH_chapter">Синтаксис:</p>{ru}(&lt;Аргумент&gt;)'
                '<p class="V8SH_chapter">Параметры:</p>'
                '<div class="V8SH_rubric">&lt;Аргумент&gt; (обязательный)</div>'
                'Тип: Строка.<br>Учебный параметр.'
                f'<p class="V8SH_chapter">Описание:</p>{escape(description)}'
                '<p class="V8SH_chapter">Доступность:</p>Сервер.')
        pages.append((path, ru, html))

    for ru, en in [('СтрНайти', 'StrFind'), ('СтрНайтиПример', 'StrFindExample')]:
        page(f'objects/Global context/methods/{en}.html', ru, en)
    page('objects/ValueTable.html', 'ТаблицаЗначений', 'ValueTable')
    for ru, en in [('Пустая', 'Empty'), ('ИзСтрок', 'FromRows')]:
        page(f'objects/ValueTable/ctors/{en}.html', ru, en, 'ТаблицаЗначений', 'ValueTable')
    for ru, en in [('СправочникМенеджер', 'CatalogManager'), ('СправочникОбъект', 'CatalogObject'),
                   ('ДокументМенеджер', 'DocumentManager'), ('ПланОбменаМенеджер', 'ExchangePlanManager'),
                   ('РегистрСведенийМенеджер', 'InformationRegisterManager'),
                   ('ПеречислениеМенеджер', 'EnumManager')]:
        page(f'objects/{en}/methods/FindByCode.html', 'НайтиПоКоду', 'FindByCode',
             ru + '.<Имя объекта>', en + '.<Object name>')
    page('objects/ValueTable/events/Changed.html', 'Изменение', 'Changed', 'ТаблицаЗначений', 'ValueTable')
    page('objects/ValueTable/properties/Rows.html', 'Строки', 'Rows', 'ТаблицаЗначений', 'ValueTable')
    page('objects/Global context/properties/Example.html', 'Пример', 'Example')
    return _book(pages)


def query_book():
    return _book([('StrFind', 'СтрНайти', '<h1>Учебная функция</h1><p>СтрНайти(А, Б)</p>'
                   '<p>Учебная функция языка запросов.</p>')])
