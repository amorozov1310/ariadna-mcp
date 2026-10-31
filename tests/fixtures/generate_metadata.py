"""
Генератор синтетических метаданных для тестов: отчёты по конфигурации
(report_ru.txt, report_en.txt) и XML-выгрузки тех же конфигураций без кода
(xml_meta_ru/, xml_meta_en/).

Тесты не должны зависеть от реальных конфигураций, поэтому всё собирается
здесь из выдуманной модели «демо-склада». Формат отчёта —
как у отчёта Конфигуратора («Отчёт по конфигурации»): UTF-16 LE с BOM,
иерархия табуляциями, заголовки объектов «- Вид.Имя[.Часть.Имя…]»,
свойства «Ключ: "Значение"», многострочные «Тип:» / «Состав:» с
кавычками на следующем уровне, служебные слова без значения
(СтандартныеРеквизиты, Характеристики), многострочные значения в кавычках,
вложенные подсистемы, дочерние элементы, которые разбор пропускает
(команды, макеты, шаблоны URL и методы HTTP-сервиса).

BOM перед первой строкой важен: в настоящем отчёте строка
«- Конфигурации.Имя» / «- Configurations.Name» начинается с него, поэтому
разбор не принимает её за объект, и свойства конфигурации остаются
свойствами конфигурации.

XML-выгрузка строится из разобранного отчёта (src/core/report_parser.py) —
так обе формы гарантированно описывают одну и ту же модель, и
tests/test_xml_metadata.py сверяет разбор XML с разбором отчёта. Формат —
как у «Выгрузить конфигурацию в файлы»: Configuration.xml со списком
объектов, файл на объект в каталоге вида (Catalogs/Склады.xml), вложенные
подсистемы в Subsystems/Родитель/Subsystems/, типы в виде cfg:CatalogRef.X /
xs:string с квалификаторами, состав подсистем английскими видами
(Catalog.X) и ссылками-UUID на объекты чужой конфигурации.

Запуск (перезаписывает файлы рядом со скриптом):
    python tests/fixtures/generate_metadata.py
"""

import re
import shutil
import sys
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

# ============================================
# СЛОВАРЬ ЯЗЫКОВ ОТЧЁТА
# ============================================

RU = {
    'config_plural': 'Конфигурации',
    'name': 'Имя', 'synonym': 'Синоним', 'comment': 'Комментарий',
    'version': 'Версия', 'vendor': 'Поставщик', 'compat': 'РежимСовместимости',
    'type': 'Тип', 'content': 'Состав', 'true': 'Истина', 'false': 'Ложь',
    'check': 'ПроверкаЗаполнения', 'check_error': 'ВыдаватьОшибку', 'check_none': 'НеПроверять',
    'indexing': 'Индексирование', 'index_yes': 'Индексировать', 'index_no': 'НеИндексировать',
    'use': 'Использование', 'use_item': 'ДляЭлемента', 'use_both': 'ДляГруппыИЭлемента',
    'attrs': 'Реквизиты', 'dims': 'Измерения', 'res': 'Ресурсы', 'ts': 'ТабличныеЧасти',
    'forms': 'Формы', 'enum_values': 'ЗначенияПеречисления', 'commands': 'Команды',
    'templates': 'Макеты',
    'std_attrs': 'СтандартныеРеквизиты', 'characteristics': 'Характеристики',
    'kinds': {
        'Catalog': 'Справочники', 'Document': 'Документы', 'InfoReg': 'РегистрыСведений',
        'AccumReg': 'РегистрыНакопления', 'Enum': 'Перечисления', 'Module': 'ОбщиеМодули',
        'Subsystem': 'Подсистемы', 'DefinedType': 'ОпределяемыеТипы', 'Language': 'Языки',
        'Style': 'Стили', 'Role': 'Роли', 'Picture': 'ОбщиеКартинки',
        'SessionParam': 'ПараметрыСеанса', 'Constant': 'Константы', 'Report': 'Отчеты',
        'DataProcessor': 'Обработки', 'CommonForm': 'ОбщиеФормы',
        'ScheduledJob': 'РегламентныеЗадания', 'EventSubscription': 'ПодпискиНаСобытия',
    },
    'module_flags': ['Глобальный', 'КлиентУправляемоеПриложение', 'Сервер',
                     'ВнешнееСоединение', 'КлиентОбычноеПриложение', 'ВызовСервера',
                     'Привилегированный'],
    'reuse': ('ПовторноеИспользованиеВозвращаемыхЗначений', 'НеИспользовать'),
    'belonging': ('ПринадлежностьОбъекта', 'Собственный'),
    'extended': 'ОбъектРасширяемойКонфигурации',
}

EN = {
    'config_plural': 'Configurations',
    'name': 'Name', 'synonym': 'Synonym', 'comment': 'Comment',
    'version': 'Version', 'vendor': 'Vendor', 'compat': 'CompatibilityMode',
    'type': 'Type', 'content': 'Content', 'true': 'True', 'false': 'False',
    'check': 'FillChecking', 'check_error': 'ShowError', 'check_none': 'DontCheck',
    'indexing': 'Indexing', 'index_yes': 'Index', 'index_no': 'DontIndex',
    'use': 'Use', 'use_item': 'ForItem', 'use_both': 'ForFolderAndItem',
    'attrs': 'Attributes', 'dims': 'Dimensions', 'res': 'Resources', 'ts': 'TabularSections',
    'forms': 'Forms', 'enum_values': 'EnumValues', 'commands': 'Commands',
    'templates': 'Templates',
    'std_attrs': 'StandardAttributes', 'characteristics': 'Characteristics',
    'kinds': {
        'Catalog': 'Catalogs', 'Document': 'Documents', 'InfoReg': 'InformationRegisters',
        'AccumReg': 'AccumulationRegisters', 'Enum': 'Enums', 'Module': 'CommonModules',
        'Subsystem': 'Subsystems', 'DefinedType': 'DefinedTypes', 'Language': 'Languages',
        'Style': 'Styles', 'Role': 'Roles', 'Picture': 'CommonPictures',
        'SessionParam': 'SessionParameters', 'Constant': 'Constants', 'Report': 'Reports',
        'DataProcessor': 'DataProcessors', 'CommonForm': 'CommonForms',
        'ScheduledJob': 'ScheduledJobs', 'EventSubscription': 'EventSubscriptions',
        'HTTPService': 'HTTPServices', 'Task': 'Tasks',
    },
    'module_flags': ['Global', 'ClientManagedApplication', 'Server', 'ExternalConnection',
                     'ClientOrdinaryApplication', 'ServerCall', 'Privileged'],
    'reuse': ('ReturnValuesReuse', 'DontUse'),
    'belonging': ('ObjectBelonging', 'Native'),
    'extended': 'ExtendedConfigurationObject',
}


