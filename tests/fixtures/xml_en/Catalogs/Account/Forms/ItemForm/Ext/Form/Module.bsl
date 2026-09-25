
#Region FormEventHandlers   

&AtServer
Procedure OnCreateAtServer(Cancel, StandardProcessing)
	CheckAvailabilityOfElements();
EndProcedure

&AtServer
Procedure OnReadAtServer(CurrentObject)
	
	stPosition = New Structure("Type, Behavior, ControlRepresentation, Representation", FormGroupType.UsualGroup, UsualGroupBehavior.Collapsible, UsualGroupControlRepresentation.Picture, UsualGroupRepresentation.None);
	DataUtil.DisplayJSONAttributesOnForm(Object.Attrs, ThisForm, stPosition);
	
	accountNodes = Catalogs.Node.GetAccountNodes(Object.App, , , Object.Ref);
	
	If accountNodes.Count() Then
		Person = accountNodes[Object.Ref].Person;
	EndIf;
	Items.Person.Visible = ValueIsFilled(Person);
	
EndProcedure 

&AtServer
Procedure BeforeWriteAtServer(Cancel, CurrentObject, WriteParameters) 
	
	SaveAttributes(CurrentObject);
	
EndProcedure

#EndRegion

#Region FormTableItemsEventHandlersTableAttrs
 
&AtClient
Procedure AttributesTableSelection(Item, SelectedRow, Field, StandardProcessing)

	TableName = Item.Name; 
	currData  = Items[TableName].CurrentData;
	
	If StrFind(Item.CurrentItem.Name, "Value") <> 0 Then
		If Not ValueIsFilled(currData.Value) Then
			Items[TableName + "Value"].TypeRestriction = New TypeDescription("String"); 
		Else	
			Items[TableName + "Value"].ChooseType = False;
		EndIf;

	EndIf;
	
EndProcedure 

&AtClient
Procedure AttributesTableOnChange(Item) 
	
    TableName = Item.Name; 
	currData  = Items[TableName].CurrentData;
	If Not currData = Undefined Then
		currData.IsChange = True;
	EndIf;
	 
EndProcedure

#EndRegion

#Region FormCommandsEventHandlers

&AtClient
Procedure RelatedNodes(Command) 
	
	FormParameters = New Structure("Filter", New Structure("Value", Object.Ref));
    OpenForm("FilterCriterion.RelatedNodes.Form.ListForm", FormParameters); 
	
EndProcedure

#EndRegion

#Region Private

&AtServer
Procedure SaveAttributes(CurrentObject)
	
	strAttrTable = New Structure("AttrTable", Undefined); 
	
	FillPropertyValues(strAttrTable, ThisForm);
	If strAttrTable.AttrTable = Undefined Then 
		Return;
	EndIf;
	
	tAttrTable = FormAttributeToValue("AttrTable");
	If (TypeOf(tAttrTable) = Type("ValueTree") And tAttrTable.Rows.FindRows(New Structure("IsChange", True), True).Count() > 0)
	   Or (TypeOf(tAttrTable) = Type("ValueTable") And tAttrTable.FindRows(New Structure("IsChange", True)).Count() > 0) Then
	
	   TaskParams = DataUtil.ConvertAttributesTableToJSON(tAttrTable, ThisForm);
	   
	EndIf;
	
EndProcedure

&AtServer
Procedure CheckAvailabilityOfElements()
	If IsInRole("Administrator") Then  
		Items.Type.ReadOnly = False;		
	EndIf;	
EndProcedure

#EndRegion