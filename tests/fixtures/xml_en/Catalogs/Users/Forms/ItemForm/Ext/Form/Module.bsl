#Region FormEventHandlers

&AtServer
Procedure OnCreateAtServer(Cancel, StandardProcessing)

	If Not ValueIsFilled(Object.Ref) Then
		Object.UserType = Enums.UserType.Internal;
	EndIf;
	Employee = Catalogs.Employees.EmptyRef();

EndProcedure

&AtClient
Procedure AfterWrite(WriteParameters)

	Notify("Write_Users", , Object.Ref);

EndProcedure

#EndRegion

#Region FormCommandsEventHandlers

&AtClient
Procedure ResetPassword(Command)

	OpenForm("Catalog.Users.Form.ResetPassword", New Structure("UserRef", Object.Ref), ThisObject);

EndProcedure

#EndRegion
