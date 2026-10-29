#Region Public

// Returns the integration settings of the current session.
// TraceSettings are cached in a session parameter, see InitTraceSettings.
//
// Returns:
//   Structure - Endpoint, Timeout and, when tracing is on, Trace.
//
Function GetIntegrationSettings() Export

	Settings = New Structure;
	Settings.Insert("Endpoint", Constants.ServiceUrl.Get());
	Settings.Insert("Timeout", 30);
	// TraceSettings is Undefined while tracing is switched off.
	TraceSettings = SessionParameters.TraceSettings;
	If TraceSettings <> Undefined Then
		Settings.Insert("Trace", TraceSettings);
	EndIf;
	Return Settings;

EndFunction

// Fills TraceSettings on session start.
Procedure InitTraceSettings() Export

	SessionParameters.TraceSettings = New FixedStructure("Enabled", False);

EndProcedure

// Sends a request to the external service.
//
// Parameters:
//   Method - String - HTTP method.
//   Data - Structure - request body.
//
// Returns:
//   Structure - see ServerCommon.ExecuteHttpRequest.
//
Function SendRequest(Method, Data) Export

	Settings = GetIntegrationSettings();
	Result = ServerCommon.ExecuteHttpRequest(Settings.Endpoint, Method, Data, Settings.Timeout);
	If Not Result.Success Then
		WriteLogEvent(NStr("en = 'Integration'"), EventLogLevel.Error, , , Result.ErrorText);
	EndIf;
	Return Result;

EndFunction

// Registers the job state reported by the external service.
Procedure RegisterJobState(Job, State) Export

	If JobState(Job) = State Then
		Return;
	EndIf;
	RecordManager = InformationRegisters.JobStatus.CreateRecordManager();
	RecordManager.Job = Job;
	RecordManager.State = State;
	RecordManager.Write();

EndProcedure

#EndRegion

#Region Private

Function JobState(Job)

	Query = New Query;
	Query.Text =
	"SELECT
	|	JobStatus.State AS State
	|FROM
	|	InformationRegister.JobStatus AS JobStatus
	|WHERE
	|	JobStatus.Job = &Job";
	Query.SetParameter("Job", Job);
	Selection = Query.Execute().Select();
	If Selection.Next() Then
		Return Selection.State;
	EndIf;
	Return "";

EndFunction

#EndRegion
