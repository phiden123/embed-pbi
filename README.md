# embed-pbi
embed-pbi

## Azure App Service monitoring

The application emits one structured `performance_metric` log event for each
timing marker. Configure Application Insights on the App Service so stdout logs
are retained and searchable. Do not use the in-memory `/performance-metrics`
endpoint as durable storage; its values are reset when the worker restarts and
are not shared between scaled-out instances.

### Azure setup

1. Create or open an Application Insights resource.
2. In the App Service, open **Application Insights**, choose that resource,
	and enable it.
3. In **Configuration**, add `APPLICATIONINSIGHTS_CONNECTION_STRING` using the
	connection string from Application Insights, then restart the App Service.
	The frontend uses this value for browser page views, sessions, and users. If
	the browser should use a different Application Insights resource, set
	`APPLICATIONINSIGHTS_BROWSER_CONNECTION_STRING` instead.
4. Keep application logs enabled if you also need App Service **Log stream**.
	Application Insights is the durable store for querying and alerting.

In **Logs**, query the events with:

```kusto
traces
| extend event = parse_json(message)
| where tostring(event.event_type) == "performance_metric"
| extend metric_name = tostring(event.metric_name)
| extend duration_ms = todouble(event.duration_ms)
| summarize
	 count = count(),
	 avg_ms = avg(duration_ms),
	 p95_ms = percentile(duration_ms, 95),
	 max_ms = max(duration_ms)
  by metric_name, bin(timestamp, 5m)
| order by timestamp desc
```

Create an Azure Monitor alert from this query for a high `p95_ms`, for example
when `token_api` or `visual_loaded` exceeds the agreed threshold.

### Secrets

Store `TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`, `PORTAL_PASSWORD`, and
`SESSION_SECRET` in App Service **Configuration > Application settings** or
Key Vault references. Do not deploy `.env` to Azure or commit it to Git. If a
secret has been exposed, rotate it immediately.
