app_name = "contanext_connector"
app_title = "ContaNext Connector"
app_publisher = "ContaNext"
app_description = "Conector de ContaNext SaaS"
app_email = "dev@contanext.com"
app_license = "AGPL-3.0-only"

scheduler_events = {
	"cron": {"*/5 * * * *": ["contanext_connector.tasks.process_outbox"]},
}
