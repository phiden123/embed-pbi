import os
import threading
import time
import hmac
import json
import logging
import sys
from functools import wraps
from flask import Flask, render_template, request, jsonify, session
from flask.logging import default_handler
import requests
from dotenv import load_dotenv
from opentelemetry._logs import set_logger_provider
from opentelemetry import metrics
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from azure.monitor.opentelemetry.exporter import (
    AzureMonitorLogExporter,
    AzureMonitorMetricExporter,
)

# Load local configuration without exposing secrets to the browser.
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SESSION_SECRET') or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', '0').lower() in {'1', 'true', 'yes'},
)

performance_metrics = {}
performance_metrics_lock = threading.Lock()


class AzureMonitorJsonFormatter(logging.Formatter):
    """Emit one structured performance event per log line for App Service."""
    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for field in (
            "event_type",
            "metric_name",
            "duration_ms",
            "recorded_at",
            "username",
            "session_id",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_logging():
    """Write structured logs to stdout and directly to Application Insights."""
    app.logger.removeHandler(default_handler)
    if not any(getattr(handler, "name", None) == "azure_app_service" for handler in app.logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.name = "azure_app_service"
        handler.setFormatter(AzureMonitorJsonFormatter())
        app.logger.addHandler(handler)

    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if connection_string and not any(
        getattr(handler, "name", None) == "azure_monitor_logs"
        for handler in app.logger.handlers
    ):
        logger_provider = LoggerProvider()
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                AzureMonitorLogExporter(connection_string=connection_string)
            )
        )
        set_logger_provider(logger_provider)
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        handler.name = "azure_monitor_logs"
        app.logger.addHandler(handler)

    app.logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


configure_logging()


def configure_metrics():
    """Export duration measurements as Azure Application Insights metrics."""
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not connection_string:
        app.logger.warning("APPLICATIONINSIGHTS_CONNECTION_STRING is not configured")
        return None

    exporter = AzureMonitorMetricExporter(connection_string=connection_string)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=5000)
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    return metrics.get_meter("embed-pbi.performance")


performance_meter = configure_metrics()
performance_duration = (
    performance_meter.create_histogram(
        name="performance.duration",
        unit="ms",
        description="Duration of application and Power BI embed performance events",
    )
    if performance_meter
    else None
)


def allowed_users():
    """Return the configured RLS usernames in a normalized form."""
    return {
        value.strip().casefold()
        for value in os.getenv('ALLOWED_USERS', '').split(',')
        if value.strip()
    }


def portal_login_required(view):
    """Protect portal APIs after the temporary portal password is entered."""
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get('portal_authenticated'):
            return jsonify({'error': 'portal login required'}), 401
        return view(*args, **kwargs)
    return wrapped_view


def record_performance_metric(name, duration_ms, context=None):
    """Keep a local latest value and emit a durable Azure Monitor event."""
    metric = {
        "duration_ms": round(float(duration_ms), 2),
        "recorded_at": time.time()
    }
    with performance_metrics_lock:
        performance_metrics[name] = metric
    app.logger.info(
        "performance_metric",
        extra={
            "event_type": "performance_metric",
            "metric_name": name,
            "duration_ms": metric["duration_ms"],
            "recorded_at": metric["recorded_at"],
            **(context or {}),
        },
    )
    if performance_duration:
        performance_duration.record(
            metric["duration_ms"],
            attributes={"metric_name": name},
        )

def get_access_token():
    """Request a short-lived Power BI API token for the service principal."""
    started_at = time.perf_counter()
    url = f"https://login.microsoftonline.com/{os.getenv('TENANT_ID')}/oauth2/v2.0/token"
    payload = {
        "client_id": os.getenv('CLIENT_ID'),
        "client_secret": os.getenv('CLIENT_SECRET'),
        "scope": "https://analysis.windows.net/powerbi/api/.default",
        "grant_type": "client_credentials"
    }
    try:
        response = requests.post(url, data=payload, timeout=15)
        response.raise_for_status()
        token_result = response.json()
    finally:
        record_performance_metric(
            "access_token",
            (time.perf_counter() - started_at) * 1000
        )
    access_token = token_result.get("access_token")
    if not access_token:
        raise RuntimeError("Microsoft identity platform did not return an access token")
    return access_token

