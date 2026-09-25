#Region Public

// Устанавливает пароль учетной записи.
// При этом происходит вызов Tasks.AgentTask.AddSetPasswordTask и создание задачи агента с типом SetPassword(Установить пароль).
// Если статус узла учетной записи приложения Revoked(Отозвано), то возвращается исключение.  
//
// Параметры:
//  AccountNode           - CatalogRef.Node - ссылка на узел с типом роли App(Приложение).
//  NewPassword           - Строка - пароль для установки.
//  RequirePasswordChange - Булево - требовать смену пароля.
//  CheckByPwdPolicy      - Булево - проверить пароль на соответствие парольной политике.
// 
// Возвращаемое значение:
//	TaskRef.AgentTask - сформированная задача ссылка.
//  Строка - сообщение об ошибке.
//
Function SetPassword(AccountNode, NewPassword, RequirePasswordChange, CheckByPwdPolicy = False) Export
	
	stAccountData = CommonAtServer.ObjectAttributeValues(AccountNode, "Person, Account, Account.App, Status");
	
	IsRevoked = stAccountData.Status = Enums.NodeStatus.Revoked;
	If IsRevoked Then
		Raise NStr("en = 'Account revoked.';ru = 'Учетная запись отозвана.'");
	EndIf;
	
	If CheckByPwdPolicy Then
		
		PwdPolicy = CommonAtServer.ObjectAttributeValues(stAccountData.AccountApp, "Account.App.PwdPolicy").AccountAppPwdPolicy;
		
		If Not ValueIsFilled(PwdPolicy) Then
			Raise StrTemplate(NStr("en = 'The password policy for the application %1 is not specified.';ru = 'Не указана парольная политика для приложения %1.'"), String(stAccountData.AccountApp));
		EndIf;
		
		ErrorString = "";  
		If Not Catalogs.PwdPolicy.Check(PwdPolicy, NewPassword, stAccountData.Person, stAccountData.Account, ErrorString) Then
			Raise ErrorString;
		EndIf;
		
	EndIf;  
	
	Try
		Return Tasks.AgentTask.AddSetPasswordTask(stAccountData.AccountApp, AccountNode, NewPassword, RequirePasswordChange);
	Except 
		Raise ErrorProcessing.DetailErrorDescription(ErrorInfo()); 
	EndTry;
	
EndFunction

// Возвращает статус последней задачи агента с типом SetPassword(Установить пароль). 
// Используется для получения статуса задачи и флага возможнсти создания новой задачи.
//
// Параметры:
//  AccountNode - CatalogRef.Node - ссылка на узел с типом роли App(Приложение).
// 
// Возвращаемое значение:
//	Структура - статус задачи.
//	  * CanCreateTask - Булево - можно создавать новую задачу по установке пароля.
//	  * TextStatus    - Строка - описание статуса Waiting(Ожидает), Completed(Выполнена), Error(Ошибка), Canceled(Отменена), Пустая строка(если задач нет совсем).
//
Function GetSetPasswordStatus(AccountNode) Export
	
	Query = New Query;
	Query.Text = "SELECT TOP 1
	             |	AgenTasks.Ref AS Ref
	             |FROM
	             |	Task.AgentTask AS AgenTasks
	             |WHERE
	             |	AgenTasks.Node = &AccountNode
	             |	AND AgenTasks.Date <= &Date
	             |	AND AgenTasks.Type = &TaskType
	             |
	             |ORDER BY
	             |	AgenTasks.Date DESC";
	
	Query.SetParameter("AccountNode", AccountNode);
	Query.SetParameter("Date", CurrentUniversalDate());
	Query.SetParameter("TaskType", Enums.AgentTaskType.SetPassword);
	
	Result = Query.Execute();
	If Result.IsEmpty() Then
		Return New Structure("CanCreateTask, TextStatus", True, "");
	Else
		Selection = Result.Select();
		Selection.Next();
		Status = InformationRegisters.AgentTaskStatus.GetAgentTaskStatus(Selection.Ref);
		
		CanCreateTask = False;
		StrStatus = NStr("en = 'Waiting';ru = 'Ожидает'");
		
		If Status = Enums.AgentTaskStatus.Completed Then
			StrStatus = NStr("en = 'Completed';ru = 'Выполнена'");
			CanCreateTask = True;
		ElsIf Status = Enums.AgentTaskStatus.Error Then
			StrStatus = NStr("en = 'Error';ru = 'Ошибка'");
		ElsIf Status = Enums.AgentTaskStatus.Canceled Then
			StrStatus = NStr("en = 'Canceled';ru = 'Отменена'");
			CanCreateTask = True;
		EndIf;
		
		Return New Structure("CanCreateTask, TextStatus", CanCreateTask, StrStatus);
	EndIf;
	
