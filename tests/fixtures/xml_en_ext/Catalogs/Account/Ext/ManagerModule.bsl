// Synthetic fragment for testing extension override directive parsing
// (&Around/&Before, EN keywords). No real &Around example was found in the
// bshp/1idm2 corpora at the time this fixture was assembled; this snippet
// follows the official 1C:Enterprise 8.3 extension directive syntax and
// targets Search/SetPassword, which are real exported functions in the
// main Account manager module (tests/fixtures/xml_en/Catalogs/Account),
// so the override edge has an actual procedure to resolve to.
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
Procedure sample_ext_SetPasswordBefore(AccountNode, NewPassword, RequirePasswordChange, CheckByPwdPolicy = False)

	If IsBlankString(NewPassword) Then
		Raise "Password cannot be blank";
	EndIf;

EndProcedure

#EndRegion