# ============================================
# ЭМИТТЕР
# ============================================

class Report:
    def __init__(self, lang: dict):
        self.L = lang
        self.lines: list[str] = []

    def line(self, indent: int, text: str):
        self.lines.append('\t' * indent + text)

    def prop(self, indent: int, key: str, value: str):
        self.line(indent, f'{key}: "{value}"')

    def array(self, indent: int, key: str, values: list[str]):
        self.line(indent, f'{key}:')
        for v in values:
            self.line(indent + 1, f'"{v}"')

    def head(self, indent: int, path: str, name: str, synonym: str, comment: str = ''):
        L = self.L
        self.line(indent, f'- {path}')
        self.prop(indent + 1, L['name'], name)
        self.prop(indent + 1, L['synonym'], synonym)
        self.prop(indent + 1, L['comment'], comment)
        self.prop(indent + 1, *L['belonging'])
        self.prop(indent + 1, L['extended'], '')

    def field(self, indent: int, path: str, name: str, synonym: str, types: list[str],
              check: bool = False, indexed: bool = False, use: str | None = None):
        """Реквизит / измерение / ресурс / реквизит табличной части."""
        L = self.L
        self.head(indent, path, name, synonym)
        self.array(indent + 1, L['type'], types)
        # Служебные свойства, которые разбор пропускает, — как в настоящем отчёте.
        self.prop(indent + 1, 'PasswordMode' if L is EN else 'РежимПароля', L['false'])
        self.prop(indent + 1, 'Format' if L is EN else 'Формат', '')
        self.prop(indent + 1, L['check'], L['check_error'] if check else L['check_none'])
        if use:
            self.prop(indent + 1, L['use'], L[use])
        self.prop(indent + 1, L['indexing'], L['index_yes'] if indexed else L['index_no'])

    def form(self, indent: int, owner: str, name: str, synonym: str):
        self.head(indent, f'{owner}.{self.L["forms"]}.{name}', name, synonym)
        self.prop(indent + 1, 'FormType' if self.L is EN else 'ТипФормы',
                  'Managed' if self.L is EN else 'Управляемая')

    def text(self) -> str:
        return '\n'.join(self.lines) + '\n'


def write_report(rep: Report, path: Path):
    # UTF-16 LE с BOM, переводы строк Windows — как сохраняет Конфигуратор.
    data = '﻿' + rep.text().replace('\n', '\r\n')
    path.write_bytes(data.encode('utf-16-le'))


# ============================================
# РУССКИЙ ОТЧЁТ: ДЕМО-СКЛАД
# ============================================

