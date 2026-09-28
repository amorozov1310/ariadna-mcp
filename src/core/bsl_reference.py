"""
BSL reference data: platform constructors, system enumerations, built-in functions.
Generated from verified 1C 8.3 syntax reference.
Used by bsl_parser.py to filter noise from call detection.

Both Russian and English forms are supported. Every 1C platform identifier
has two canonical names — e.g. "НСтр" / "NStr", "Новый Массив" / "New Array".
Configurations built with ScriptVariant = English use the English names
in their BSL source, so the filter lists must include both variants.
"""

# Types created via "Новый" / "New" — NOT module calls
CONSTRUCTOR_TYPES_RU = {s.lower() for s in [
    "COMБезопасныйМассив", "COMОбъект", "HTTPЗапрос", "HTTPСоединение",
    "MMSВложение", "TCPСоединение", "UDPСоединение", "WSОпределения", "WSПрокси",
    "ZIPДиалогВыбораФайла", "ZipФайл",
    "АвтоВыбранноеПолеКомпоновкиДанных", "АнализДанных", "АутентификацияОС",
    "БлокировкаДанных", "БуферДвоичныхДанных", "БуферОбмена", "ВнешняяКомпонента",
    "Градиент", "Граница", "ГрафическаяСхема",
    "ДанныеРасшифровкиКомпоновкиДанных", "ДвоичныеДанные", "ДеревоЗначений",
    "Диаграмма", "ДиалогВыбораСертификата", "ДоставляемоеУведомление",
    "ЗаписьDOM", "ЗаписьFastInfoset", "ЗаписьHTML", "ЗаписьJSON", "ЗаписьXML",
    "ЗаписьZipФайла", "ЗаписьТекста", "Запрос",
    "ЗначениеПараметраНастроекКомпоновкиДанных",
    "ИнтервалДиаграммы", "ИнтернетПрокси", "ИнформацияОКаталоге", "ИнформацияОФайле",
    "ИсторияРаботыПользователя", "Картинка", "Каталог",
    "КвалификаторыДаты", "КвалификаторыСтроки", "КвалификаторыЧисла",
    "КомпоновщикМакетаКомпоновкиДанных", "КонвертацияXSL",
    "КонструкторЗапроса", "КонструкторНастроекКомпоновкиДанных",
    "КонтейнерКлючейКриптографии",
    "Макет", "МакетКомпоновкиДанных", "МакетПечати", "Массив",
    "МенеджерВременныхТаблиц", "МенеджерКриптографии",
    "МенеджерМобильногоПриложения", "МенеджерОбработкиОжидания", "МенеджерФоновыхЗаданий",
    "НаборСхемXML", "НастройкиКлиентскогоПриложения",
    "НастройкиКомпоновкиДанных", "НастройкиКриптографии",
    "ОписаниеЗащитыОтОпасныхДействий", "ОписаниеЗащитыСКД",
    "ОписаниеОповещения", "ОписаниеПоставки", "ОписаниеТипов",
    "ПамятьПоток", "ПараметрыЗаписиJSON", "ПараметрыЗаписиXML",
    "ПараметрыСистемы", "ПараметрыСканированияДокументов",
    "ПараметрыЧтенияJSON", "ПараметрыЧтенияXML",
    "Планировщик", "ПользовательскиеПоляКомпоновкиДанных",
    "ПоляНастройкиКомпоновкиДанных",
    "ПостроительDOM", "ПостроительДереваЗапроса", "ПостроительЗапроса",
    "Поток", "Почта", "ПроцессорКомпоновкиДанных",
    "РасширениеПотока", "РасширениеФайловойСистемы",
    "РезультатГлобальногоПоиска", "РезультатПоиска",
    "СекцияДиаграммы", "СерияДиаграммы", "СертификатКриптографии",
    "СистемнаяИнформация", "СканированиеДокументов",
    "СообщениеПользователю", "Соответствие", "СписокЗначений",
    "СтильГраницы", "Структура",
    "СхемаXML", "СхемаXMLКоллекцияИспользованныхПространствИмен",
    "СхемаКомпоновкиДанных",
    "ТаблицаЗначений", "ТабличныйДокумент", "ТекстовыйДокумент",
    "ТестируемаяГруппаФормы", "ТестируемаяДекорацияФормы",
    "ТестируемаяКнопкаФормы", "ТестируемаяТаблицаФормы",
    "ТестируемаяФорма", "ТестируемоеОкноКлиентскогоПриложения",
    "ТестируемоеПолеФормы", "ТестируемоеПриложение",
    "ТочкаДиаграммы", "УведомлениеПользователя", "УникальныйИдентификатор",
    "Файл", "ФайловыйПоток",
    "ФиксированнаяСтруктура", "ФиксированноеСоответствие", "ФиксированныйМассив",
    "ФорматированнаяСтрока", "ФорматированныйДокумент",
    "ХранилищеЗначения", "Цвет",
    "ЧтениеFastInfoset", "ЧтениеJSON", "ЧтениеXML", "ЧтениеZipФайла", "ЧтениеТекста",
    "Шрифт",
    "ЭлементБлокировкиДанных", "ЭлементРасшифровкиКомпоновкиДанных",
    "ЭлементРезультатаКомпоновкиДанных",
    # Additional constructors found in typical code
    "ПараметрВыбора", "СвязьПараметраВыбора", "ПараметрыДиалогаПомещенияФайлов",
    "СжатиеДанных",
    # Этап 7: found via diagnose_index's top-unresolved-callees on bshp —
    # see the matching note on CONSTRUCTOR_TYPES_EN.
    "РеквизитФормы", "ПолеКомпоновкиДанных", "ПараметрКомпоновкиДанных",
    # Этап 7 (третий заход): the entry here was word-order-reversed
    # ("ДоступныйИсточникНастроекКомпоновкиДанных") and never matched real
    # code — confirmed via grep on erp (Новый
    # ИсточникДоступныхНастроекКомпоновкиДанных(...), ~321 unresolved
    # bare-call edges) against the correct EN pairing
    # "DataCompositionAvailableSettingsSource" already in CONSTRUCTOR_TYPES_EN.
    "ИсточникДоступныхНастроекКомпоновкиДанных",
    # Этап 7 (второй заход, реальная ERP): графическая линия ячейки
    # табличного документа — "Новый Линия(ТипЛинии..., Ширина)".
    "Линия",
]}

