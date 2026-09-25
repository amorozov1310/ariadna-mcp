
#Region Public

#Region BackgroundAndScheduleJobs

// Регламентное задание - "Запустить задачи по времени(Run)"
// Выполняется запуск следующих сервисных процедур:
//		- запуск универсальных задач(Generic).
//		- проверка по таймауту пользовательских задач(UserTask).
// 		- запуск проверки готовности задач агента(CheckReadiness).
//		- запуск формирования и отправки уведомлений(Catalogs.Notifier.PeriodicRun).
//		- запуск обновления активности орг.единиц по указанному периоду действия(Catalogs.OrgUnit.UpdateOrgUnitsActivityByDate).
//		- запуск обновления текущего статуса назначений по наступлению даты будущего статуса(Catalogs.Appointment.UpdateAppointmentsStatusesByDate).
//		- запуск процессов по расписанию(Procs.RunOnSchedule).
//		- запуск на исполение задач активных агентов в ядре. 
//		  Вызов Tasks.AgentTask.GetNextTask(Если подключен фреймворк расширение agent_framework_1c).
//		  В данном случае используется для синхронизации и управления самим ядром 1IDM2. Модуль реализации agent_impl_Self.
//		- запуск обработки ожидающих изменений узлов (PendingNodeStatus). 
//      - запуск экспорта метрик данных в OTel коллектор
//
// Процедуры не должны генерировать исключения.
//
Procedure PeriodicTasksRun() Export	
	
	StartTime = CurrentUniversalDateInMilliseconds();
	
	Tasks.Generic.RunWaitingTasks();
	Tasks.UserTask.UserTaskCheckTimeout();
	Tasks.AgentTask.PeriodicJob();
	Catalogs.Notifier.PeriodicRun();
	Catalogs.OrgUnit.UpdateOrgUnitsActivityByDate();
	Catalogs.Appointment.UpdateAppointmentsStatusesByDate();
	Procs.RunOnSchedule();
	Catalogs.ProcessQueue.RunProccessQueue();
	RunSelfAgent();
	Catalogs.PendingNodeStatus.Process();
	History.UpdateInBackground();
	Otel.BackgroundExportDataToOtel();
	
	Attributes = New Map;
	Attributes.Insert("job.name", "Run");
	
	OTel.RecordGaugeMetric("CommonAtServer", "job.duration", "Background job execution duration", "s", ,
		(CurrentUniversalDateInMilliseconds() - StartTime) / 1000, Attributes);
			
EndProcedure

// Регламентное задание - "Обновление индекса полнотекстового поиска"
Procedure PeriodicUpdateFullTextSearchIndex() Export	
	UpdateFullTextSearchIndex(NStr("ru = 'Обновление индекса полнотекстового поиска'; en = 'Updating the full-text search index'"), False, True);
EndProcedure

// Регламентное задание - "Слияние индекса полнотекстового поиска"
Procedure PeriodicMergeFullTextSearchIndex() Export
	UpdateFullTextSearchIndex(NStr("ru = 'Слияние индекса полнотекстового поиска'; en = 'Full-text search index merge'"), True);
EndProcedure

// Выполняет фоновый запуск указанного метода с параметрами.
//
// Параметры:
//  MethodName    - Строка - имя экспортной процедуры в формате
//                       <имя объекта>.<имя процедуры>, где <имя объекта> - это
//                       общий модуль или модуль менеджера объекта.
//  MethodParams  - Массив - параметры передаются в процедуру <MethodName>
//                        в порядке расположения элементов массива.
//  KeyBackgroundJob     - Строка - Ключ фонового задания, может быть пустым.
//  NameOfBackgroundJob  - Строка - Наименование фонового задания, может быть пустым.
//  NoExtensions         - Булево - выполнять в фоне без загрузки расширений конфигурации, по умолчанию False.
//
Function RunBackgroundJob(MethodName, MethodParams = Undefined, KeyBackgroundJob = "", NameOfBackgroundJob = "", NoExtensions = False) Export
	
	arrBackgroundJobs = BackgroundJobs.GetBackgroundJobs(New Structure("Key, State", KeyBackgroundJob, BackgroundJobState.Active));
	If arrBackgroundJobs.Count() Then
		KeyBackgroundJob = KeyBackgroundJob + String(New UUID);
	EndIf;

	BackgroundJobParams = New Array;
	BackgroundJobParams.Add(MethodName);
	BackgroundJobParams.Add(MethodParams);
	                     
	Return ExecuteBackgroundJob("CommonAtServer.ExecuteConfigurationMethod", BackgroundJobParams, 
									KeyBackgroundJob, NameOfBackgroundJob, NoExtensions);
	