def generate_embed_token(username):
    """Create a report embed token with the selected user's RLS identity."""
    started_at = time.perf_counter()

    access_token = get_access_token()
        
    workspace_id = os.getenv('WORKSPACE_ID')
    report_id = os.getenv('REPORT_ID')
    dataset_id = os.getenv('DATASET_ID')

    # The service-principal token is used only on the server for this request.
    url = "https://api.powerbi.com/v1.0/myorg/GenerateToken"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {
        "datasets": [{"id": dataset_id}],
        "reports": [{"id": report_id}],
        "targetWorkspaces": [{"id": workspace_id}],
        "identities": [
            {
                "username": username, 
                "roles": ["uat"], 
                "datasets": [dataset_id]
            }
        ]
    }
    try:
        response = requests.post(url, headers=headers, json=body, timeout=15)
        response.raise_for_status()
        token_result = response.json()

        # Keep a clear application error when Power BI returns no embed token.
        if "token" not in token_result:
            app.logger.error("Power BI API did not return an embed token: %s", token_result)
            return None
        return token_result["token"]
    finally:
        record_performance_metric(
            "embed_token",
            (time.perf_counter() - started_at) * 1000
        )

@app.route('/')
def index():
    report_id = os.getenv('REPORT_ID')
    workspace_id = os.getenv('WORKSPACE_ID')
    
    # The embed URL and report ID are public report metadata, not credentials.
    embed_url = f"https://app.powerbi.com/reportEmbed?reportId={report_id}&groupId={workspace_id}"

    return render_template('index.html', 
                           embed_url=embed_url, 
                           report_id=report_id,
                           applicationinsights_connection_string=os.getenv(
                               'APPLICATIONINSIGHTS_BROWSER_CONNECTION_STRING',
                               os.getenv('APPLICATIONINSIGHTS_CONNECTION_STRING', '')
                           ),
                           portal_authenticated=session.get('portal_authenticated', False))


@app.route('/portal-login', methods=['POST'])
def portal_login():
    data = request.get_json() or {}
    password = str(data.get('password', ''))
    expected_password = os.getenv('PORTAL_PASSWORD', '')
    if not expected_password:
        app.logger.error('PORTAL_PASSWORD is not configured')
        return jsonify({'error': 'portal password is not configured'}), 503
    if not hmac.compare_digest(password, expected_password):
        return jsonify({'error': 'incorrect password'}), 401

    session.clear()
    session['portal_authenticated'] = True
    app.logger.info(
        "portal_login_succeeded",
        extra={"event_type": "portal_login_succeeded"},
    )
    return jsonify({'ok': True})

@app.route('/get-embed-token', methods=['POST'])
@portal_login_required
def get_embed_token_api():
    # The browser sends only the selected username; secrets stay server-side.
    data = request.get_json() or {}
    username = str(data.get('username', '')).strip()
    if not username:
        return jsonify({"error": "username is required"}), 400
    if username.casefold() not in allowed_users():
        return jsonify({"error": "username is not allowed"}), 403

    app.logger.info(
        "embed_token_requested",
        extra={"event_type": "embed_token_requested", "username": username},
    )

    try:
        embed_token = generate_embed_token(username)
    except (requests.RequestException, KeyError, RuntimeError) as error:
        app.logger.exception("Could not generate Power BI embed token")
        return jsonify({"error": str(error)}), 502
    if not embed_token:
        return jsonify({"error": "Power BI did not return an embed token"}), 502
    with performance_metrics_lock:
        embed_token_timing = performance_metrics.get("embed_token")
    return jsonify({"token": embed_token, "performance": {
        "embed_token": embed_token_timing
    }})


@app.route('/performance-metrics', methods=['POST'])
@portal_login_required
def record_frontend_performance():
    data = request.get_json() or {}
    name = data.get('name')
    duration_ms = data.get('duration_ms')

    if name not in {
        'page_init',
        'token_api',
        'report_loaded',
        'report_rendered',
        'visual_loaded',
    }:
        return jsonify({"error": "Unsupported performance metric"}), 400
    try:
        duration_ms = float(duration_ms)
    except (TypeError, ValueError):
        return jsonify({"error": "duration_ms must be a number"}), 400
    if duration_ms < 0:
        return jsonify({"error": "duration_ms must be non-negative"}), 400

    context = {
        key: str(data[key]).strip()
        for key in ("session_id", "username")
        if data.get(key)
    }
    record_performance_metric(name, duration_ms, context=context)
    return jsonify({"ok": True})


@app.route('/performance-metrics', methods=['GET'])
@portal_login_required
def get_performance_metrics():
    with performance_metrics_lock:
        return jsonify(performance_metrics)

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0').lower() in {'1', 'true', 'yes'}
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=debug)