# Same constructor types — English equivalents (ScriptVariant = English BSL code)
CONSTRUCTOR_TYPES_EN = {s.lower() for s in [
    "COMSafeArray", "COMObject", "HTTPRequest", "HTTPConnection",
    "MMSAttachment", "TCPConnection", "UDPConnection", "WSDefinitions", "WSProxy",
    "ZIPFileDialog", "ZipFile",
    "AutoSelectedDataCompositionField", "DataAnalysis", "OSAuthentication",
    "DataLock", "BinaryDataBuffer", "Clipboard", "AddIn",
    "Gradient", "Border", "GraphicalSchema",
    "DataCompositionDetailsData", "BinaryData", "ValueTree",
    "Chart", "CertificateDialog", "DeliverableNotification",
    "DOMWriter", "FastInfosetWriter", "HTMLWriter", "JSONWriter", "XMLWriter",
    "ZipFileWriter", "TextWriter", "Query",
    "DataCompositionSettingsParameterValue",
    "ChartInterval", "InternetProxy", "DirectoryInfo", "FileInfo",
    "UserWorkHistory", "Picture", "Directory",
    "DateQualifiers", "StringQualifiers", "NumberQualifiers",
    "DataCompositionTemplateComposer", "XSLTransform",
    "QueryBuilder", "DataCompositionSettingsComposer",
    "CryptoKeyContainer",
    "Template", "DataCompositionTemplate", "PrintingTemplate", "Array",
    "TempTablesManager", "CryptoManager",
    "MobileApplicationManager", "IdleHandlersManager", "BackgroundJobsManager",
    "XMLSchemaSet", "ClientApplicationSettings",
    "DataCompositionSettings", "CryptoSettings",
    "UnsafeOperationProtectionDescription", "SafeModeDescription",
    "NotifyDescription", "DeliveryDescription", "TypeDescription",
    "MemoryStream", "JSONWriterSettings", "XMLWriterSettings",
    "SystemSettings", "DocumentScanningSettings",
    "JSONReaderSettings", "XMLReaderSettings",
    "Scheduler", "DataCompositionUserFields",
    "DataCompositionSettingsFields",
    "DOMBuilder", "QueryTreeBuilder", "QueryBuilder",
    "Stream", "Mail", "DataCompositionProcessor",
    "StreamExtension", "FileSystemExtension",
    "GlobalSearchResult", "SearchResult",
    "ChartSection", "ChartSeries", "CryptoCertificate",
    "SystemInfo", "DocumentScanning",
    "UserMessage", "Map", "ValueList",
    "BorderStyle", "Structure",
    "XMLSchema", "XMLSchemaUsedNamespaceCollection",
    "DataCompositionSchema",
    "ValueTable", "SpreadsheetDocument", "TextDocument",
    "TestedFormGroup", "TestedFormDecoration",
    "TestedFormButton", "TestedFormTable",
    "TestedForm", "TestedClientApplicationWindow",
    "TestedFormField", "TestedApplication",
    "ChartPoint", "UserNotification", "UUID",
    "File", "FileStream",
    "FixedStructure", "FixedMap", "FixedArray",
    "FormattedString", "FormattedDocument",
    "ValueStorage", "Color",
    "FastInfosetReader", "JSONReader", "XMLReader", "ZipFileReader", "TextReader",
    "Font",
    "DataLockItem", "DataCompositionDetailsItem",
    "DataCompositionResultItem",
    "ChoiceParameter", "ChoiceParameterLink", "FilePutDialogParameters",
    "DataCompression",
    # Этап 7: found via diagnose_index's top-unresolved-callees on 1idm2 —
    # "New X(...)" is never a resolvable local call (New only constructs
    # platform/registered types, it can't invoke BSL code), so any type
    # name missing from this list shows up as a bogus unresolved call
    # rather than being silently classified as a constructor. This list
    # can never be fully exhaustive; grep the corpus for "New <Name>(" to
    # confirm before adding more.
    "CallbackDescription", "Deflation", "FormAttribute",
    "DataCompositionField", "DataCompositionParameter",
    "DataCompositionAvailableSettingsSource",
    # Этап 7 (second round, real ERP): spreadsheet-cell line — "New
    # Line(LineType..., Width)".
    "Line",
]}