def build_ru() -> Report:
    r = Report(RU)
    K = RU['kinds']

    # --- Конфигурация (первая строка — с BOM, см. докстринг) ---
    r.line(1, '- Конфигурации.ДемоКонфигурация')
    r.prop(2, 'Имя', 'ДемоКонфигурация')
    r.prop(2, 'Синоним', 'Демо-конфигурация')
    r.prop(2, 'Комментарий', 'Синтетическая конфигурация для тестов')
    r.prop(2, 'ПринадлежностьОбъекта', 'Собственный')
    r.prop(2, 'ОсновнойРежимЗапуска', 'УправляемоеПриложение')
    r.prop(2, 'ВариантВстроенногоЯзыка', 'Русский')
    r.line(2, 'ОсновныеРоли:')
    r.line(3, '"Роль.ПолныеПрава"')
    r.prop(2, 'Поставщик', 'Демо')
    r.prop(2, 'Версия', '1.2.3.4')
    r.line(2, 'ДополнительныеСловариПолнотекстовогоПоиска:')
    # Многострочное значение в кавычках — разбор его пропускает.
    r.line(2, 'ИспользуемаяФункциональностьМобильногоПриложения: "Функциональность:')
    r.line(2, '                                                   Биометрия = Ложь')
    r.line(2, '                                                   Геопозиционирование = Ложь')
    r.line(2, '                                                   Камера = Ложь"')
    r.prop(2, 'ОсновнойСтиль', 'Стиль.ОсновнойСтиль')
    r.prop(2, 'ОсновнойЯзык', 'Язык.Русский')
    r.prop(2, 'КраткаяИнформация', 'Демо-конфигурация')
    r.prop(2, 'РежимСовместимости', 'Версия8_3_24')

    # --- Языки, стили, роли, картинки, параметры сеанса, константы ---
    r.head(2, f'{K["Language"]}.Русский', 'Русский', 'Русский')
    r.prop(3, 'КодЯзыка', 'ru')
    r.head(2, f'{K["Style"]}.ОсновнойСтиль', 'ОсновнойСтиль', 'Основной стиль')
    for name, syn in (('ПолныеПрава', 'Полные права'), ('Кладовщик', 'Кладовщик'),
                      ('Менеджер', 'Менеджер по продажам')):
        r.head(2, f'{K["Role"]}.{name}', name, syn)
    for name, syn in (('Логотип', 'Логотип'), ('ЗначокСклада', 'Значок склада')):
        r.head(2, f'{K["Picture"]}.{name}', name, syn)
    r.head(2, f'{K["SessionParam"]}.ТекущийПользователь', 'ТекущийПользователь',
           'Текущий пользователь')
    r.array(3, 'Тип', ['СправочникСсылка.Сотрудники'])
    r.head(2, f'{K["Constant"]}.ОсновнаяВалюта', 'ОсновнаяВалюта', 'Основная валюта')
    r.array(3, 'Тип', ['СправочникСсылка.Валюты'])

    # --- Определяемые типы ---
    for name, syn, types in (
            ('Сумма', 'Сумма', ['Число(15, 2)']),
            ('Количество', 'Количество', ['Число(15, 3, Неотрицательный)']),
            ('ВладелецФайлов', 'Владелец файлов',
             ['СправочникСсылка.Номенклатура', 'ДокументСсылка.ПоступлениеТоваров'])):
        r.head(2, f'{K["DefinedType"]}.{name}', name, syn)
        r.array(3, 'Тип', types)

    # --- Общие модули: (имя, синоним, флаги) ---
    for name, syn, flags in (
            ('ОбщегоНазначения', 'Общего назначения',
             {'Глобальный', 'КлиентУправляемоеПриложение', 'Сервер', 'ВнешнееСоединение'}),
            ('ОбщиеНаСервере', 'Общие на сервере', {'Сервер', 'ВызовСервера', 'Привилегированный'}),
            ('ОбщиеНаКлиенте', 'Общие на клиенте', {'КлиентУправляемоеПриложение'}),
            ('ЦенообразованиеСервер', 'Ценообразование (сервер)', {'Сервер'}),
            ('РаботаСоСкладом', 'Работа со складом', {'Сервер', 'ВнешнееСоединение'})):
        r.head(2, f'{K["Module"]}.{name}', name, syn)
        for flag in RU['module_flags']:
            r.prop(3, flag, 'Истина' if flag in flags else 'Ложь')
        r.prop(3, *RU['reuse'])

    # --- Перечисления ---
    for name, syn, values in (
            ('ТипыЦен', 'Типы цен', [('Закупочная', 'Закупочная'), ('Продажная', 'Продажная')]),
            ('ТипыСкладов', 'Типы складов', [('Оптовый', 'Оптовый'), ('Розничный', 'Розничный')]),
            ('СтавкиНДС', 'Ставки НДС', [('БезНДС', 'Без НДС'), ('НДС10', 'НДС 10%'),
                                         ('НДС20', 'НДС 20%')])):
        path = f'{K["Enum"]}.{name}'
        r.head(2, path, name, syn)
        r.line(3, 'СтандартныеРеквизиты')
        r.line(3, 'Характеристики')
        r.prop(3, 'БыстрыйВыбор', 'Истина')
        for vname, vsyn in values:
            r.head(3, f'{path}.ЗначенияПеречисления.{vname}', vname, vsyn)

    # --- Справочники: (имя, синоним, реквизиты, формы, табличные части) ---
    catalogs = [
        ('Номенклатура', 'Номенклатура', [
            ('Артикул', 'Артикул', ['Строка(25, Переменная)'], False, True),
            ('ВидНоменклатуры', 'Вид номенклатуры', ['СправочникСсылка.ВидыНоменклатуры'], True, False),
            ('ЕдиницаИзмерения', 'Единица измерения', ['СправочникСсылка.ЕдиницыИзмерения'], True, False),
            ('Производитель', 'Производитель', ['СправочникСсылка.Контрагенты'], False, False),
            ('СтавкаНДС', 'Ставка НДС', ['ПеречислениеСсылка.СтавкиНДС'], True, False),
            ('Вес', 'Вес', ['ОпределяемыйТип.Количество'], False, False),
            ('Описание', 'Описание', ['Строка(0, Переменная)'], False, False),
        ], [('ФормаЭлемента', 'Форма элемента'), ('ФормаСписка', 'Форма списка'),
            ('ФормаВыбора', 'Форма выбора')],
         [('ДополнительныеРеквизиты', [('Свойство', 'Свойство', ['Строка(50, Переменная)']),
                                       ('Значение', 'Значение', ['Строка(100, Переменная)', 'Число(15, 2)'])])]),
        ('ВидыНоменклатуры', 'Виды номенклатуры', [
            ('ТипНоменклатуры', 'Тип номенклатуры', ['Строка(20, Переменная)'], False, False),
        ], [('ФормаЭлемента', 'Форма элемента')], []),
        ('ЕдиницыИзмерения', 'Единицы измерения', [
            ('Коэффициент', 'Коэффициент', ['Число(10, 3)'], False, False),
        ], [], []),
        ('Контрагенты', 'Контрагенты', [
            ('ИНН', 'ИНН', ['Строка(12, Переменная)'], False, True),
            ('КПП', 'КПП', ['Строка(9, Переменная)'], False, False),
            ('ЮридическийАдрес', 'Юридический адрес', ['Строка(300, Переменная)'], False, False),
            ('ОсновнойДоговор', 'Основной договор', ['Строка(50, Переменная)'], False, False),
        ], [('ФормаЭлемента', 'Форма элемента'), ('ФормаСписка', 'Форма списка')], []),
        ('Организации', 'Организации', [
            ('ИНН', 'ИНН', ['Строка(12, Переменная)'], False, True),
        ], [('ФормаЭлемента', 'Форма элемента')], []),
        ('Склады', 'Склады', [
            ('ТипСклада', 'Тип склада', ['ПеречислениеСсылка.ТипыСкладов'], True, False),
            ('Ответственный', 'Ответственный', ['СправочникСсылка.Сотрудники'], False, False),
        ], [('ФормаЭлемента', 'Форма элемента'), ('ФормаСписка', 'Форма списка')], []),
        ('Сотрудники', 'Сотрудники', [
            ('Должность', 'Должность', ['Строка(100, Переменная)'], False, False),
        ], [], []),
        ('Валюты', 'Валюты', [
            ('КодВалюты', 'Код валюты', ['Строка(3, Фиксированная)'], False, True),
        ], [], []),
    ]
    for name, syn, attrs, forms, tss in catalogs:
        path = f'{K["Catalog"]}.{name}'
        r.head(2, path, name, syn)
        r.prop(3, 'Иерархический', 'Ложь')
        r.line(3, 'Владельцы:')
        r.prop(3, 'ДлинаНаименования', '150')
        r.line(3, 'СтандартныеРеквизиты')
        r.line(3, 'Характеристики')
        r.array(3, 'ВводПоСтроке', ['Наименование'])
        r.array(3, 'ПоляБлокировкиДанных', [''])
        if forms:
            r.prop(3, 'ОсновнаяФормаОбъекта', f'Справочник.{name}.Форма.{forms[0][0]}')
        for aname, asyn, types, check, idx in attrs:
            r.field(3, f'{path}.Реквизиты.{aname}', aname, asyn, types, check, idx, 'use_item')
        for tsname, tsattrs in tss:
            tspath = f'{path}.ТабличныеЧасти.{tsname}'
            r.head(3, tspath, tsname, tsname)
            r.prop(4, 'ПроверкаЗаполнения', 'НеПроверять')
            r.line(4, 'СтандартныеРеквизиты')
            for aname, asyn, types in tsattrs:
                r.field(4, f'{tspath}.Реквизиты.{aname}', aname, asyn, types)
        for fname, fsyn in forms:
            r.form(3, path, fname, fsyn)
        if name == 'Номенклатура':
            # Дочерние элементы, которые разбор пропускает: их Имя не должно
            # перезаписать имя справочника.
            r.head(3, f'{path}.Команды.ПечатьЭтикеток', 'ПечатьЭтикеток', 'Печать этикеток')
            r.head(3, f'{path}.Макеты.Этикетка', 'Этикетка', 'Этикетка')

    # --- Документы ---
    goods_ts = [('Номенклатура', 'Номенклатура', ['СправочникСсылка.Номенклатура']),
                ('Количество', 'Количество', ['ОпределяемыйТип.Количество']),
                ('Цена', 'Цена', ['ОпределяемыйТип.Сумма']),
                ('Сумма', 'Сумма', ['ОпределяемыйТип.Сумма'])]
    documents = [
        ('ПоступлениеТоваров', 'Поступление товаров', [
            ('Организация', 'Организация', ['СправочникСсылка.Организации'], True),
            ('Контрагент', 'Контрагент', ['СправочникСсылка.Контрагенты'], True),
            ('Склад', 'Склад', ['СправочникСсылка.Склады'], True),
            ('Валюта', 'Валюта', ['СправочникСсылка.Валюты'], False),
        ], goods_ts),
        ('РеализацияТоваров', 'Реализация товаров', [
            ('Организация', 'Организация', ['СправочникСсылка.Организации'], True),
            ('Контрагент', 'Контрагент', ['СправочникСсылка.Контрагенты'], True),
            ('Склад', 'Склад', ['СправочникСсылка.Склады'], True),
        ], goods_ts),
        ('ПеремещениеТоваров', 'Перемещение товаров', [
            ('СкладОтправитель', 'Склад-отправитель', ['СправочникСсылка.Склады'], True),
            ('СкладПолучатель', 'Склад-получатель', ['СправочникСсылка.Склады'], True),
        ], goods_ts[:2]),
        ('ИнвентаризацияТоваров', 'Инвентаризация товаров', [
            ('Склад', 'Склад', ['СправочникСсылка.Склады'], True),
        ], [('Номенклатура', 'Номенклатура', ['СправочникСсылка.Номенклатура']),
            ('КоличествоФакт', 'Количество (факт)', ['ОпределяемыйТип.Количество']),
            ('КоличествоУчет', 'Количество (учет)', ['ОпределяемыйТип.Количество'])]),
        ('ВозвратТоваровПоставщику', 'Возврат товаров поставщику', [
            ('Контрагент', 'Контрагент', ['СправочникСсылка.Контрагенты'], True),
            ('ДокументПоступления', 'Документ поступления', ['ДокументСсылка.ПоступлениеТоваров'], False),
        ], goods_ts),
    ]
    for name, syn, attrs, ts in documents:
        path = f'{K["Document"]}.{name}'
        r.head(2, path, name, syn)
        r.prop(3, 'Проведение', 'Разрешить')
        r.line(3, 'СтандартныеРеквизиты')
        r.line(3, 'Характеристики')
        r.line(3, 'ВводитсяНаОсновании:')
        r.array(3, 'Движения', _RECORDS_RU.get(path, []))
        for aname, asyn, types, check in attrs:
            r.field(3, f'{path}.Реквизиты.{aname}', aname, asyn, types, check)
        tspath = f'{path}.ТабличныеЧасти.Товары'
        r.head(3, tspath, 'Товары', 'Товары')
        r.line(4, 'СтандартныеРеквизиты')
        for aname, asyn, types in ts:
            r.field(4, f'{tspath}.Реквизиты.{aname}', aname, asyn, types)
        r.form(3, path, 'ФормаДокумента', 'Форма документа')
        r.form(3, path, 'ФормаСписка', 'Форма списка')

    # --- Регистры сведений: (имя, синоним, измерения, ресурсы) ---
    for name, syn, dims, ress in (
            ('ЦеныНоменклатуры', 'Цены номенклатуры',
             [('Номенклатура', ['СправочникСсылка.Номенклатура']), ('ТипЦены', ['ПеречислениеСсылка.ТипыЦен'])],
             [('Цена', ['ОпределяемыйТип.Сумма'])]),
            ('КурсыВалют', 'Курсы валют',
             [('Валюта', ['СправочникСсылка.Валюты'])],
             [('Курс', ['Число(10, 4)']), ('Кратность', ['Число(10, 0)'])]),
            ('ШтрихкодыНоменклатуры', 'Штрихкоды номенклатуры',
             [('Штрихкод', ['Строка(200, Переменная)'])],
             [('Номенклатура', ['СправочникСсылка.Номенклатура'])])):
        path = f'{K["InfoReg"]}.{name}'
        r.head(2, path, name, syn)
        r.line(3, 'СтандартныеРеквизиты')
        r.prop(3, 'ПериодичностьРегистраСведений', 'День')
        for dname, types in dims:
            r.field(3, f'{path}.Измерения.{dname}', dname, dname, types, False, True)
        for rname, types in ress:
            r.field(3, f'{path}.Ресурсы.{rname}', rname, rname, types)
        r.form(3, path, 'ФормаСписка', 'Форма списка')

    # --- Регистры накопления ---
    for name, syn, dims, ress in (
            ('ТоварыНаСкладах', 'Товары на складах',
             [('Номенклатура', ['СправочникСсылка.Номенклатура']), ('Склад', ['СправочникСсылка.Склады'])],
             [('Количество', ['ОпределяемыйТип.Количество'])]),
            ('Продажи', 'Продажи',
             [('Номенклатура', ['СправочникСсылка.Номенклатура']), ('Контрагент', ['СправочникСсылка.Контрагенты'])],
             [('Количество', ['ОпределяемыйТип.Количество']), ('Сумма', ['ОпределяемыйТип.Сумма'])])):
        path = f'{K["AccumReg"]}.{name}'
        r.head(2, path, name, syn)
        r.prop(3, 'ВидРегистра', 'Остатки')
        r.line(3, 'СтандартныеРеквизиты')
        for dname, types in dims:
            r.field(3, f'{path}.Измерения.{dname}', dname, dname, types)
        for rname, types in ress:
            r.field(3, f'{path}.Ресурсы.{rname}', rname, rname, types)

    # --- Отчёты, обработки, общие формы, регламентные задания, подписки ---
    r.head(2, f'{K["Report"]}.АнализПродаж', 'АнализПродаж', 'Анализ продаж')
    r.form(3, f'{K["Report"]}.АнализПродаж', 'ФормаОтчета', 'Форма отчета')
    r.head(2, f'{K["DataProcessor"]}.ЗагрузкаЦен', 'ЗагрузкаЦен', 'Загрузка цен')
    r.form(3, f'{K["DataProcessor"]}.ЗагрузкаЦен', 'Форма', 'Форма')
    r.head(2, f'{K["CommonForm"]}.ВыборПериода', 'ВыборПериода', 'Выбор периода')
    r.head(2, f'{K["ScheduledJob"]}.ОбновлениеКурсов', 'ОбновлениеКурсов', 'Обновление курсов валют')
    r.prop(3, 'ИмяМетода', 'ОбщиеНаСервере.ОбновитьКурсыВалют')
    r.head(2, f'{K["EventSubscription"]}.ПроверкаЗаполнения', 'ПроверкаЗаполнения',
           'Проверка заполнения')

    # --- Подсистемы, в том числе вложенная ---
    for name, syn, content in (
            ('Склад', 'Склад', ['Справочник.Склады', 'Справочник.Номенклатура',
                                'Документ.ПеремещениеТоваров', 'Документ.ИнвентаризацияТоваров',
                                'РегистрНакопления.ТоварыНаСкладах']),
            ('Закупки', 'Закупки', ['Документ.ПоступлениеТоваров', 'Документ.ВозвратТоваровПоставщику',
                                    'Справочник.Контрагенты']),
            ('Продажи', 'Продажи', ['Документ.РеализацияТоваров', 'РегистрНакопления.Продажи',
                                    'РегистрСведений.ЦеныНоменклатуры'])):
        path = f'{K["Subsystem"]}.{name}'
        r.head(2, path, name, syn)
        r.prop(3, 'ВключатьВКомандныйИнтерфейс', 'Истина')
        r.array(3, 'Состав', content)
        if name == 'Продажи':
            npath = f'{path}.Подсистемы.Отчеты'
            r.head(3, npath, 'Отчеты', 'Отчеты по продажам')
            r.array(4, 'Состав', ['Отчет.АнализПродаж'])
    r.head(2, f'{K["Subsystem"]}.Администрирование', 'Администрирование', 'Администрирование')
    r.array(3, 'Состав', ['Обработка.ЗагрузкаЦен', 'Константа.ОсновнаяВалюта'])
    return r


