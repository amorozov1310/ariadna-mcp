#Region Public

// Возвращает настройки агента.
// Получение структуры настроек происходит из реквизита AgentSettings справочников AppHR или AppMS,
// в зависимости от типа агента.
// 
// Параметры:
//  AgentApp   - CatalogRef.AppHR - ссылка на приложение агента.
//			   - CatalogRef.AppMS
//	TaskNumber - Число - номер задачи агента для установки статуса.
//
// Возвращаемое значение:
//   Структура - настройки агента для передачи во внешний агент. Если в хранилище приложения не было настроек
//              (ранее возвращалось Неопределено), возвращается структура минимум с ключом OTelSettings.
//              Если тип значения настроек не Структура и не ФиксированнаяСтруктура, OTel не добавляется (см. журнал регистрации AGENT.GET_SETTINGS.OTEL).
//              * OTelSettings - Структура - настройки OpenTelemetry для агента (см. менеджер константы
//                Constants.OTelSettings, DefaultSettings). Сначала берутся значения из глобальной константы;
//                если в настройках приложения задан вложенный OTelSettings с заполненными Otel_CollectorHost,
//                Otel_CollectorPort или Otel_Authorization, эти три параметра подставляются вместо значений
//                константы. В ответ агенту возвращается итоговая полная структура OTelSettings.
//
Function AgentGetSettings(AgentApp, TaskNumber) Export
	
	If TaskNumber <> Undefined Then
		Tasks.AgentTask.SetStatus(AgentApp, TaskNumber, Enums.AgentTaskStatus.Completed);
	EndIf;	
	
	Settings = ?(TypeOf(AgentApp) = Type("CatalogRef.AppHR"),
		Catalogs.AppHR.GetAgentSettings(AgentApp),
		Catalogs.AppMS.GetAgentSettings(AgentApp));
	
	Return AgentSettingsWithOTel(Settings);
	
EndFunction

// Возвращает задачу агента для исполнения.
// см. Tasks.AgentTask.GetNextTask.
// 
// Параметры:
//  AgentApp   - CatalogRef.AppHR - ссылка на приложение агента.
//			   - CatalogRef.AppMS
//
// Возвращаемое значение:
//   - Неопределено - если нет задач для исполнения (нет активных задач; аналог пустого ответа GetNextTask).
//   - Структура - то же, что возвращает Tasks.AgentTask.GetNextTask (type, id, params, traceparent).
//			Для HTTP GET /task/next в HTTPServices.Agent тело JSON — New FixedStructure("type, id, params", ...);
//			traceparent — в заголовок.
//
Function AgentGetNextTask(AgentApp) Export
	
	Return Tasks.AgentTask.GetNextTask(AgentApp);
	
EndFunction

// Получает данные от агента и запускает обработку этих данных.
// При это статус задачи агента устанавливается в Processing(Обработка). 
//
// Параметры:
//  AgentApp - CatalogRef.AppMS - ссылка на приложение агента.
//		     - CatalogRef.AppHR 
//  Entity     - Строка - имя загружаемой сущности. Значения: "persons", "appointments", "orgunits", "roles",
//               "accounts", "accountroles", "resources", "resourceaccesstypes", "accesstoresources".
//	IsFullSet  - Булево - это полный набор данных или частичный. 
//	DataStr    - Строка - данные в формате JSON.
//	TaskNumber - Число - номер задачи агента, для установки статуса выполнения.
//  Sync       - Булево - флаг полной синхронизации.
//
Procedure AgentDataPost(AgentApp, Entity, IsFullSet, DataStr, TaskNumber, Sync) Export

	If TaskNumber <> Undefined Then
		Tasks.AgentTask.SetStatus(AgentApp, TaskNumber, Enums.AgentTaskStatus.Processing);		
	EndIf;

	If Sync Then
		ProcessAgentData(AgentApp, Entity, DataStr, IsFullSet, TaskNumber);
	Else
		Params = New Array();
		Params.Add(AgentApp);
		Params.Add(Entity);
		Params.Add(DataStr);
		Params.Add(IsFullSet);
		Params.Add(TaskNumber);
		
		Description = "ProcessAgentData: " + String(AgentApp) + ": " + Entity;
		
		BackgroundJobs.Execute("AgentDataUtils.ProcessAgentData", Params, , Description);
	EndIf; 
	
EndProcedure

// Устанавливает статус задачи агента.
// см. Tasks.AgentTask.SetStatus.
// 
// Параметры:
//  AgentApp   - CatalogRef.AppHR - ссылка на приложение агента.
//			   - CatalogRef.AppMS
//  TaskNumber - Число - номер задачи агента(AgentTask).
//  Status     - EnumRef.AgentTaskStatus - статус задачи для установки.
//  AdditionalAgentData - Структура, Неопределено - дополнительные данные.
//
Procedure AgentTaskStatusPost(AgentApp, TaskNumber, Status, AdditionalAgentData) Export
	Tasks.AgentTask.SetStatus(AgentApp, TaskNumber, Status, AdditionalAgentData);	