# System enumerations: ИмяПеречисления.Значение — NOT module calls
SYSTEM_ENUMERATIONS_RU = {s.lower() for s in [
    "ВидДвиженияНакопления", "ВидДвиженияБухгалтерии",
    "ВидДополненияЭлементаСортировки", "ВидИерархии",
    "ВидОткрытияСписка", "ВидПоляФормы", "ВидСравнения",
    "ВидФормыДокумента", "ВидЦвета", "ВидЭлементаФормыКлиентаУправляемоеПриложение",
    "ВыравниваниеЭлементовУправления",
    "ИспользованиеГруппыИЭлементов", "ИспользованиеПодчинения",
    "КодировкаТекста",
    "ОтображениеКнопкиОчистки",
    "ПозицияВПотоке",
    "РежимВключенияСертификатовКриптографии", "РежимЗаписиXML",
    "РежимОткрытияФайла", "РежимПробеловДаты",
    "РежимРазделенияЗначенияДаты", "РежимСокращенияДаты", "РежимЧтенияXML",
    "ТипАтрибутаXML", "ТипДиаграммы", "ТипЗначенияJSON",
    "ТипКартинки", "ТипКлиентскогоПриложения", "ТипМаркераДиаграммы",
    "ТипНомераТаблицы", "ТипОбходаДереваЗначений", "ТипПодписиКриптографии",
    "ТипПоказаПлатформенногоДиалога", "ТипПоляСписка",
    "ТипПроверкиXML", "ТипУзлаXML", "ТипХранилищаДвоичныхДанных",
    "УровеньЖурналаРегистрации", "ЧастиДаты",
    # Common in forms
    "ВидГруппыФормы", "ВидПоляФормы", "ВидДекорацииФормы",
    "ВидОтображенияПолногоИмени", "ВидОтображенияФормы",
    "ПоложениеКоманднойПанели", "ПоложениеЗаголовкаЭлементаФормы",
    "ОтображениеПодсказки", "ОтображениеОбычнойГруппы",
    "РежимСовместимостиИнтерфейса",
    "РежимДиалогаВопрос", "КодВозвратаДиалога",
    "РежимОткрытияОкнаФормы", "ВариантВажностиКлиентскогоСообщения",
    "НаправлениеСортировки",
    # Accumulation/accounting
    "ВидДвиженияБухгалтерии", "ВидДвиженияНакопления",
    "ВидСчета", "ВидПериодаРегистраСведений",
]}

