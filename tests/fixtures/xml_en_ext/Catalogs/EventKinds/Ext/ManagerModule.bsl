// Synthetic extension module: &After over a procedure the main
// configuration of tests/fixtures/xml_en does not have — the override
// stays unresolved instead of attaching to something unrelated.
#Region Private

&After("AddEventKinds")
Procedure sample_ext_AddEventKinds(AllKinds)

	AllKinds.Add("AccessReview", NStr("en = 'Access review'; ru = 'Пересмотр доступа'"));
	AllKinds.Add("PasswordExpiry", NStr("en = 'Password expiry'; ru = 'Истечение пароля'"));

EndProcedure

#EndRegion