EndFunction

// Выполняет экспортную процедуру по имени
//
// Параметры:
//  MethodName    - Строка - имя экспортной процедуры в формате
//                       <имя объекта>.<имя процедуры>, где <имя объекта> - это
//                       общий модуль или модуль менеджера объекта.
//  MethodParams  - Массив - параметры передаются в процедуру <ИмяЭкспортнойПроцедуры>
//                        в порядке расположения элементов массива.
// 
// Пример:
//  params = New Array();
//  params.Add("1");
//  CommonAtServer.ExecuteConfigurationMethod("МойОбщийМодуль.МояПроцедура", params);
//
Procedure ExecuteConfigurationMethod(Val MethodName, Val MethodParams = Undefined) Export
		
	MethodParamsString = "";
	If MethodParams <> Undefined И MethodParams.Count() > 0 Then
		For i = 0 To MethodParams.UBound() Do
			MethodParamsString = MethodParamsString + "MethodParams[" + XMLString(i) + "],";			
		EndDo;
		MethodParamsString = Mid(MethodParamsString, 1, StrLen(MethodParamsString) - 1);
	EndIf;
	
	Execute MethodName + "(" + MethodParamsString + ")";
	
EndProcedure

#EndRegion

#Region DataCompositionSchema

// Устанавливает параметр в пользовательских настройках СКД
//
// Параметры:
//  SettingsComposer  - DataCompositionSettingsComposer - компоновщик настроек компоновки данных.
//  ParamName  - Строка - имя параметра.
//  Value      - ЛюбойТип - значение параметра.
//
Procedure SetCustomParameterDataCompositionSchema(SettingsComposer, ParamName, Value) Export	

	Var DataParameter, Settings, UserSettings, UserParameter;
	
	Settings = SettingsComposer.Settings;
	
	DataParameter = Settings.DataParameters.Items.Find(ParamName);
	
	If ValueIsFilled(DataParameter.UserSettingID) Then
		
		UserSettings = SettingsComposer.UserSettings;
		UserParameter = UserSettings.Items.Find(DataParameter.UserSettingID);
		UserParameter.Use = True;
		UserParameter.Value = Value;

	EndIf;
	
EndProcedure

#EndRegion

#Region ЕventSubscriptionProcedure