# Same system enumerations — English equivalents
SYSTEM_ENUMERATIONS_EN = {s.lower() for s in [
    "AccumulationRecordType", "AccountingRecordType",
    "OrderItemAdditionType", "HierarchyType",
    "ListOpenKind", "FormFieldKind", "ComparisonType",
    "DocumentFormKind", "ColorKind", "ManagedFormElementKind",
    "ControlAlignment",
    "GroupAndItemsUsage", "SubordinationUse",
    "TextEncoding",
    "ClearButtonRepresentation",
    "StreamPosition",
    "CryptoCertificateIncludeMode", "XMLWriteMode",
    "FileOpenMode", "DateSpacesMode",
    "DateValueSplittingMode", "DateFractionMode", "XMLReadMode",
    "XMLAttributeType", "ChartType", "JSONValueType",
    "PictureType", "ClientApplicationType", "ChartMarkerType",
    "TableNumberType", "ValueTreeWalkingType", "CryptoSignatureType",
    "PlatformDialogShowType", "ListFieldType",
    "XMLValidationType", "XMLNodeType", "BinaryDataStorageType",
    "EventLogLevel", "DateFractions",
    # Common in forms
    "FormGroupType", "FormFieldType", "FormDecorationType",
    "FullNameDisplayType", "FormDisplayType",
    "CommandBarLocation", "FormElementTitleLocation",
    "TooltipRepresentation", "NormalGroupRepresentation",
    "InterfaceCompatibilityMode",
    "QuestionDialogMode", "DialogReturnCode",
    "FormWindowOpeningMode", "ClientMessageImportanceVariant",
    "SortDirection",
    "AccountingRecordType", "AccumulationRecordType",
    "AccountType", "InformationRegisterPeriodType",
]}