EndFunction

#Region REST

// Преобразует данные об учетной записи(Account) для сериализации в JSON.
// Используется http сервисом Model.
//
// Параметры:
//  Account  - Структура - данные учетной записи. 
//           - ВыборкаДанных
//
// Возвращаемое значение:
//   ФиксированнаяСтруктура
//   	* id       - Число  - код учетной записи. 
//		* username - Строка - логин учетной записи.
//		* appId    - Число  - код управляемой системы(AppMS).
//		* personId - Число  - код сотрудника.
//
Function ToJson(Account) Export
	
	Return New FixedStructure("id, username, appId, personId",
		Account.Code, Account.Username, Account.AppCode, Account.PersonCode);
		
EndFunction

// Поиск учетных записей(Account) по фильтру с поддержкой пагинации.
// Используется http сервисом Model.
//
// Параметры:
//  Filter  - Структура - содержит фильтр для поиска. Может быть пустым.
//      * id         - Число, Массив - код учетной записи.
//      * personId   - Число, Массив - код сотрудника.
//      * chunkSize  - Число - размер страницы, по умолчанию 100. Если передано 0, то пагинация не используется.
//      * pageNumber - Число - номер страницы, по умолчанию 1.
//      * countOnly  - Булево - если Истина, вернуть только количество записей; data будет Числом, не массивом.
//  Locale  - Строка - код языка.
//
// Возвращаемое значение:
//   Структура:
//      * data - Массив из Структура (при countOnly = Истина — Число, количество записей):
//          * id       - Число  - код учетной записи.
//          * username - Строка - логин учетной записи.
//          * appId    - Число - код управляемой системы AppMS.
//          * personId - Число - код сотрудника.
//      * chunkSize  - Число - размер страницы.
//      * totalPages - Число - общее количество страниц.
//
Function Search(Filter, Locale) Export
	
	Query = New Query;
	
	ChunkSize = 100;
	PageNumber = 1;
	TotalPages = 0;
	
	If Filter.Property("chunkSize") And Filter.chunkSize > 0 Then
		ChunkSize = Filter.chunkSize;
	EndIf;
	
	If Filter.Property("pageNumber") And Filter.pageNumber > 0 Then
		PageNumber = Filter.pageNumber;
	EndIf;
	
	StrFilter = "";
	
	If Filter.Property("id") Then
		
		idArray = New Array;
		If TypeOf(Filter.id) = Type("Array") Then
			idArray = Filter.id;
		Else
			idArray.Add(Filter.id);
		EndIf;
		
		If idArray.Count() > 0 Then
			StrFilter = "N.Account.Code IN (&id)";
			Query.SetParameter("id", idArray);
		EndIf;
		
	EndIf;
	
	If Filter.Property("personId") Then
		
		personIdArray = New Array;
		If TypeOf(Filter.personId) = Type("Array") Then
			personIdArray = Filter.personId;
		Else
			personIdArray.Add(Filter.personId);
		EndIf;
		
		If personIdArray.Count() > 0 Then
			StrFilter = ?(StrFilter = "", "", StrFilter + " AND ") + "N.Person.Code IN (&personId)";
			Query.SetParameter("personId", personIdArray);
		EndIf;
		
	EndIf;
	
	If Filter.Property("countOnly") And Filter.countOnly Then

		Query.Text =
		"SELECT
		|    COUNT(DISTINCT N.Account) AS RecordCount
		|FROM
		|    Catalog.Node AS N
		|WHERE
		|    N.Role.Type = VALUE(Enum.RoleType.App)
		|    AND N.Account <> VALUE(Catalog.Account.EmptyRef)
		|    AND &Filter";

		Query.Text = StrReplace(Query.Text, "&Filter", ?(StrFilter = "", "TRUE", StrFilter));
		CountResult = Query.Execute().Select();
		RecordCount = 0;
		If CountResult.Next() Then
			RecordCount = CountResult.RecordCount;
		EndIf;

		Return New Structure("data, chunkSize, totalPages", RecordCount, 0, 0);

	EndIf;

	If Filter.Property("chunkSize") And Filter.chunkSize = 0 Then
		
		Query.Text = "SELECT DISTINCT TOP 1000
		             |	N.Account.Code AS Code,
		             |	N.Account.Username AS Username,
		             |	N.Account.App.Code AS AppCode,
		             |	ISNULL(N.Person.Code, 0) AS PersonCode
		             |FROM
		             |	Catalog.Node AS N
		             |WHERE
		             |	N.Role.Type = VALUE(Enum.RoleType.App)
		             |	AND N.Account <> VALUE(Catalog.Account.EmptyRef)
		             |	AND &Filter";
		
		Query.Text = StrReplace(Query.Text, "&Filter", ?(StrFilter = "", "TRUE", StrFilter));
		DataSel = Query.Execute().Select();
		ChunkSize = 0;
		
	Else
		
		Query.Text = "SELECT DISTINCT
		             |	N.Account AS Account,
		             |	N.Account.Code AS Code,
		             |	N.Account.Username AS Username,
		             |	N.Account.App.Code AS AppCode,
		             |	ISNULL(N.Person.Code, 0) AS PersonCode
		             |INTO vtAccountData
		             |FROM
		             |	Catalog.Node AS N
		             |WHERE
		             |	N.Role.Type = VALUE(Enum.RoleType.App)
		             |	AND N.Account <> VALUE(Catalog.Account.EmptyRef)
		             |	AND &Filter
		             |;
		             |
		             |////////////////////////////////////////////////////////////////////////////////
		             |SELECT
		             |	CASE
		             |		WHEN COUNT(vtAccountData.Account) - (CAST(COUNT(vtAccountData.Account) / &ChunkSize AS NUMBER(15, 0))) * &ChunkSize = 0
		             |			THEN CAST(COUNT(vtAccountData.Account) / &ChunkSize AS NUMBER(15, 0))
		             |		ELSE CAST(COUNT(vtAccountData.Account) / &ChunkSize + 0.5 AS NUMBER(15, 0))
		             |	END AS TotalPages
		             |INTO vtTotalPages
		             |FROM
		             |	vtAccountData AS vtAccountData
		             |;
		             |
		             |////////////////////////////////////////////////////////////////////////////////
		             |SELECT
		             |	vtAccountData.Account AS Account,
		             |	vtAccountData.Code AS Code,
		             |	vtAccountData.Username AS Username,
		             |	vtAccountData.AppCode AS AppCode,
		             |	vtAccountData.PersonCode AS PersonCode,
		             |	RECORDAUTONUMBER() AS RecNum
		             |INTO vtAccount
		             |FROM
		             |	vtAccountData AS vtAccountData
		             |;
		             |
		             |////////////////////////////////////////////////////////////////////////////////
		             |SELECT
		             |	MIN(vtAccount.RecNum) AS MinNumber
		             |INTO vtMinRecNumber
		             |FROM
		             |	vtAccount AS vtAccount
		             |;
		             |
		             |////////////////////////////////////////////////////////////////////////////////
		             |SELECT
		             |	vtAccount.Code AS Code,
		             |	vtAccount.Username AS Username,
		             |	vtAccount.AppCode AS AppCode,
		             |	vtAccount.PersonCode AS PersonCode
		             |FROM
		             |	vtAccount AS vtAccount,
		             |	vtMinRecNumber AS MinRecNumber
		             |WHERE
		             |	vtAccount.RecNum >= MinRecNumber.MinNumber + (&PageNumber - 1) * &ChunkSize
		             |	AND vtAccount.RecNum <= MinRecNumber.MinNumber + (&PageNumber - 1) * &ChunkSize + (&ChunkSize - 1)
		             |;
		             |
		             |////////////////////////////////////////////////////////////////////////////////
		             |SELECT
		             |	vtTotalPages.TotalPages AS TotalPages
		             |FROM
		             |	vtTotalPages AS vtTotalPages";
		
		Query.SetParameter("ChunkSize", ChunkSize);
		Query.SetParameter("PageNumber", PageNumber);
		Query.Text = StrReplace(Query.Text, "&Filter", ?(StrFilter = "", "TRUE", StrFilter));
		
		BatchResult = Query.ExecuteBatch();
		DataSel = BatchResult[4].Select();
		TotalPageNumSel = BatchResult[5].Select();
		
		If TotalPageNumSel.Next() Then
			TotalPages = TotalPageNumSel.TotalPages;
		EndIf;
		
	EndIf;
	
	arrData = New Array();
	
	While DataSel.Next() Do
		arrData.Add(ToJson(DataSel));
	EndDo;
	
	Return New Structure("data, chunkSize, totalPages", arrData, ChunkSize, TotalPages);
	