# ============================================
# АНГЛИЙСКИЙ ОТЧЁТ: DEMO APP
# ============================================

def build_en() -> Report:
    r = Report(EN)
    K = EN['kinds']
    T, F = 'True', 'False'

    r.line(1, '- Configurations.DemoApp')
    r.prop(2, 'Name', 'DemoApp')
    r.prop(2, 'Synonym', 'Demo application')
    r.prop(2, 'Comment', 'Synthetic configuration for tests')
    r.prop(2, 'ObjectBelonging', 'Native')
    r.prop(2, 'DefaultRunMode', 'ManagedApplication')
    r.prop(2, 'ScriptVariant', 'English')
    r.line(2, 'DefaultRoles:')
    r.line(3, '"Role.FullAccess"')
    # Кавычки внутри значения — как у настоящих названий поставщиков.
    r.line(2, 'Vendor: "Demo "Example" Ltd"')
    r.prop(2, 'Version', '2.0.1')
    r.prop(2, 'CompatibilityMode', 'DontUse')

    r.head(2, f'{K["Language"]}.English', 'English', 'English')
    r.prop(3, 'LanguageCode', 'en')
    r.head(2, f'{K["Style"]}.MainStyle', 'MainStyle', 'Main style')
    for name, syn in (('FullAccess', 'Full access'), ('Operator', 'Operator'),
                      ('Auditor', 'Auditor')):
        r.head(2, f'{K["Role"]}.{name}', name, syn)
    for name in ('Logo', 'TaskIcon'):
        r.head(2, f'{K["Picture"]}.{name}', name, name)
    r.head(2, f'{K["SessionParam"]}.CurrentUser', 'CurrentUser', 'Current user')
    r.array(3, 'Type', ['CatalogRef.Users'])
    r.head(2, f'{K["Constant"]}.ServiceUrl', 'ServiceUrl', 'Service URL')
    r.array(3, 'Type', ['String(200, Variable)'])

    # --- Common modules ---
    for name, syn, flags in (
            ('ServerCommon', 'Server common', {'Server', 'ServerCall', 'Privileged'}),
            ('ClientCommon', 'Client common', {'ClientManagedApplication'}),
            ('GlobalHelpers', 'Global helpers', {'Global', 'ClientManagedApplication', 'Server'}),
            ('IntegrationServer', 'Integration (server)', {'Server', 'ExternalConnection'}),
            ('NotificationsServer', 'Notifications (server)', {'Server'}),
            ('JobsServer', 'Jobs (server)', {'Server', 'ExternalConnection'})):
        r.head(2, f'{K["Module"]}.{name}', name, syn)
        for flag in EN['module_flags']:
            r.prop(3, flag, T if flag in flags else F)
        r.prop(3, *EN['reuse'])

    # --- Enums ---
    for name, syn, values in (
            ('UserType', 'User type', [('Internal', 'Internal'), ('External', 'External')]),
            ('Priority', 'Priority', [('Low', 'Low'), ('Normal', 'Normal'), ('High', 'High')]),
            ('Channel', 'Notification channel', [('Email', 'Email'), ('Sms', 'SMS')])):
        path = f'{K["Enum"]}.{name}'
        r.head(2, path, name, syn)
        r.line(3, 'StandardAttributes')
        r.line(3, 'Characteristics')
        for vname, vsyn in values:
            r.head(3, f'{path}.EnumValues.{vname}', vname, vsyn)

    # --- Catalogs ---
    catalogs = [
        ('Users', 'Пользователи', [
            ('Login', 'Логин', ['String(50, Variable)'], True, True),
            ('FullName', 'Полное имя', ['String(150, Variable)'], False, False),
            ('Department', 'Подразделение', ['CatalogRef.Departments'], False, False),
            ('UserType', 'Тип пользователя', ['EnumRef.UserType'], True, False),
            ('Role', 'Роль', ['CatalogRef.Roles'], False, False),
        ], ['ItemForm', 'ListForm', 'ChoiceForm', 'ResetPassword'], []),
        ('Roles', 'Роли пользователей', [], ['ItemForm'], []),
        ('Departments', 'Подразделения', [
            ('Head', 'Руководитель', ['CatalogRef.Employees'], False, False),
        ], ['ItemForm'], []),
        ('Employees', 'Сотрудники', [
            ('Position', 'Должность', ['String(100, Variable)'], False, False),
        ], ['ItemForm', 'ListForm'], []),
        ('Products', 'Номенклатура', [
            ('Sku', 'Артикул', ['String(25, Variable)'], False, True),
            ('Unit', 'Единица', ['CatalogRef.Units'], True, False),
        ], ['ItemForm', 'ListForm'], []),
        ('Units', 'Единицы', [], [], []),
        ('Customers', 'Клиенты', [
            ('TaxId', 'ИНН', ['String(12, Variable)'], False, True),
        ], ['ItemForm'], []),
        ('Warehouses', 'Склады', [], ['ItemForm'], []),
        ('Currencies', 'Валюты', [], [], []),
        ('Projects', 'Проекты', [
            ('Owner', 'Владелец', ['CatalogRef.Users'], False, False),
        ], [], []),
        ('Tags', 'Метки', [], [], []),
        ('EventKinds', 'Виды событий', [
            ('Active', 'Активный', ['Boolean'], False, False),
        ], ['ItemForm'], [('Schedule', [('IntervalDays', 'Интервал, дней', ['Number(10, 2)']),
                                        ('Channel', 'Канал', ['EnumRef.Channel'])])]),
    ]
    for name, syn, attrs, forms, tss in catalogs:
        path = f'{K["Catalog"]}.{name}'
        r.head(2, path, name, syn)
        r.prop(3, 'Hierarchical', F)
        r.line(3, 'Owners:')
        r.line(3, 'StandardAttributes')
        r.line(3, 'Characteristics')
        # Многострочное значение элемента массива — как в настоящем отчёте.
        r.line(3, 'InputByString:')
        r.line(4, '"Description,')
        r.line(4, ' Code"')
        r.array(3, 'DataLockFields', [''])
        for aname, asyn, types, check, idx in attrs:
            r.field(3, f'{path}.Attributes.{aname}', aname, asyn, types, check, idx, 'use_item')
        for tsname, tsattrs in tss:
            tspath = f'{path}.TabularSections.{tsname}'
            r.head(3, tspath, tsname, tsname)
            r.prop(4, 'FillChecking', 'DontCheck')
            r.line(4, 'StandardAttributes')
            for aname, asyn, types in tsattrs:
                r.field(4, f'{tspath}.Attributes.{aname}', aname, asyn, types)
        for fname in forms:
            r.form(3, path, fname, fname)
        if name == 'Users':
            r.head(3, f'{path}.Commands.SendInvite', 'SendInvite', 'Send invite')

    # --- Information register ---
    path = f'{K["InfoReg"]}.JobStatus'
    r.head(2, path, 'JobStatus', 'Job status')
    r.line(3, 'StandardAttributes')
    r.prop(3, 'InformationRegisterPeriodicity', 'Second')
    r.field(3, f'{path}.Dimensions.Job', 'Job', 'Job', ['CatalogRef.Projects'], False, True)
    r.field(3, f'{path}.Resources.State', 'State', 'State', ['String(20, Variable)'])
    r.field(3, f'{path}.Resources.Attempts', 'Attempts', 'Attempts', ['Number(5, 0)'])

    # --- Tasks, reports, data processors, common forms, scheduled jobs ---
    r.head(2, f'{K["Task"]}.Approval', 'Approval', 'Approval')
    r.head(2, f'{K["Report"]}.UserActivity', 'UserActivity', 'User activity')
    r.head(2, f'{K["DataProcessor"]}.ImportUsers', 'ImportUsers', 'Import users')
    r.head(2, f'{K["CommonForm"]}.JobsLog', 'JobsLog', 'Jobs log')
    r.head(2, f'{K["ScheduledJob"]}.RunJobs', 'RunJobs', 'Run jobs')
    r.prop(3, 'MethodName', 'JobsServer.RunJobs')

    # --- HTTP service: шаблоны URL и методы разбор пропускает ---
    path = f'{K["HTTPService"]}.Api'
    r.head(2, path, 'Api', 'Public API')
    r.prop(3, 'RootURL', 'api')
    r.head(3, f'{path}.URLTemplates.status', 'status', 'Status')
    r.prop(4, 'Template', '/status')
    r.head(4, f'{path}.URLTemplates.status.Methods.Get', 'Get', 'Get status')
    r.prop(5, 'HTTPMethod', 'GET')
    r.prop(5, 'Handler', 'StatusGet')

    # --- Subsystems (в том числе вложенная, с пустыми элементами Content) ---
    path = f'{K["Subsystem"]}.Administration'
    r.head(2, path, 'Administration', 'Администрирование')
    r.array(3, 'Content', ['Catalog.Users', 'Catalog.Departments', 'Role.FullAccess',
                           'DataProcessor.ImportUsers'])
    path = f'{K["Subsystem"]}.Operations'
    r.head(2, path, 'Operations', 'Операции')
    r.array(3, 'Content', ['Catalog.Projects', 'InformationRegister.JobStatus', 'Task.Approval'])
    r.head(3, f'{path}.Subsystems.Jobs', 'Jobs', 'Задания')
    r.array(4, 'Content', ['', '', ''])
    path = f'{K["Subsystem"]}.Sales'
    r.head(2, path, 'Sales', 'Продажи')
    r.array(3, 'Content', ['Catalog.Products', 'Catalog.Customers', 'Report.UserActivity'])
    return r