# Additional platform global functions missing from the main stoplist
PLATFORM_FUNCTIONS_EXTRA = {s.lower() for s in [
    # --- Russian names ---
    # Async methods (НачатьXxx)
    'начатьзапускприложения', 'начатьподключениерасширенияработысфайлами',
    'начатьполучениефайлассервера', 'начатьпомещениефайланасервер',
    'начатьустановкурасширенияработысфайлами',
    'начатьвыборфайла', 'начатьполучениекаталогавременныхфайлов',
    'начатьполучениекаталогадокументов',
    # Dialog/UI
    'показатьвводстроки', 'показатьвводчисла', 'показатьвводдаты',
    'показатьвопрос', 'показатьпредупреждение', 'показатьвводзначения',
    # Implicit context methods (form/manager module calling its own,
    # unqualified — Этап 7: found via diagnose_index's top-unresolved on
    # 1idm2, e.g. bare Close()/GetTemplate() inside a form/manager module)
    'закрыть', 'получитьмакет',
    # Temp storage
    'получитьизвременногохранилища', 'поместитьвовременноехранилище',
    'этоадресвременногохранилища', 'удалитьизвременногохранилища',
    'получитьнавигационнуюссылку',
    # Session/system
    'установитьпривилегированныйрежим', 'привилегированныйрежим',
    'безопасныйрежим', 'установитьбезопасныйрежим',
    'завершитьработусистемы', 'завершитьработу',
    'перейтиповигационнойссылке', 'перейтипонавигационнойссылке',
    'очиститьсообщения',
    'значениевреквизитформы', 'реквизитформывзначение',
    # Error handling
    'вызватьисключение', 'информацияобошибке',
    'краткоепредставлениеошибки', 'подробноепредставлениеошибки',
    'описаниеошибки', 'тексошибки', 'текстошибки',
    # Transaction
    'начатьтранзакцию', 'зафиксироватьтранзакцию', 'отменитьтранзакцию',
    # User / access
    'пользователиинформационнойбазы', 'ролидоступны', 'текущийпользователь',
    'параметрысеанса', 'текущийязык',
    # Strings
    'стрчислострок', 'стрчислостроки',
    'установитьзаголовокприложения',
    # Dates (additional)
    'текущаяуниверсальнаядата', 'текущаяуниверсальнаядатавмиллисекундах',
    'универсальноевремя', 'местноевремя', 'смещениестандартноговремени',
    # Event log
    'записьжурналарегистрации', 'журналрегистрации',
    # Metadata access
    'метаданные', 'типпланавидоврасчета',
    # Global platform singleton objects used with dot notation (Этап 7,
    # second round, real ERP) — "ФабрикаXDTO.Создать(...)" etc.
    'фабрикаxdto',
    # Этап 7 (третий заход): "ПланыОбмена.ГлавныйУзел()" — the global
    # ExchangePlans manager collection itself (no specific plan name), a
    # 2-part platform call distinct from the 3-part
    # "ПланыОбмена.ИмяПлана.Метод(" manager-instance pattern (already
    # handled separately via _METADATA_MANAGERS/_RE_MANAGER_CALL).
    'планыобмена',
    # Background jobs
    'фоновыезадания', 'запуститьобработчикожидания', 'отключитьобработчикожидания',
    # File system
    'создатькаталог', 'удалитьфайлы', 'копироватьфайл',
    'переместитьфайл', 'переименоватьфайл',
    'найтифайлы', 'каталогвременныхфайлов', 'каталогпрограммы',
    'получитьимявременногофайла',
    # Full-text search
    'обновитьиндексполнотекстовогопоиска', 'полнотекстовыйпоискобъектов',
    # Settings
    'хранилищесистемныхнастроек', 'хранилищеобщихнастроек',
    'хранилищепользовательскихнастроекдинамическихсписков',
    'хранилищепользовательскихнастроекотчетов',
    # Misc
    'кэшируемыезначения', 'проверитьзаполнение', 'проверитьсистемныеправа',
    'обходрезультатазапроса',
    # Этап 7: found via diagnose_index's top-unresolved-callees on bshp —
    # the highest-frequency bare-call misses (ВыполнитьОбработкуОповещения
    # alone was ~14k unresolved edges across the corpus).
    'выполнитьобработкуоповещения', 'получитьфункциональнуюопцию',
    'праводоступа', 'представлениепериода',
    'заблокироватьданныедляредактирования', 'разблокироватьданныедляредактирования',
    'показатьвыборизсписка', 'показатьзначение', 'показатьоповещениепользователя',
    # Этап 7 (третий заход, реальная ERP): reserved context identifiers —
    # ЭтаФорма/ЭтотОбъект always refer to the current form/object, never a
    # module, so a dotted call on them (ЭтаФорма.Активизировать(...)) is
    # exactly the same "not a module" case as a system enumeration above.
    'этаформа', 'этотобъект',
    # More bare-call stoplist gaps found the same way (diagnose_index on
    # erp): genuine platform globals/form built-ins missing from the list.
    'base64значение', 'base64строка', 'стрсравнить',
    'обновитьповторноиспользуемыезначения', 'установитьотключениебезопасногорежима',
    'получитьразделительпути', 'получитьсообщенияпользователю', 'прочитатьjson',
    'получитьобщиймакет', 'получитьфункциональнуюопциюформы',
    'установитьпараметрыфункциональныхопцийформы', 'создатьнаборзаписей',
    'оповеститьовыборе', 'открыта', 'вводдоступен', 'этоновый',

    # --- English names ---
    # Async
    'beginrunningapplication', 'beginattachingfilesystemextension',
    'begingettingfilefromserver', 'beginputtingfiletoserver',
    'begininstallfilesystemextension',
    'beginputtingfiles', 'beginputtingfile',
    'beginchoosefile', 'begingettingtempfilesdir',
    'begingettingdocumentsdir',
    # Dialog/UI
    'showinputstring', 'showinputnumber', 'showinputdate',
    'showquestion', 'showmessagebox', 'showinputvalue',
    'showuserinotification',
    # Implicit context methods (form/manager module calling its own,
    # unqualified — see the RU list above for why)
    'close', 'gettemplate',
    # Temp storage
    'getfromtempstorage', 'puttotempstorage',
    'istempstorageurl', 'deletefromtempstorage',
    'getnavigationurl',
    # Session/system
    'setprivilegedmode', 'privilegedmode',
    'safemode', 'setsafemode',
    'exitsystem', 'exit',
    'gotourl', 'gotonavigationurl',
    'clearmessages',
    'valuetoformattribute', 'formattributetovalue',
    # Error handling
    'raise', 'errorinfo',
    'brieferrordescription', 'detailederrordescription',
    'errordescription', 'errortext',
    # Transaction
    'begintransaction', 'committransaction', 'rollbacktransaction',
    'transactionactive',
    # User / access
    'infobaseusers', 'rolesavailable', 'currentuser', 'isinrole',
    'sessionparameters', 'currentlanguage',
    # Strings
    'strlinecount',
    'setapplicationcaption',
    # Dates (additional)
    'currentuniversaldate', 'currentuniversaldateinmilliseconds',
    'universaltime', 'localtime', 'standardtimeoffset',
    # Event log
    'writelogevent', 'eventlog',
    # Metadata access
    'metadata', 'calculationtypeschart',
    'xdtofactory',
    'exchangeplans',
    # Background jobs
    'backgroundjobs', 'attachidlehandler', 'detachidlehandler',
    # File system
    'createdirectory', 'deletefiles', 'filecopy',
    'movefile', 'renamefile', 'findfiles',
    'tempfilesdir', 'applicationdir', 'gettempfilename',
    # Full-text search
    'updatefulltextsearchindex', 'fulltextsearchobjects', 'fulltextsearch',
    # Settings
    'systemsettingsstorage', 'commonsettingsstorage',
    'dynamiclistsuserssettingsstorage', 'reportsuserssettingsstorage',
    'reportsvariantsstorage', 'formdatasettingsstorage',
    # Misc
    'cachedvalues', 'checkfilling', 'checksystemrights',
    'queryresultiteration',
    # Этап 7: EN equivalents of the RU platform globals found via
    # diagnose_index on bshp (see the RU list above) — not confirmed in
    # an EN corpus yet, but these are the documented EN platform names.
    'executenotifyprocessing', 'getfunctionaloption',
    'accessright', 'periodpresentation',
    'lockdataforedit', 'unlockdataforedit',
    'showchoosefromlist', 'showvalue', 'showusernotification',
    # Этап 7 (третий заход): EN equivalents of the reserved context
    # identifiers and bare-call stoplist gaps added to the RU list above.
    'thisform', 'thisobject',
    'base64value', 'base64string', 'strcompare',
    'refreshreusablevalues', 'setsafemodedisabled',
    'getpathseparator', 'getusermessages', 'readjson',
    'getcommontemplate', 'getformfunctionaloption',
    'setformfunctionaloptionsparameters', 'createrecordset',
    'notifychoice', 'isopen', 'inputavailable', 'isnew',

    # --- Specific procedures commonly seen in 1C code (both scripts) ---
    # NStr / str template
    'nstr', 'нстр', 'строкатемплейт', 'strtemplate',
    # Number/type helpers
    'isblankstring', 'пустаястрока', 'valueisfilled', 'значениезаполнено',
    'getmetadataobject', 'получитьобъектметаданных',
    'getmetadataobjectid', 'получитьидентификаторобъектаметаданных',
    # Locking
    'lock', 'unlock', 'заблокировать', 'разблокировать',
    # Misc globals that look like user methods
    'user', 'пользователь',
    # Extension override call-through — meaningful only inside a &Вместо/
    # &Around interceptor, where bsl_parser redirects it to the intercepted
    # procedure instead (see ProcedureInfo.intercepts). Listed here purely
    # as a safety net so a stray occurrence elsewhere doesn't produce a
    # dangling unresolvable call node.
    'продолжитьвызов', 'proceedwithcall',
    # Infobase / arrays / misc globals that also look like method calls
    'infobaseconnectionstring', 'строкасоединенияинформационнойбазы',
    'ubound', 'вграницах',
    'trimall', 'trim', 'сокрлп',
    'char', 'chars', 'символ', 'chr',
    'сравнитьстроки', 'comparestrings',
    'representationtext', 'текстпредставления',
    # Common global helpers
    'beep', 'сигнал',
    'importieren', 'import',   # rarely in 1C but guard
    # NOTE: 'Common' / 'ОбщегоНазначения' removed — they're real BSP
    # common modules, not platform globals. Filtering them would hide
    # a huge chunk of real cross-module calls.
]}