EndProcedure

// Обрабатывает данные агента. Вызов "ProcessData" соответствующего адаптера приложения.
// Создаёт Core span (OTel) как дочерний к AgentTask span при наличии TaskNumber и найденной задаче.
// При TaskNumber = Undefined или задача не найдена — Core span корневой (fallback trace).
//
// Параметры:
//  App - CatalogRef.AppMS - ссылка на управляемую систему.
//		- CatalogRef.AppHR - ссылка на кадровый источник.
//  Entity - Строка - имя загружаемой сущности. Значения: "persons", "appointments", "orgunits", "roles",
//           "accounts", "accountroles", "resources", "resourceaccesstypes", "accesstoresources".
//	DataStr - Строка - данные в формате JSON.
//	IsFullSet - Булево - это полный набор данных или частичный.
//	TaskNumber - Число - номер задачи агента, для установки статуса выполнения.
//
Procedure ProcessAgentData(App, Entity, DataStr, IsFullSet, TaskNumber) Export

	TraceContext     = BuildProcessAgentDataTraceContext(App, Entity, IsFullSet, TaskNumber);
	TraceId          = TraceContext.TraceId;
	CoreSpanId       = TraceContext.CoreSpanId;
	TraceLogMetadata = TraceContext.TraceLogMetadata;

	Try
		If IsBlankString(DataStr) Then
			Data = New Array;
		Else
			Data = DataUtil.ParseJSON(DataStr);
		EndIf;
		
		Result = DataProcessors[App.Adapter].ProcessData(App, Entity, Data, IsFullSet);

		If Result And TaskNumber <> Undefined Then
			Tasks.AgentTask.SetStatus(App, TaskNumber, Enums.AgentTaskStatus.Completed);
		EndIf;
	Except
		ErrorMessage = ErrorProcessing.BriefErrorDescription(ErrorInfo());

		Try
			If CoreSpanId <> Undefined Then
				OTel.SpanEnd(TraceId, CoreSpanId, , True, ErrorMessage);
				CoreSpanId = Undefined;
			EndIf;
		Except
			WriteLogEvent("BASE.AgentDataUtils.OTel", EventLogLevel.Warning, , ,
				ErrorProcessing.DetailErrorDescription(ErrorInfo()));
		EndTry;

		If TaskNumber <> Undefined Then
			Tasks.AgentTask.SetStatus(App, TaskNumber, Enums.AgentTaskStatus.Error,
				New Structure("error", ErrorDescription()));
		Else
			WriteLogEvent("AGENT.TASK.PROCESS_DATA", EventLogLevel.Error, ,
				?(TraceLogMetadata = Undefined, App, TraceLogMetadata),
				ErrorProcessing.DetailErrorDescription(ErrorInfo()));
		EndIf;
	EndTry;

	Try
		If CoreSpanId <> Undefined Then
			OTel.SpanEnd(TraceId, CoreSpanId);
		EndIf;
	Except
		WriteLogEvent("BASE.AgentDataUtils.OTel", EventLogLevel.Warning, , ,
			ErrorProcessing.DetailErrorDescription(ErrorInfo()));
	EndTry;

EndProcedure

#EndRegion

#Region Private