# ============================================
# XML-ВЫГРУЗКА ИЗ РАЗОБРАННОГО ОТЧЁТА
# ============================================

_NS = ('xmlns="http://v8.1c.ru/8.3/MDClasses" xmlns:cfg="http://v8.1c.ru/8.1/data/enterprise/current-config" '
       'xmlns:v8="http://v8.1c.ru/8.1/data/core" xmlns:xr="http://v8.1c.ru/8.3/xcf/readable" '
       'xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
       'version="2.20"')
_UUID_NS = uuid.UUID('0b7c1d8e-5f3a-4c2e-9a61-2d4e8f0a1b3c')

# Обратные таблицы — только для того, что встречается в модели выше.
_REF_EN = {'СправочникСсылка': 'CatalogRef', 'ДокументСсылка': 'DocumentRef',
           'ПеречислениеСсылка': 'EnumRef', 'ОпределяемыйТип': 'DefinedType'}
# Движения документов (отчёт: «Движения», выгрузка: RegisterRecords).
_RECORDS_RU = {
    'Документ.РеализацияТоваров': ['РегистрНакопления.ТоварыНаСкладах', 'РегистрНакопления.Продажи'],
}
for _doc in ('ПоступлениеТоваров', 'ПеремещениеТоваров', 'ИнвентаризацияТоваров',
             'ВозвратТоваровПоставщику'):
    _RECORDS_RU[f'Документ.{_doc}'] = ['РегистрНакопления.ТоварыНаСкладах']