# Идентификаторы, которые в `Имя.Метод(` обозначают встроенный объект
# платформы, а не общий модуль. В отличие от PLATFORM_FUNCTIONS_EXTRA, в
# стоп-лист голых вызовов не входят (`Элементы(` не бывает), а в парсере
# проверяются только если имени нет в known_modules: настоящий общий модуль
# с таким именем (если конфигурация его всё же завела) важнее списка.
#
# Свойства управляемой формы, доступные в её модуле без «ЭтаФорма.».
# В модулях других видов эти имена — обычные локальные переменные, тоже не
# модули. Окно/Window сознательно не включено — не подтверждено.
FORM_CONTEXT_IDENTIFIERS_RU = {s.lower() for s in [
    'Элементы', 'Параметры', 'ВладелецФормы', 'Команды',
    'КоманднаяПанель', 'КомандныйИнтерфейс',
]}
FORM_CONTEXT_IDENTIFIERS_EN = {s.lower() for s in [
    'Items', 'Parameters', 'FormOwner', 'Commands',
    'CommandBar', 'CommandInterface',
]}

# Объекты глобального контекста, у которых вызывают методы через точку.
# ОбработкаОшибок/ErrorProcessing — с платформы 8.3.17 (найдено в фикстуре
# xml_en: ErrorProcessing.DetailErrorDescription писалось вызовом модуля).
GLOBAL_CONTEXT_OBJECTS_RU = {s.lower() for s in [
    'ОбработкаОшибок',
]}
GLOBAL_CONTEXT_OBJECTS_EN = {s.lower() for s in [
    'ErrorProcessing',
]}


# Неявные свойства объекта, доступные в его СОБСТВЕННОМ модуле без
# «ЭтотОбъект.» — источник факта здесь тип модуля, а не метаданные.
# Ключ — (object_kind, module_type) из xml_walker.ModuleFile.
#
# ОтчетОбъект.КомпоновщикНастроек (КомпоновщикНастроекКомпоновкиДанных) —
# платформенное свойство любого отчёта, в модуле объекта отчёта пишется
# просто `КомпоновщикНастроек.ПолучитьНастройки()`.
# Обработка сюда не входит: у ОбработкаОбъект нет платформенных свойств,
# кроме её реквизитов/табличных частей из метаданных.
MODULE_CONTEXT_VARS = {
    ('Отчет', 'МодульОбъекта'): {s.lower() for s in [
        'КомпоновщикНастроек', 'SettingsComposer',
    ]},
}


def module_context_vars(object_kind: str, module_type: str) -> set[str]:
    return MODULE_CONTEXT_VARS.get((object_kind, module_type), set())
