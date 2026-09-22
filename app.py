import os
import threading
import time
import hmac
from functools import wraps
from flask import Flask, render_template, request, jsonify, session
import requests
from dotenv import load_dotenv

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


def record_performance_metric(name, duration_ms):
    """Lưu timing gần nhất cho từng mốc và ghi ra log server."""
    metric = {
        "duration_ms": round(float(duration_ms), 2),
        "recorded_at": time.time()
    }
    with performance_metrics_lock:
        performance_metrics[name] = metric
    app.logger.info("performance.%s duration_ms=%.2f", name, metric["duration_ms"])

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
    response = requests.post(url, data=payload, timeout=15)
    response.raise_for_status()
    token_result = response.json()
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
    response = requests.post(url, headers=headers, json=body, timeout=15)
    response.raise_for_status()
    token_result = response.json()
    
    # Keep a clear application error when Power BI returns no embed token.
    if "token" not in token_result:
        app.logger.error("Power BI API did not return an embed token: %s", token_result)
        record_performance_metric(
            "embed_token",
            (time.perf_counter() - started_at) * 1000
        )
        return None
    record_performance_metric(
        "embed_token",
        (time.perf_counter() - started_at) * 1000
    )
    return token_result["token"]

@app.route('/')
def index():
    report_id = os.getenv('REPORT_ID')
    workspace_id = os.getenv('WORKSPACE_ID')
    
    # The embed URL and report ID are public report metadata, not credentials.
    embed_url = f"https://app.powerbi.com/reportEmbed?reportId={report_id}&groupId={workspace_id}"

    return render_template('index.html', 
                           embed_url=embed_url, 
                           report_id=report_id,
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

    if name not in {'report_loaded', 'report_rendered'}:
        return jsonify({"error": "Unsupported performance metric"}), 400
    try:
        duration_ms = float(duration_ms)
    except (TypeError, ValueError):
        return jsonify({"error": "duration_ms must be a number"}), 400
    if duration_ms < 0:
        return jsonify({"error": "duration_ms must be non-negative"}), 400

    record_performance_metric(name, duration_ms)
    return jsonify({"ok": True})


@app.route('/performance-metrics', methods=['GET'])
@portal_login_required
def get_performance_metrics():
    with performance_metrics_lock:
        return jsonify(performance_metrics)

if __name__ == '__main__':
    debug = os.getenv('FLASK_DEBUG', '0').lower() in {'1', 'true', 'yes'}
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=debug)