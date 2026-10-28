app_name = "contanext_connector"
app_title = "ContaNext Connector"
app_publisher = "ContaNext"
app_description = "Conector seguro para el plano de control ContaNext SaaS"
app_email = "engineering@contanext.invalid"
app_license = "MIT"

required_apps = ["erpnext", "contanext_guatemala"]

override_doctype_class = {
	"File": "contanext_connector.file_storage.ContaNextFile",
}

before_write_file = "contanext_connector.file_storage.before_write_file"
write_file = "contanext_connector.file_storage.write_file"
delete_file_data_content = "contanext_connector.file_storage.delete_file_data_content"
before_request = ["contanext_connector.file_storage.serve_object_file"]

doc_events = {
	"*": {
		"before_insert": "contanext_connector.entitlements.validate_module_operation",
		"validate": "contanext_connector.entitlements.validate_module_operation",
	},
	"User": {
		"before_insert": "contanext_connector.entitlements.validate_user",
		"validate": "contanext_connector.entitlements.validate_user",
	},
	"Company": {
		"before_insert": "contanext_connector.entitlements.validate_company",
	},
}

has_permission = {
	"Workspace": "contanext_connector.entitlements.has_module_permission",
	"Account": "contanext_connector.entitlements.has_module_permission",
	"Journal Entry": "contanext_connector.entitlements.has_module_permission",
	"Payment Entry": "contanext_connector.entitlements.has_module_permission",
	"GL Entry": "contanext_connector.entitlements.has_module_permission",
	"Cost Center": "contanext_connector.entitlements.has_module_permission",
	"Budget": "contanext_connector.entitlements.has_module_permission",
	"Customer": "contanext_connector.entitlements.has_module_permission",
	"Quotation": "contanext_connector.entitlements.has_module_permission",
	"Sales Order": "contanext_connector.entitlements.has_module_permission",
	"Delivery Note": "contanext_connector.entitlements.has_module_permission",
	"Sales Invoice": "contanext_connector.entitlements.has_module_permission",
	"Supplier": "contanext_connector.entitlements.has_module_permission",
	"Request for Quotation": "contanext_connector.entitlements.has_module_permission",
	"Purchase Order": "contanext_connector.entitlements.has_module_permission",
	"Purchase Receipt": "contanext_connector.entitlements.has_module_permission",
	"Purchase Invoice": "contanext_connector.entitlements.has_module_permission",
	"Item": "contanext_connector.entitlements.has_module_permission",
	"Warehouse": "contanext_connector.entitlements.has_module_permission",
	"Stock Entry": "contanext_connector.entitlements.has_module_permission",
	"Stock Reconciliation": "contanext_connector.entitlements.has_module_permission",
	"Stock Ledger Entry": "contanext_connector.entitlements.has_module_permission",
	"Asset": "contanext_connector.entitlements.has_module_permission",
	"Asset Category": "contanext_connector.entitlements.has_module_permission",
	"Asset Movement": "contanext_connector.entitlements.has_module_permission",
	"Asset Repair": "contanext_connector.entitlements.has_module_permission",
	"Asset Capitalization": "contanext_connector.entitlements.has_module_permission",
	"Lead": "contanext_connector.entitlements.has_module_permission",
	"Opportunity": "contanext_connector.entitlements.has_module_permission",
	"Prospect": "contanext_connector.entitlements.has_module_permission",
	"CRM Note": "contanext_connector.entitlements.has_module_permission",
	"Project": "contanext_connector.entitlements.has_module_permission",
	"Task": "contanext_connector.entitlements.has_module_permission",
	"Timesheet": "contanext_connector.entitlements.has_module_permission",
	"Project Template": "contanext_connector.entitlements.has_module_permission",
	"BOM": "contanext_connector.entitlements.has_module_permission",
	"Work Order": "contanext_connector.entitlements.has_module_permission",
	"Job Card": "contanext_connector.entitlements.has_module_permission",
	"Production Plan": "contanext_connector.entitlements.has_module_permission",
	"POS Profile": "contanext_connector.entitlements.has_module_permission",
	"POS Invoice": "contanext_connector.entitlements.has_module_permission",
	"POS Opening Entry": "contanext_connector.entitlements.has_module_permission",
	"POS Closing Entry": "contanext_connector.entitlements.has_module_permission",
	"GT Accounting Book Run": "contanext_connector.entitlements.has_module_permission",
	"GT Requirement Package": "contanext_connector.entitlements.has_module_permission",
	"GT Physical Count Import": "contanext_connector.entitlements.has_module_permission",
}