// Инициализирует OTel-контекст трассировки для ProcessAgentData.
// Возвращает структуру с ключами TraceId, CoreSpanId, TraceLogMetadata.
// Если OTel отключен или возникла ошибка — ключи равны Неопределено.
Function BuildProcessAgentDataTraceContext(App, Entity, IsFullSet, TaskNumber)

	TraceId          = Undefined;
	CoreSpanId       = Undefined;
	TraceLogMetadata = Undefined;

	Try
		OTelSettings = SessionParameters.OTelSettings.Get();
		If OTelSettings <> Undefined And OTelSettings.Otel_RegisterSpans Then

			ParentSpanId = Undefined;
			If TaskNumber <> Undefined Then
				TaskRef = Tasks.AgentTask.FindByNumber(TaskNumber);
				If Not TaskRef.IsEmpty() Then
					Ids          = Tasks.AgentTask.TaskTraceIds(TaskRef);
					TraceId      = Ids.TraceId;
					ParentSpanId = Ids.SpanId;
				Else
					TraceId = OTel.TraceId();
				EndIf;
			Else
				TraceId = OTel.TraceId();
			EndIf;

			AppAttrs = CommonAtServer.ObjectAttributeValues(App, "Code, Description");
			Attrs = New Map;
			If TypeOf(App) = Type("CatalogRef.AppHR") Then
				Attrs.Insert("idm.app.type", "hr");
			Else
				Attrs.Insert("idm.app.type", "ms");
			EndIf;
			Attrs.Insert("idm.app.code", AppAttrs.Code);
			Attrs.Insert("idm.app.name", AppAttrs.Description);
			Attrs.Insert("idm.entity.type", Entity);
			If IsFullSet Then
				Attrs.Insert("idm.import.mode", "full");
			Else
				Attrs.Insert("idm.import.mode", "delta");
			EndIf;
			If TaskNumber = Undefined Then
				Attrs.Insert("idm.import.trigger", "direct");
			Else
				Attrs.Insert("idm.import.trigger", "task");
			EndIf;

			If ParentSpanId <> Undefined Then
				CoreKind = 1;
			Else
				CoreKind = 2;
			EndIf;

			CoreSpanId = OTel.SpanCreate("AgentDataUtils", TraceId, , ParentSpanId,
				"ProcessAgentData", , , Attrs, , , , CoreKind);

		EndIf;

		If TraceId <> Undefined And CoreSpanId <> Undefined Then
			TraceLogMetadata = OTel.BuildLogMetadata(
				OTel.BuildScope("AgentDataUtils"), TraceId, CoreSpanId);
		EndIf;

	Except
		WriteLogEvent("BASE.AgentDataUtils.OTel", EventLogLevel.Warning, , ,
			ErrorProcessing.DetailErrorDescription(ErrorInfo()));
	EndTry;

	Return New Structure("TraceId, CoreSpanId, TraceLogMetadata", TraceId, CoreSpanId, TraceLogMetadata);

EndFunction

Function AgentSettingsWithOTel(Settings)
	
	If Settings <> Undefined Then
		
		SettingsType = TypeOf(Settings);
		
		If SettingsType <> Type("Structure") And SettingsType <> Type("FixedStructure") Then
			
			LogText = NStr("ru = 'Неожиданный тип настроек агента, блок OTelSettings не добавлен. Тип: '; en = 'Unexpected agent settings type, OTelSettings not added. Type: '")
				+ String(SettingsType);
			WriteLogEvent("AGENT.GET_SETTINGS.OTEL", EventLogLevel.Error, , , LogText);
			
			Return Settings;
			
		EndIf;
		
	EndIf;
	
	MergedOTelSettings = CopyOTelSettingsFromConstants();
	Result = BuildAgentSettingsResult(Settings);
	AgentOTelOverrides = AgentOTelOverridesFromSettings(Settings);
	
	If AgentOTelOverrides <> Undefined Then
		ApplyAgentOTelOverrides(MergedOTelSettings, AgentOTelOverrides);
	EndIf;
	
	Result.Insert("OTelSettings", MergedOTelSettings);
	
	Return Result;
	
EndFunction

Function CopyOTelSettingsFromConstants()
	
	Return ShallowCopyStructure(Constants.OTelSettings.GetSettings());
	
EndFunction

Function BuildAgentSettingsResult(Settings)
	
	If Settings = Undefined Then
		Return New Structure();
	EndIf;
	
	Return ShallowCopyStructure(Settings);
	
EndFunction

Function AgentOTelOverridesFromSettings(Settings)
	
	If Settings = Undefined Then
		Return Undefined;
	EndIf;
	
	AgentOTelSettings = Undefined;
	
	If Not Settings.Property("OTelSettings", AgentOTelSettings) Then
		Return Undefined;
	EndIf;
	
	NestedType = TypeOf(AgentOTelSettings);
	
	If NestedType <> Type("Structure") And NestedType <> Type("FixedStructure") Then
		Return Undefined;
	EndIf;
	
	DecryptableOverrides = ShallowCopyStructure(AgentOTelSettings);
	DataUtil.DecryptEncryptedValuesOfAttrs(DecryptableOverrides);
	
	Return DecryptableOverrides;
	
EndFunction

Procedure ApplyAgentOTelOverrides(OTelSettings, AgentOTelOverrides)
	
	OverrideKeys = Constants.OTelSettings.GetOverrideSettingsNames();
	
	For Each OverrideKey In OverrideKeys Do
		
		OverrideValue = Undefined;
		
		If AgentOTelOverrides.Property(OverrideKey, OverrideValue) And ValueIsFilled(OverrideValue) Then
			OTelSettings.Insert(OverrideKey, OverrideValue);
		EndIf;
		
	EndDo;
	
EndProcedure

Function ShallowCopyStructure(Source)
	
	Result = New Structure();
	
	For Each KeyValue In Source Do
		Result.Insert(KeyValue.Key, KeyValue.Value);
	EndDo;
	
	Return Result;
	
EndFunction

#EndRegion