# Свойства регистров: ключ отчёта (RU/EN) → (тег выгрузки, значения RU → EN).
_REGISTER_PROPS = {
    'ВидРегистра': ('RegisterType', {'Остатки': 'Balance', 'Обороты': 'Turnovers'}),
    'RegisterType': ('RegisterType', {}),
    'ПериодичностьРегистраСведений': ('InformationRegisterPeriodicity',
                                      {'День': 'Day', 'Непериодический': 'Nonperiodical'}),
    'InformationRegisterPeriodicity': ('InformationRegisterPeriodicity', {}),
}
_VALUE_EN = {'ВыдаватьОшибку': 'ShowError', 'НеПроверять': 'DontCheck', 'ДляЭлемента': 'ForItem',
             'НеИспользовать': 'DontUse'}
_MODULE_FLAGS = ('Global', 'ClientManagedApplication', 'Server', 'ExternalConnection',
                 'ServerCall', 'Privileged')
_MODULE_ATTRS = ('is_global', 'is_client', 'is_server', 'is_external', 'server_call', 'is_privileged')


def _uid(*parts):
    return str(uuid.uuid5(_UUID_NS, '/'.join(parts)))


def _syn(text, lang, ind):
    if not text:
        return f'{ind}<Synonym/>\n'
    return (f'{ind}<Synonym>\n{ind}\t<v8:item>\n{ind}\t\t<v8:lang>{lang}</v8:lang>\n'
            f'{ind}\t\t<v8:content>{escape(text)}</v8:content>\n{ind}\t</v8:item>\n{ind}</Synonym>\n')


