// Synthetic common module for tests/test_call_tree.py: a scheduled job
// entry point (RunJobs) that fans out into local procedures, plus calls
// to platform functions the indexer must filter out.

#Region Public

// Scheduled job handler: runs every pending job.
Procedure RunJobs() Export

	StartedAt = CurrentUniversalDateInMilliseconds();
	WriteLogEvent(NStr("en = 'Jobs.Start'"), EventLogLevel.Information);
	If Not JobsEnabled() Then
		Return;
	EndIf;
	ClearMessages();
	PrepareQueue();
	RunLocalJob();
	RunRemoteJobs();
	CleanupFinished();
	ReportDuration(StartedAt);

EndProcedure

// Runs the job that belongs to this infobase.
Procedure RunLocalJob() Export

	Job = NextJob();
	If Job = Undefined Then
		Return;
	EndIf;
	Try
		ExecuteJob(Job);
		MarkJob(Job, "Done");
	Except
		ErrorText = DetailErrorDescription(ErrorInfo());
		WriteLogEvent(NStr("en = 'Jobs.Error'"), EventLogLevel.Error, , , ErrorText);
		MarkJob(Job, "Failed");
	EndTry;

EndProcedure

// Hands the remaining jobs over to the external service.
Procedure RunRemoteJobs() Export

	Job = NextJob();
	While Job <> Undefined Do
		Result = IntegrationServer.SendRequest("POST", New Structure("Job", String(Job)));
		If Result.Success Then
			MarkJob(Job, "Sent");
		Else
			MarkJob(Job, "Failed");
			WriteLogEvent(NStr("en = 'Jobs.Send'"), EventLogLevel.Warning, , ,
				BriefErrorDescription(ErrorInfo()));
		EndIf;
		Job = NextJob();
	EndDo;

EndProcedure

#EndRegion

#Region Private

Function JobsEnabled()

	If IsBlankString(InfoBaseConnectionString()) Then
		Return False;
	EndIf;
	Return Metadata.ScheduledJobs.Find("RunJobs") <> Undefined;

EndFunction

Procedure PrepareQueue()

	ServerCommon.LogInfo("Jobs", NStr("en = 'Preparing the queue'"));

EndProcedure

Function NextJob()

	Query = New Query;
	Query.Text =
	"SELECT TOP 1
	|	JobStatus.Job AS Job
	|FROM
	|	InformationRegister.JobStatus AS JobStatus
	|WHERE
	|	JobStatus.State = ""Queued""";
	Selection = Query.Execute().Select();
	Return ?(Selection.Next(), Selection.Job, Undefined);

EndFunction

Procedure ExecuteJob(Job)

	If Not ValueIsFilled(Job) Then
		Raise NStr("en = 'Empty job'");
	EndIf;
	ServerCommon.Run("Jobs.Execute", New Array);

EndProcedure

Procedure MarkJob(Job, State)

	IntegrationServer.RegisterJobState(Job, State);

EndProcedure

Procedure CleanupFinished()

	ServerCommon.ResetSessionCache();

EndProcedure

Procedure ReportDuration(StartedAt)

	Duration = CurrentUniversalDateInMilliseconds() - StartedAt;
	ServerCommon.LogInfo("Jobs", Format(Duration, "NG=0"));

EndProcedure

#EndRegion