permission_query_conditions = {
	"Account": "contanext_connector.entitlements.module_permission_query",
	"Journal Entry": "contanext_connector.entitlements.module_permission_query",
	"Payment Entry": "contanext_connector.entitlements.module_permission_query",
	"GL Entry": "contanext_connector.entitlements.module_permission_query",
	"Cost Center": "contanext_connector.entitlements.module_permission_query",
	"Budget": "contanext_connector.entitlements.module_permission_query",
	"Customer": "contanext_connector.entitlements.module_permission_query",
	"Quotation": "contanext_connector.entitlements.module_permission_query",
	"Sales Order": "contanext_connector.entitlements.module_permission_query",
	"Delivery Note": "contanext_connector.entitlements.module_permission_query",
	"Sales Invoice": "contanext_connector.entitlements.module_permission_query",
	"Supplier": "contanext_connector.entitlements.module_permission_query",
	"Request for Quotation": "contanext_connector.entitlements.module_permission_query",
	"Purchase Order": "contanext_connector.entitlements.module_permission_query",
	"Purchase Receipt": "contanext_connector.entitlements.module_permission_query",
	"Purchase Invoice": "contanext_connector.entitlements.module_permission_query",
	"Item": "contanext_connector.entitlements.module_permission_query",
	"Warehouse": "contanext_connector.entitlements.module_permission_query",
	"Stock Entry": "contanext_connector.entitlements.module_permission_query",
	"Stock Reconciliation": "contanext_connector.entitlements.module_permission_query",
	"Stock Ledger Entry": "contanext_connector.entitlements.module_permission_query",
	"Asset": "contanext_connector.entitlements.module_permission_query",
	"Asset Category": "contanext_connector.entitlements.module_permission_query",
	"Asset Movement": "contanext_connector.entitlements.module_permission_query",
	"Asset Repair": "contanext_connector.entitlements.module_permission_query",
	"Asset Capitalization": "contanext_connector.entitlements.module_permission_query",
	"Lead": "contanext_connector.entitlements.module_permission_query",
	"Opportunity": "contanext_connector.entitlements.module_permission_query",
	"Prospect": "contanext_connector.entitlements.module_permission_query",
	"CRM Note": "contanext_connector.entitlements.module_permission_query",
	"Project": "contanext_connector.entitlements.module_permission_query",
	"Task": "contanext_connector.entitlements.module_permission_query",
	"Timesheet": "contanext_connector.entitlements.module_permission_query",
	"Project Template": "contanext_connector.entitlements.module_permission_query",
	"BOM": "contanext_connector.entitlements.module_permission_query",
	"Work Order": "contanext_connector.entitlements.module_permission_query",
	"Job Card": "contanext_connector.entitlements.module_permission_query",
	"Production Plan": "contanext_connector.entitlements.module_permission_query",
	"POS Profile": "contanext_connector.entitlements.module_permission_query",
	"POS Invoice": "contanext_connector.entitlements.module_permission_query",
	"POS Opening Entry": "contanext_connector.entitlements.module_permission_query",
	"POS Closing Entry": "contanext_connector.entitlements.module_permission_query",
	"GT Accounting Book Run": "contanext_connector.entitlements.module_permission_query",
	"GT Requirement Package": "contanext_connector.entitlements.module_permission_query",
	"GT Physical Count Import": "contanext_connector.entitlements.module_permission_query",
}

scheduler_events = {
	"hourly": [
		"contanext_connector.entitlements.observe_entitlements",
		"contanext_connector.file_storage.scheduled_reconcile_file_storage",
	],
}

extend_bootinfo = ["contanext_connector.entitlements.filter_bootinfo"]