def _type_xml(type_desc, ind):
    """«Строка(25, Переменная), СправочникСсылка.X» → <Type> c v8:Type и квалификаторами."""
    if not type_desc:
        return ''
    types, quals = [], []
    for part in re.split(r',\s(?![^()]*\))', type_desc):
        m = re.fullmatch(r'(?:Строка|String)\((\d+), (\w+)\)', part)
        if m:
            types.append('<v8:Type>xs:string</v8:Type>')
            fixed = m.group(2) in ('Фиксированная', 'Fixed')
            quals.append(f'<v8:StringQualifiers><v8:Length>{m.group(1)}</v8:Length>'
                         f'<v8:AllowedLength>{"Fixed" if fixed else "Variable"}</v8:AllowedLength>'
                         '</v8:StringQualifiers>')
            continue
        m = re.fullmatch(r'(?:Число|Number)\((\d+), (\d+)(, \w+)?\)', part)
        if m:
            types.append('<v8:Type>xs:decimal</v8:Type>')
            sign = 'Nonnegative' if m.group(3) else 'Any'
            quals.append(f'<v8:NumberQualifiers><v8:Digits>{m.group(1)}</v8:Digits>'
                         f'<v8:FractionDigits>{m.group(2)}</v8:FractionDigits>'
                         f'<v8:AllowedSign>{sign}</v8:AllowedSign></v8:NumberQualifiers>')
            continue
        if part in ('Булево', 'Boolean'):
            types.append('<v8:Type>xs:boolean</v8:Type>')
            continue
        head, _, name = part.partition('.')
        head = _REF_EN.get(head, head)
        tag = 'TypeSet' if head == 'DefinedType' else 'Type'
        types.append(f'<v8:{tag}>cfg:{head}.{name}</v8:{tag}>')
    inner = ''.join(f'{ind}\t{t}\n' for t in types + quals)
    return f'{ind}<Type>\n{inner}{ind}</Type>\n'


def _attr_xml(tag, attr, lang, ind):
    value = lambda v: _VALUE_EN.get(v, v)
    return (f'{ind}<{tag} uuid="{_uid(tag, attr.name, attr.type_desc)}">\n{ind}\t<Properties>\n'
            f'{ind}\t\t<Name>{attr.name}</Name>\n' + _syn(attr.synonym, lang, ind + '\t\t') +
            f'{ind}\t\t<Comment/>\n' + _type_xml(attr.type_desc, ind + '\t\t') +
            f'{ind}\t\t<FillChecking>{value(attr.check_fill)}</FillChecking>\n' +
            (f'{ind}\t\t<Use>{value(attr.usage)}</Use>\n' if attr.usage else '') +
            f'{ind}\t\t<Indexing>{"Index" if attr.is_indexed else "DontIndex"}</Indexing>\n'
            f'{ind}\t</Properties>\n{ind}</{tag}>\n')


