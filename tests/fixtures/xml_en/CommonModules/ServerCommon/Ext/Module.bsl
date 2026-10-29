#Region Public

// Executes an HTTP request and wraps the answer into a structure.
//
// Parameters:
//   Url - String - service address.
//   Method - String - GET, POST, ...
//   Data - Structure, Undefined - request body.
//   Timeout - Number - seconds.
//
// Returns:
//   Structure - Success, StatusCode, Body, ErrorText.
//
Function ExecuteHttpRequest(Url, Method, Data = Undefined, Timeout = 30) Export

	Result = NewRequestResult();
	Try
		Connection = New HTTPConnection(HostFromUrl(Url), , , , , Timeout);
		Request = New HTTPRequest(PathFromUrl(Url));
		If Data <> Undefined Then
			Request.SetBodyFromString(ToJsonString(Data));
		EndIf;
		Response = Connection.CallHTTPMethod(Method, Request);
		Result.StatusCode = Response.StatusCode;
		Result.Body = Response.GetBodyAsString();
		Result.Success = Response.StatusCode < 300;
	Except
		Result.ErrorText = DetailErrorDescription(ErrorInfo());
	EndTry;
	Return Result;

EndFunction

// Runs a method by name with an array of parameters.
Procedure Run(Val MethodName, Val MethodParams = Undefined) Export

	ParamsText = "";
	If MethodParams <> Undefined Then
		For i = 0 To MethodParams.UBound() Do
			ParamsText = ParamsText + ?(i = 0, "", ",") + "MethodParams[" + Format(i, "NG=0") + "]";
		EndDo;
	EndIf;
	Execute(MethodName + "(" + ParamsText + ")");

EndProcedure

// Writes an informational event to the event log.
Procedure LogInfo(EventName, Text) Export

	WriteLogEvent(EventName, EventLogLevel.Information, , , Text);

EndProcedure

// Writes an error event to the event log.
Procedure LogError(EventName, Text) Export

	WriteLogEvent(EventName, EventLogLevel.Error, , , Text);

EndProcedure

// Sets the value of a constant only when it differs from the stored one.
Procedure SetConstant(Name, Value) Export

	If Constants[Name].Get() <> Value Then
		Constants[Name].Set(Value);
		LogInfo("Constants", Name);
	EndIf;

EndProcedure

// Clears the session cache.
Procedure ResetSessionCache() Export

	RefreshReusableValues();

EndProcedure

#EndRegion

#Region Private

Function NewRequestResult()

	Result = New Structure;
	Result.Insert("Success", False);
	Result.Insert("StatusCode", 0);
	Result.Insert("Body", "");
	Result.Insert("ErrorText", "");
	Return Result;

EndFunction

Function HostFromUrl(Url)

	Position = StrFind(Url, "/", , 9);
	Return ?(Position = 0, Url, Left(Url, Position - 1));

EndFunction

Function PathFromUrl(Url)

	Position = StrFind(Url, "/", , 9);
	Return ?(Position = 0, "/", Mid(Url, Position));

EndFunction

Function ToJsonString(Data)

	Writer = New JSONWriter;
	Writer.SetString();
	WriteJSON(Writer, Data);
	Return Writer.Close();

EndFunction

Procedure CheckAccess(Val RoleName)

	If Not IsInRole(RoleName) Then
		Raise NStr("en = 'Access denied'");
	EndIf;

EndProcedure

#EndRegion