EndFunction  

// Преобразует ссылку на учетную запись(Account) в Markdown.
// Используется http сервисом Model.
//
// Параметры:
//  Ref     - CatalogRef.Account - ссылка на учетную запись.
//  Locale  - Строка - код языка, может быть не задан.
//
// Возвращаемое значение:
//   Строка - Markdown учетной записи(Account).
//
Function RefToMarkdown(Ref, Locale = Undefined) Export
	
	AccountData = CommonAtServer.ObjectAttributeValues(Ref, "Username, App, Type, Attrs");
	
	If AccountData.Type = Enums.AccountType.Personal Then
		TypePresentation = NStr("en = 'Personal';ru = 'Персональная'", Locale);
	ElsIf AccountData.Type = Enums.AccountType.Service Then
		TypePresentation = NStr("en = 'Service';ru = 'Сервисная'", Locale);
	Else
		TypePresentation = NStr("en = 'Not set';ru = 'Не заполнено'", Locale);
	EndIf;
	
	AppPresentation = NStr("en = 'Not set';ru = 'Не заполнено'", Locale);
	If ValueIsFilled(AccountData.App) Then
		AppPresentation = String(AccountData.App);
	EndIf;
	
	TypePresentation = StrReplace(TypePresentation, "&", "&amp;");
	TypePresentation = StrReplace(TypePresentation, "<", "&lt;");
	TypePresentation = StrReplace(TypePresentation, ">", "&gt;");
	AppPresentation = StrReplace(AppPresentation, "&", "&amp;");
	AppPresentation = StrReplace(AppPresentation, "<", "&lt;");
	AppPresentation = StrReplace(AppPresentation, ">", "&gt;");
	
	md = "# " + AccountData.Username;
	
	Attrs = DataUtil.ParseJSON(AccountData.Attrs);
	AttrsRows = "";
	AttrsCount = 0;
	
	If Attrs <> Undefined Then
		
		For Each KV In Attrs Do
			
			AttrsCount = AttrsCount + 1;
			
			AttrName = StrReplace(String(KV.Key), "&", "&amp;");
			AttrName = StrReplace(AttrName, "<", "&lt;");
			AttrName = StrReplace(AttrName, ">", "&gt;");
			
			AttrValue = StrReplace(String(KV.Value), "&", "&amp;");
			AttrValue = StrReplace(AttrValue, "<", "&lt;");
			AttrValue = StrReplace(AttrValue, ">", "&gt;");
			
			AttrsRows = AttrsRows + "
			|<tr><td>" + AttrName + "</td><td>" + AttrValue + "</td></tr>";
			
		EndDo;
	EndIf;
	
	md = md + "
	|
	|**" + NStr("en = 'Type:';ru = 'Тип:'", Locale) + "** " + TypePresentation + "  
	|**" + NStr("en = 'App:';ru = 'Приложение:'", Locale) + "** " + AppPresentation;
	
	If AttrsCount > 0 Then
		md = md + "
		|
		|<details>
		|<summary>" + NStr("en = 'Attributes';ru = 'Атрибуты'", Locale) + " (" + Format(AttrsCount, "NZ=0; NG=0") + ")</summary>
		|
		|<table>
		|<thead>
		|<tr><th>" + NStr("en = 'Attribute';ru = 'Атрибут'", Locale) + "</th><th>" + NStr("en = 'Value';ru = 'Значение'", Locale) + "</th></tr>
		|</thead>
		|<tbody>" + AttrsRows + "
		|</tbody>
		|</table>
		|</details>";
	EndIf;
	
	Return md;
	
