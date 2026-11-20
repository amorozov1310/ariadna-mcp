"""Миниатюрные книги синтакс-помощника. Страницы и оглавление полностью синтетические."""

import io
import struct
import zipfile
from html import escape

_LAST = 0x7fffffff


def _zip(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in files:
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
    files = []
    for i, (path, name, page) in enumerate(pages, start=2):
        nodes.append('{%d,1,0,{0,0,{0,1,{"ru","%s"}},"/%s"}}' % (i, name, path))
        files.append((path, page))
    toc = '{' + ','.join([str(len(nodes)), *nodes]) + '}'
    return _container({'Book': b'', 'FileStorage': _zip(files), 'PackBlock': _zip([('0', toc)])})


def context_book(description='Учебное описание.'):
    pages = []

    def page(path, ru, en, owner='', owner_en='', version='', availability='Сервер', value_type=''):
        name = f'{ru} ({en})'
        title = f'{owner} ({owner_en})' if owner else name
        type_line = f'Тип: {escape(value_type)}.<br>' if value_type else ''
        html = (f'<h1>{escape(name)}</h1><p class="V8SH_title">{escape(title)}</p>'
                f'<p class="V8SH_heading">{escape(name)}</p>'
                f'<p class="V8SH_chapter">Синтаксис:</p>{ru}(&lt;Аргумент&gt;)'
                '<p class="V8SH_chapter">Параметры:</p>'
                '<div class="V8SH_rubric">&lt;Аргумент&gt; (обязательный)</div>'
                'Тип: Строка.<br>Учебный параметр.'
                f'<p class="V8SH_chapter">Описание:</p>{type_line}{escape(description)}'
                '<p class="V8SH_chapter">Возвращаемое значение:</p>Тип: Число.<br>Учебный результат.'
                f'<p class="V8SH_chapter">Доступность:</p>{availability}.')
        if version:
            html += f'<p class="V8SH_chapter">Использование в версии:</p>{escape(version)}'
        pages.append((path, ru, html))

    for ru, en in [('СтрНайти', 'StrFind'), ('СтрНайтиПример', 'StrFindExample')]:
        page(f'objects/Global context/methods/{en}.html', ru, en)
    page('objects/ValueTable.html', 'ТаблицаЗначений', 'ValueTable')
    page('objects/ValueTable/methods/Count.html', 'Количество', 'Count', 'ТаблицаЗначений',
         'ValueTable', 'Доступен начиная с версии 8.3.25.', 'Тонкий клиент, сервер')
    page('objects/Global context/methods/ModeExample.html', 'ПримерРежима', 'ModeExample',
         version='Доступен в режиме совместимости с версией 8.3.26 и последующими.')
    page('objects/Global context/methods/StrTemplate.html', 'СтрШаблон', 'StrTemplate',
         version='Доступен начиная с версии 8.3.6. Доступен в режиме совместимости с версией 8.3.5 и последующими.')
    page('objects/Structure/methods/Property.html', 'Свойство', 'Property', 'Структура', 'Structure')
    page('objects/Array/methods/Count.html', 'Количество', 'Count', 'Массив', 'Array')
    page('objects/ConstantManager/methods/Get.html', 'Получить', 'Get',
         'КонстантаМенеджер.<Имя константы>', 'ConstantManager.<Constant Name>')
    page('objects/Global context/properties/Catalogs.html', 'Справочники', 'Catalogs')
    page('objects/ConfigurationMetadataObject.html', 'ОбъектМетаданныхКонфигурация',
         'ConfigurationMetadataObject')
    page('objects/XMLCanonicalizingWriter.html', 'КаноническаяЗаписьXML', 'XMLCanonicalizingWriter')
    page('objects/XSLTransform.html', 'ПреобразованиеXSL', 'XSLTransform')
    page('objects/XMLStringProcessingManager.html', 'МенеджерОбработкиСтрокиXML', 'XMLStringProcessingManager')
    page('objects/Global context/properties/XMLStringProcessing.html',
         'ОбработкаСтрокиXML', 'XMLStringProcessing', value_type='МенеджерОбработкиСтрокиXML')
    page('objects/Global context/properties/Metadata.html', 'Метаданные', 'Metadata',
         value_type='ОбъектМетаданныхКонфигурация')
    page('objects/Global context/properties/ExternalDataProcessors.html',
         'ВнешниеОбработки', 'ExternalDataProcessors', value_type='ВнешниеОбработкиМенеджер',
         availability='Сервер, толстый клиент, внешнее соединение')
    page('objects/ExternalDataProcessorsManager/methods/Connect.html', 'Подключить', 'Connect',
         'ВнешниеОбработкиМенеджер', 'ExternalDataProcessorsManager', availability='Сервер, внешнее соединение')
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
    page('objects/Global context/properties/ExampleTable.html', 'ПримерТаблицы', 'ExampleTable',
         version='Доступен начиная с версии 8.3.27.', value_type='ТаблицаЗначений')
    return _book(pages)


def query_book():
    return _book([('StrFind', 'СтрНайти', '<h1>Учебная функция</h1><p>СтрНайти(А, Б)</p>'
                   '<p>Учебная функция языка запросов.</p>')])