def _object_xml(obj, kind_en, lang, nested_subsystems, english):
    t = '\t\t\t'
    props = f'{t}<Name>{obj.name}</Name>\n' + _syn(obj.synonym, lang, t) + f'{t}<Comment/>\n'
    if obj.module_info:
        mi = obj.module_info
        for flag, attr in zip(_MODULE_FLAGS, _MODULE_ATTRS):
            props += f'{t}<{flag}>{"true" if getattr(mi, attr) else "false"}</{flag}>\n'
        props += f'{t}<ReturnValuesReuse>{_VALUE_EN.get(mi.reuse, mi.reuse)}</ReturnValuesReuse>\n'
    if obj.type_desc:
        props += _type_xml(obj.type_desc, t)
    for key, value in obj.properties.items():
        if key in _REGISTER_PROPS:
            tag, values = _REGISTER_PROPS[key]
            props += f'{t}<{tag}>{values.get(value, value)}</{tag}>\n'
    if kind_en == 'InformationRegister':
        props += f'{t}<WriteMode>Independent</WriteMode>\n'
    records = _RECORDS_RU.get(obj.full_name, [])
    if records:
        items = [f'{_KIND_EN[r.split(".", 1)[0]]}.{r.split(".", 1)[1]}' for r in records]
        props += f'{t}<RegisterRecords>\n' + ''.join(
            f'{t}\t<xr:Item xsi:type="xr:MDObjectRef">{i}</xr:Item>\n' for i in items) + f'{t}</RegisterRecords>\n'
    if obj.subsystem_info is not None:
        items = []
        for ref in obj.subsystem_info.content:
            if not ref:  # объект чужой конфигурации — в выгрузке только его UUID
                items.append(_uid('foreign', obj.full_name, str(len(items))))
                continue
            head, _, name = ref.partition('.')
            items.append(f'{_KIND_EN.get(head, head)}.{name}')
        props += f'{t}<Content>\n' + ''.join(
            f'{t}\t<xr:Item xsi:type="xr:MDObjectRef">{i}</xr:Item>\n' for i in items) + f'{t}</Content>\n'

    children = ''
    c = '\t\t\t'
    tags = {'Реквизит': 'Attribute', 'Измерение': 'Dimension', 'Ресурс': 'Resource'}
    for attr in obj.attributes:
        children += _attr_xml(tags[attr.kind], attr, lang, c)
    for ts_name, ts_attrs in obj.tabular_sections.items():
        children += (f'{c}<TabularSection uuid="{_uid(obj.full_name, ts_name)}">\n{c}\t<Properties>\n'
                     f'{c}\t\t<Name>{ts_name}</Name>\n{c}\t\t<Synonym/>\n{c}\t\t<Comment/>\n'
                     f'{c}\t</Properties>\n{c}\t<ChildObjects>\n'
                     + ''.join(_attr_xml('Attribute', a, lang, c + '\t\t') for a in ts_attrs)
                     + f'{c}\t</ChildObjects>\n{c}</TabularSection>\n')
    for form in obj.forms:
        children += f'{c}<Form>{form.name}</Form>\n'
    for ev in obj.enum_values:
        children += (f'{c}<EnumValue uuid="{_uid(obj.full_name, ev.name)}">\n{c}\t<Properties>\n'
                     f'{c}\t\t<Name>{ev.name}</Name>\n' + _syn(ev.synonym, lang, c + '\t\t') +
                     f'{c}\t\t<Comment/>\n{c}\t</Properties>\n{c}</EnumValue>\n')
    for name in nested_subsystems:
        children += f'{c}<Subsystem>{name}</Subsystem>\n'
    # Дочерний элемент, который разбор пропускает, — как команды в настоящей выгрузке.
    if obj.forms:
        children += f'{c}<Command uuid="{_uid(obj.full_name, "cmd")}"><Properties><Name>Открыть</Name></Properties></Command>\n'

    body = f'\t\t<ChildObjects>\n{children}\t\t</ChildObjects>\n' if children else '\t\t<ChildObjects/>\n'
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_NS}>\n'
            f'\t<{kind_en} uuid="{_uid(obj.full_name)}">\n\t\t<Properties>\n{props}\t\t</Properties>\n'
            f'{body}\t</{kind_en}>\n</MetaDataObject>\n')


def write_xml_dump(report: Path, out: Path, english: bool):
    from src.core.report_parser import OBJECT_TYPE_MAP, parse_report

    kinds = {ru: (en, plural) for plural, (ru, en) in OBJECT_TYPE_MAP.items() if plural.isascii()}
    global _KIND_EN
    _KIND_EN = {ru: en for ru, (en, _plural) in kinds.items()}
    lang = 'en' if english else 'ru'
    config, objects = parse_report(report)

    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    def write(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\xef\xbb\xbf' + text.encode('utf-8'))

    # Вложенные подсистемы: full_name «Подсистема.A.Подсистемы.B».
    sub_word = '.Subsystems.' if english else '.Подсистемы.'
    nested: dict[str, list[str]] = {}
    for obj in objects:
        if obj.kind == 'Подсистема' and sub_word in obj.full_name:
            nested.setdefault(obj.full_name.rsplit(sub_word, 1)[0], []).append(obj.name)

    top = []
    for obj in objects:
        kind_en, plural = kinds[obj.kind]
        if obj.kind == 'Подсистема' and sub_word in obj.full_name:
            parents = obj.full_name.split('.', 1)[1].split(sub_word)
            path = out / plural
            for parent in parents[:-1]:
                path = path / parent / 'Subsystems'
        else:
            path = out / plural
            top.append((kind_en, obj.name))
        write(path / f'{obj.name}.xml',
              _object_xml(obj, kind_en, lang, nested.get(obj.full_name, []), english))

    compat = config.compatibility_mode.replace('Версия', 'Version', 1)
    t = '\t\t\t'
    write(out / 'Configuration.xml',
          f'<?xml version="1.0" encoding="UTF-8"?>\n<MetaDataObject {_NS}>\n'
          f'\t<Configuration uuid="{_uid(config.name)}">\n\t\t<Properties>\n'
          f'{t}<Name>{config.name}</Name>\n' + _syn(config.synonym, lang, t) +
          f'{t}<Comment>{escape(config.comment)}</Comment>\n'
          f'{t}<ScriptVariant>{"English" if english else "Russian"}</ScriptVariant>\n'
          f'{t}<Version>{config.version}</Version>\n'
          f'{t}<CompatibilityMode>{compat}</CompatibilityMode>\n'
          '\t\t</Properties>\n\t\t<ChildObjects>\n'
          + ''.join(f'{t}<{k}>{n}</{k}>\n' for k, n in top) +
          '\t\t</ChildObjects>\n\t</Configuration>\n</MetaDataObject>\n')


def main():
    write_report(build_ru(), HERE / 'report_ru.txt')
    write_report(build_en(), HERE / 'report_en.txt')
    write_xml_dump(HERE / 'report_ru.txt', HERE / 'xml_meta_ru', english=False)
    write_xml_dump(HERE / 'report_en.txt', HERE / 'xml_meta_en', english=True)


if __name__ == '__main__':
    main()
