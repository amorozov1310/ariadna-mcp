// Synthetic extension module: &Around/&Before (EN keywords) over the
// Users manager module of tests/fixtures/xml_en, where Search and
// SetPassword are real exported procedures, so the override edges have
// an actual target to resolve to.
#Region Private

&Around("Search")
Function sample_ext_Search(Filter, Locale)

	Result = ProceedWithCall(Filter, Locale);
	If Result.Count() = 0 Then
		WriteLogEvent("sample_ext", EventLogLevel.Information);
	EndIf;
	Return Result;

EndFunction

&Before("SetPassword")
Procedure sample_ext_SetPasswordBefore(UserNode, NewPassword, RequirePasswordChange, CheckByPwdPolicy = False)

	If IsBlankString(NewPassword) Then
		Raise "Password cannot be blank";
	EndIf;

EndProcedure

#EndRegion