EndFunction

// Проверяет разрешение на доступ и устанавливает настройки доступа.
// 
// Параметры:
// stPermission - Структура:
//                 * Name   - Строка - Название доступа.
//                 * Action - Строка - Действие. 
//
// Возвращаемое значение:
//   - Неопределено, ЛюбоеЗначение - например: 
//                                      - Булево    - True - доступ разрешен, False - нет разрешения на доступ.
//                                      - Структура - результат проверки разрешения доступа.
//
Function CheckPermitts(stPermission) Export 
	
	FuncName        = "CheckPermission" + "_" + stPermission.Name + "_" + stPermission.Action + "(stPermission)";
	Result          = Eval(FuncName);
	
	Return Result;  
	
EndFunction 

#EndRegion

#EndRegion

#Region EventHandlers

Procedure PresentationFieldsGetProcessing(Fields, StandardProcessing)
	StandardProcessing = False;
	Fields.Add("Username");
EndProcedure

Procedure PresentationGetProcessing(Data, Presentation, StandardProcessing)
	StandardProcessing = False;
	Presentation = Data.Username;
EndProcedure

#EndRegion

#Region Internal

//Обновление разрешений на доступ для объекта Account.
// - формирует массив структур доступных разрешений (Name, Action, Parameterized).
// - вызывает PermissionsAndACL.RegisterPermissions для актуализации разрешений в каталоге доступа(Permissions).
//
Procedure CheckRegisteredPermissions() Export
	
	Permissions = New Array;
	
	PermissionsAndACL.RegisterPermissions("Catalog.Account", Permissions);
	                                                                       
EndProcedure

#EndRegion
