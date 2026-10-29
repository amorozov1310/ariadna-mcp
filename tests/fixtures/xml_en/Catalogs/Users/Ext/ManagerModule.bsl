#Region Public

// Searches users by a filter and returns their JSON presentations.
//
// Parameters:
//   Filter - Structure - Login, FullName, Department.
//   Locale - String - language code for presentations.
//
// Returns:
//   Array of String - JSON presentations.
//
Function Search(Filter, Locale) Export

	Query = New Query;
	Query.Text =
	"SELECT
	|	Users.Ref AS Ref,
	|	Users.Login AS Login,
	|	Users.FullName AS FullName
	|FROM
	|	Catalog.Users AS Users
	|WHERE
	|	NOT Users.DeletionMark";
	If Filter.Property("Login") Then
		Query.Text = Query.Text + "
		|	AND Users.Login = &Login";
		Query.SetParameter("Login", Filter.Login);
	EndIf;

	Result = New Array;
	Selection = Query.Execute().Select();
	While Selection.Next() Do
		Result.Add(ToJson(Selection, Locale));
	EndDo;
	Return Result;

EndFunction

// Finds a user by login.
//
// Returns:
//   CatalogRef.Users, Undefined.
//
Function FindByLogin(Login) Export

	Ref = FindByAttribute("Login", Login);
	Return ?(Ref.IsEmpty(), Undefined, Ref);

EndFunction

// Sets a new password for the user.
//
// Parameters:
//   UserNode - CatalogRef.Users - user.
//   NewPassword - String - password.
//   RequirePasswordChange - Boolean - ask to change the password on next logon.
//   CheckByPwdPolicy - Boolean - validate against the password policy.
//
Procedure SetPassword(UserNode, NewPassword, RequirePasswordChange, CheckByPwdPolicy = False) Export

	If CheckByPwdPolicy And Not PasswordMatchesPolicy(NewPassword) Then
		Raise NStr("en = 'The password does not match the policy'");
	EndIf;

	UserObject = UserNode.GetObject();
	UserObject.AdditionalProperties.Insert("NewPassword", NewPassword);
	UserObject.AdditionalProperties.Insert("RequirePasswordChange", RequirePasswordChange);
	UserObject.Write();
	ServerCommon.LogInfo("Users.SetPassword", UserObject.Login);

EndProcedure

#EndRegion

#Region Private

Function ToJson(Selection, Locale)

	Data = New Structure;
	Data.Insert("login", Selection.Login);
	Data.Insert("name", Selection.FullName);
	Data.Insert("locale", Locale);
	Writer = New JSONWriter;
	Writer.SetString();
	WriteJSON(Writer, Data);
	Return Writer.Close();

EndFunction

Function PasswordMatchesPolicy(Password)

	Return StrLen(Password) >= 8;

EndFunction

#EndRegion
