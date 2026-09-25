#Region EventHandlers

Procedure BeforeWrite(Cancel)
	If Not Cancel And Not ValueIsFilled(Type) Then
		Type = Enums.AccountType.Personal;
	EndIf;
EndProcedure

#EndRegion