// Процедура подписки на событие BeforeDelete(ПередУдалением) для справочников OrgUnit, Role, Appointment, Person, Account, Resource, ResourceAccessType.
// При удалении объекта справочника удаляется Mapping(Сопоставление) для данного объекта.
// При удаления объекта Person(Сотрудник) так же удаляются всего его Appointment(Назначения).
//
// Параметры:
//  Source - CatalogObject(OrgUnit, Role, Appointment, Person, Account, Resource, ResourceAccessType) - удаляемый объект справочника.
//  Cancel - Булево - флаг отмены удаления.
//
Procedure OnDeleteMainCatalogsBeforeDelete(Source, Cancel) Export
	
	Query = New Query("SELECT TOP 1 Ref AS Ref FROM Catalog.Mapping WHERE Object = &Ref");
	Query.SetParameter("Ref", Source.Ref);
	
	Sel = Query.Execute().Select();
	If Sel.Next() Then
		Sel.Ref.GetObject().Delete();
	EndIf;
	
	If TypeOf(Source) = Type("CatalogObject.Person") Then
		Query = New Query("SELECT
		                  |	Appointment.Ref AS Ref
		                  |FROM
		                  |	Catalog.Appointment AS Appointment
		                  |WHERE
		                  |	Appointment.Person = &Ref
		                  |
		                  |UNION ALL
		                  |
		                  |SELECT
		                  |	File.Ref
		                  |FROM
		                  |	Catalog.File AS File
		                  |WHERE
		                  |	File.OwnerFile = &Ref");
		Query.SetParameter("Ref", Source.Ref);
		Sel = Query.Execute().Select();
		While Sel.Next() Do
			Sel.Ref.GetObject().Delete();
		EndDo;
	EndIf;   
	
EndProcedure

// Процедура подписки на событие BeforeDelete(ПередУдалением) для справочника Node(Узел)
// При удалении Node(Узла) удаляется NodeLink(Связь узлов).
//
// Параметры:
//  Source - CatalogObject.Node - удаляемый Node(Узел).
//  Cancel - Булево - флаг отмены удаления.
//
Procedure OnDeleteNodeBeforeDelete(Source, Cancel) Export

	Query = New Query("SELECT L.Ref AS NodeLink 
	|FROM Catalog.NodeLink AS L 
	|WHERE L.Source = &Node OR L.Destination = &Node");
	
	Query.SetParameter("Node", Source.Ref);
	Sel = Query.Execute().Select();
	
	While Sel.Next() Do
		Sel.NodeLink.GetObject().Delete();
	EndDo; 
	
EndProcedure

// Процедура подписки на событие BeforeDelete(ПередУдалением) для справочников AppHR, AppMS.
// При удалении объекта справочника удаляется InfoBaseUser(сервисный пользователь агента). 
//
// Параметры:
//  Source - CatalogObject(AppHR, AppMS) - удаляемый объект справочника.
//  Cancel - Булево - флаг отмены удаления.   
//
Procedure OnDeleteAppBeforeDelete(Source, Cancel) Export
	
	AgentUser = Auth.GetAgentUser(Source.Ref);
	
	If AgentUser <> Undefined Then 
		AgentUser.Delete();
	EndIf;
	
EndProcedure  

#EndRegion

#Region Extensions

// Проверка активности расширения по имени.
// Параметры:
//  ExtensionName  - Строка - имя расширения.
//
// Возвращаемое значение:
//   Булево - активно или нет расширение.
//
Function IsExtensionActive(ExtensionName) Export
	
	SetPrivilegedMode(True);
	Exts = ConfigurationExtensions.Get(New Structure("Name", ExtensionName));    
	SetPrivilegedMode(False);
	
	Return ?(Exts.Count() = 1, Exts[0].Active, False); 
	
EndFunction

#EndRegion

#Region WorkingWithData

// Возвращает структуру, содержащую значения реквизитов, прочитанные из информационной базы по ссылке на объект.
// Рекомендуется использовать вместо обращения к реквизитам объекта через точку от ссылки на объект
// для быстрого чтения отдельных реквизитов объекта из базы данных.
//
// Если необходимо зачитать реквизит независимо от прав текущего пользователя,
// то следует использовать предварительный переход в привилегированный режим.
//
// Параметры:
//  Ref       - ЛюбаяСсылка - объект, значения реквизитов которого необходимо получить.
//  Attrs     - Строка - имена реквизитов, перечисленные через запятую, в формате
//                       требований к свойствам структуры.
//                       Например, "Код, Наименование, Родитель".
//            - Структура
//            - ФиксированнаяСтруктура - в качестве ключа передается
//                       псевдоним поля для возвращаемой структуры с результатом, а в качестве
//                       значения (опционально) фактическое имя поля в таблице.
//                       Если ключ задан, а значение не определено, то имя поля берется из ключа.
//                       Допускается указание имени поля через точку.
//            - Массив из Строка
//            - ФиксированныйМассив из Строка - имена реквизитов в формате требований к свойствам структуры.
//  SelectAllowed - Булево - если Истина, то запрос к объекту выполняется с учетом прав пользователя;
//                                если есть ограничение на уровне записей, то все реквизиты вернутся со 
//                                значением Неопределено; если нет прав для работы с таблицей, то возникнет исключение;
//                                если Ложь, то возникнет исключение при отсутствии прав на таблицу 
//                                или любой из реквизитов.
//
// Возвращаемое значение:
//  Структура - содержит имена (ключи) и значения затребованных реквизитов.
//              Если в параметр Реквизиты передана пустая строка, то возвращается пустая структура.
//              Если в параметр Реквизиты передано имя табличной части, то возвращается результат запроса.
//              Если в параметр Ссылка передана пустая ссылка, то возвращается структура, 
//              соответствующая именам реквизитов со значениями Неопределено.
//              Если в параметр Ссылка передана ссылка несуществующего объекта (битая ссылка), 
//              то все реквизиты вернутся со значением Неопределено.
//
Function ObjectAttributeValues(Ref, Val Attrs, SelectAllowed = False) Export
	
	If TypeOf(Attrs) = Type("String") Then
		If IsBlankString(Attrs) Then
			Return New Structure;
		EndIf;;
		Attrs = StrSplit(Attrs, ",", False);
	EndIf;
	
	stAttrs = New Structure;
	If TypeOf(Attrs) = Type("Structure") Or TypeOf(Attrs) = Type("FixedStructure") Then
		stAttrs = Attrs;
	ElsIf TypeOf(Attrs) = Type("Array") Or TypeOf(Attrs) = Type("FixedArray") Then
		For Each Attr Из Attrs Do
			stAttrs.Insert(StrReplace(Attr, ".", ""), Attr);
		EndDo;
	Else
		Raise StrTemplate(NStr("en = 'Wrong type of the second parameter Attrs: %1';ru = 'Неверный тип второго параметра Реквизиты: %1'"), String(TypeOf(Attrs)));
	EndIf;
	
	FieldText = "";
	For Each KeyAndValue In stAttrs Do
		FieldName = ?(ValueIsFilled(KeyAndValue.Value),
		TrimAll(KeyAndValue.Value),
		TrimAll(KeyAndValue.Key));
		
		Alias = TrimAll(KeyAndValue.Key);
		
		FieldText  = FieldText + ?(IsBlankString(FieldText), "", ",") + "
		|	" + FieldName + " AS " + Alias;
	EndDo;
	
	Query = New Query;
	Query.SetParameter("Ref", Ref);
	Query.Text =
	"SELECT " + ?(SelectAllowed, "ALLOWED", "") + "
	|" + FieldText + "
	|FROM
	|	" + Ref.Metadata().FullName() + " AS AliasSpecifiedTable
	|WHERE
	|	AliasSpecifiedTable.Ref = &Ref
	|";
	Selection = Query.Execute().Select();
	
	Result = New Structure;
	For Each KeyAndValue In stAttrs Do
		Result.Insert(KeyAndValue.Key);
	EndDo;
	
	If Selection.Next() Then
		FillPropertyValues(Result, Selection);
	EndIf;
	
	Возврат Result;
	
EndFunction

// Проверяет, является ли ссылка пустой или указывает на физически отсутствующий в ИБ объект.
// Использует признак DataVersion: для пустой и несуществующей ссылки метод
// ObjectAttributeValues возвращает Неопределено в качестве значения DataVersion.
// Пометка удаления не равна отсутствию объекта — объект с пометкой считается существующим.
//
// Параметры:
//  Ref - ЛюбаяСсылка - проверяемая ссылка.
//
// Возвращаемое значение:
//  Булево - Истина, если ссылка пустая или объект физически отсутствует в ИБ; Ложь — объект существует.
//
Function ObjectNotFound(Ref) Export

	If Ref = Undefined Then
		Return True;
	EndIf;

	If Not ValueIsFilled(Ref) Then
		Return True;
	EndIf;

	RefData = ObjectAttributeValues(Ref, "DataVersion");
	Return Not ValueIsFilled(RefData.DataVersion);

EndFunction

#EndRegion

#Region WorkingWithInfobase

// Получить представление физического места размещения информационной базы для отображения администратору.
//
// Возвращаемое значение:
//   Строка - представление информационной базы.
//
// Пример возвращаемого результата:
// - для ИБ в файлом режиме: \\FileServer\1c_ib\
// - для ИБ в серверном режиме: ServerName:1111 / information_base_name.
//
Function PresentationOfTheInformationBase() Export
	
	ConnectionStringDB = InfoBaseConnectionString();
	
	SearchPosition = StrFind(Upper(ConnectionStringDB), "FILE=");
	If SearchPosition = 1 Then
		Return Mid(ConnectionStringDB, 6, StrLen(ConnectionStringDB) - 6);
	EndIf;
	
	SearchPosition = StrFind(Upper(ConnectionStringDB), "SRVR=");
	If SearchPosition <> 1 Then
		Return Undefined;
	EndIf;
	
	PositionSemicolons = StrFind(ConnectionStringDB, ";");
	InitialPositionCopy = 6 + 1;
	EndPositionCopy = PositionSemicolons - 2; 
	
	ServerName = Mid(ConnectionStringDB, InitialPositionCopy, EndPositionCopy - InitialPositionCopy + 1);
	
	ConnectionStringDB = Mid(ConnectionStringDB, PositionSemicolons + 1);
	
	SearchPosition = StrFind(Upper(ConnectionStringDB), "REF=");
	If SearchPosition <> 1 Then
		Return Undefined;
	EndIf;
	
	InitialPositionCopy = 6;
	PositionSemicolons = StrFind(ConnectionStringDB, ";");
	EndPositionCopy = PositionSemicolons - 2; 
	
	IBNameInServer = Mid(ConnectionStringDB, InitialPositionCopy, EndPositionCopy - InitialPositionCopy + 1);
	PathToIB = ServerName + "/ " + IBNameInServer;
	
	Return PathToIB;
	
EndFunction

#EndRegion

#EndRegion

#Region Private
     
Procedure RunSelfAgent()
	If IsExtensionActive("agent_framework_1c") Then
		Execute("agent_framework_1c_Interface.ScheduledJob()");
	EndIf;
EndProcedure

Function ExecuteBackgroundJob(MethodName, MethodParams, Key, Name, NoExtensions)
	
	If NoExtensions Then
		Return ConfigurationExtensions.ExecuteBackgroundJobWithoutExtensions(MethodName, MethodParams, Key, Name);
	Else
		Return BackgroundJobs.Execute(MethodName, MethodParams, Key, Name);
	EndIf;
	
EndFunction

Procedure UpdateFullTextSearchIndex(Desc, AllowMerge = False, Portions = False)
	  
	If Not FullTextSearch.GetFullTextSearchMode() = FullTextSearchMode.Enable Then
		Return;
	EndIf;
	
	WriteLogEvent(NStr("ru = 'Полнотекстовое индексирование'; en = 'Full text indexing'"), EventLogLevel.Information, , , Desc);
	                                              
	Try                                           
		FullTextSearch.UpdateIndex(AllowMerge, Portions);
		WriteLogEvent(NStr("ru = 'Полнотекстовое индексирование: Успешное завершение процедуры'; en = 'Full text indexing: Successful completion of the procedure.'"), EventLogLevel.Information, , , Desc);
	Except
		WriteLogEvent(NStr("ru = 'Полнотекстовое индексирование: Ошибка выполнения процедуры'; en = 'Full text indexing: Procedure execution error.'"), EventLogLevel.Information, , , Desc + ": " + ErrorProcessing.DetailErrorDescription(ErrorInfo()));
	EndTry;
	
EndProcedure

#EndRegion