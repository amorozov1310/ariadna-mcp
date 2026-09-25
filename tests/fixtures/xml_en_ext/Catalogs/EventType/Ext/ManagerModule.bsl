#Region Private 

&After("AddEventTypes")
Procedure Policy_Cont_AddEventTypes(AllEventTypes)
 
	AllEventTypes.Add("CheckFactAccessPolicyViolationsWithFilter", NStr("en = 'Checking for access policy violations using a filter.'; ru = 'Проверка фактов нарушения политики доступа с помощью фильтра.' "));
	AllEventTypes.Add("AutoapprovingAccessPolicyViolation", NStr("en = 'Auto-negotiate access policy violation.'; ru = 'Автосогласование нарушения политики доступа.' "));

EndProcedure    

#EndRegion