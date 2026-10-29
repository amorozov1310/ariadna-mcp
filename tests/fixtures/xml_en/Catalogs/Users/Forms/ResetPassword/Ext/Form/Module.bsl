#Region FormEventHandlers

&AtServer
Procedure OnCreateAtServer(Cancel, StandardProcessing)

	UserRef = Parameters.UserRef;

EndProcedure

#EndRegion

#Region FormCommandsEventHandlers

&AtClient
Procedure Save(Command)

	If NewPassword <> Confirmation Then
		Message(NStr("en = 'Passwords do not match'"));
		Return;
	EndIf;
	SaveAtServer();
	Close();

EndProcedure

#EndRegion

#Region Private

&AtServer
Procedure SaveAtServer()

	Catalogs.Users.SetPassword(UserRef, NewPassword, True, True);

EndProcedure

#EndRegion
