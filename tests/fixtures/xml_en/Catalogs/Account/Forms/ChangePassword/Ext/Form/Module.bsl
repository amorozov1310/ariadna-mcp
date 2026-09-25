#Region FormEventHandlers

&AtServer
Procedure OnCreateAtServer(Cancel, StandardProcessing)
	
	Parameters.Property("AccountNode", NodeRef);
	
	If Not ValueIsFilled(NodeRef) Then
		Cancel = True;
		Return;
	EndIf;
	
	FillDataToSetPassword(Cancel);
		
EndProcedure

#EndRegion

#Region FormHeaderItemsEventHandlers

&AtClient
Procedure Password1EditTextChange(Item, Text, StandardProcessing)
	
	If Not Items.Password1.PasswordMode Then
	
		Items.Password1.PasswordMode = True;
		Items.Password2.Enabled = True;	

		Password2 = "";
		
	EndIf;
	
EndProcedure

#EndRegion

#Region FormCommandsEventHandlers

&AtClient
Procedure GenerateNewPassword(Command)
	
	Items.Password2.Enabled = False;	
	Items.Password1.PasswordMode = False;
	
	GenerateNewPasswordAtServer();	
	Password2 = Password1;
	
EndProcedure

&AtClient
Procedure ChangePassword(Command)
	
	ClearMessages();
	
	If Password1 = Password2 Then
		
		AllowBlankPassword = CommonAtServer.ObjectAttributeValues(PwdPolicy, "AllowBlankPassword").AllowBlankPassword;
		If Password1 = "" And Not AllowBlankPassword Then
			ShowMessageBox( , Nstr("en = 'Password cannot be empty';ru = 'Пароль не может быть пустым'"));
			Return;
		EndIf;
		
		If ValueIsFilled(Password1) Then
			ErrorString = "";
			If Not CheckPasswordByPwdPolicy(PwdPolicy, Person, Account, Password1, ErrorString) Then
				ShowMessageBox( , ErrorString);
				Return;
			EndIf;
		EndIf;
		
	Else
		ShowMessageBox( , Nstr("en = 'Password mismatch';ru = 'Пароли не совпадают'"));
		Return;
	EndIf;
	
	Task = SetPasswordAtServer();
	If TypeOf(Task) = Type("TaskRef.AgentTask") Then
		Close();
	EndIf;
	
EndProcedure

#EndRegion

#Region Private

Procedure FillDataToSetPassword(Cancel)

	RequirePasswordChange = True;

	Query      = New Query; 
	Query.Text = "SELECT
	             |	Node.Person AS Person,
	             |	Node.Appointment AS Appointment,
	             |	Node.Account.App AS App,
	             |	Node.Account.Username AS Login,
	             |	Node.Status AS Status,
	             |	Node.Account AS Account
	             |INTO VT_NodeInfo
	             |FROM
	             |	Catalog.Node AS Node
	             |WHERE
	             |	Node.Ref = &NodeRef
	             |;
	             |
	             |////////////////////////////////////////////////////////////////////////////////
	             |SELECT
	             |	VT_NodeInfo.Person AS Person,
	             |	VT_NodeInfo.Appointment AS Appointment,
	             |	VT_NodeInfo.Login AS Login,
	             |	VT_NodeInfo.App AS App,
	             |	AppMS.PwdPolicy AS PwdPolicy,
	             |	VT_NodeInfo.Status AS Status,
	             |	VT_NodeInfo.Account AS Account
	             |FROM
	             |	VT_NodeInfo AS VT_NodeInfo
	             |		LEFT JOIN Catalog.AppMS AS AppMS
	             |		ON VT_NodeInfo.App = AppMS.Ref";
	
	Query.SetParameter("NodeRef", NodeRef);
	Result = Query.Execute();
	
	If Result.IsEmpty() Then
		Cancel = True;
		Return;
	EndIf;     
	
	Selection = Result.Select();
	Selection.Next();
	FillPropertyValues(ThisForm, Selection);
	
	IsRevoked = Selection.Status = Enums.NodeStatus.Revoked;
	If IsRevoked Then
		ShowMessageToUser(NStr("en = 'Account revoked.';ru = 'Учетная запись отозвана.'"));
	EndIf;
	
	If ValueIsFilled(PwdPolicy) Then
		TextRequired = Catalogs.PwdPolicy.GetTextPolicyRequirements(PwdPolicy);
		Items.Password1.ToolTip = TextRequired;
		Items.Password2.ToolTip = TextRequired;
	Else
		ShowMessageToUser(StrTemplate(NStr("en = 'The password policy for the application %1 is not specified.';ru = 'Не указана парольная политика для приложения %1.'"), String(App)));		
	EndIf;
	
	Items.FormGenerateNewPassword.Enabled = ValueIsFilled(PwdPolicy) And Not IsRevoked;
	Items.FormChangePassword.Enabled = ValueIsFilled(PwdPolicy) And Not IsRevoked;
	
EndProcedure

&AtServer
Procedure GenerateNewPasswordAtServer()
	Password1 = Catalogs.PwdPolicy.GenerateByPolicy(PwdPolicy);
EndProcedure

&AtServer
Function SetPasswordAtServer()
	
	Return Catalogs.Account.SetPassword(NodeRef, Password1, RequirePasswordChange);
	
EndFunction

&AtServerNoContext
Function CheckPasswordByPwdPolicy(PwdPolicy, Person, Account, Password, ErrorString)
	
	Return Catalogs.PwdPolicy.Check(PwdPolicy, Password, Person, Account, ErrorString);
	
EndFunction

#EndRegion