import hashlib
import hmac
import json
import time

import requests
from flask import current_app, jsonify, request
from . import chat
from .. import limiter

MAX_MESSAGE_LENGTH = 500
MAX_HISTORY_MESSAGES = 6
STATUS_TIMEOUT = (1, 2)
ANSWER_TIMEOUT = (2, 8)

def _service_configured():
    return bool(current_app.config['CHATBOT_SERVICE_URL']) and \
        bool(current_app.config['CHATBOT_SERVICE_TOKEN'])

def _service_url(path):
    return current_app.config['CHATBOT_SERVICE_URL'].rstrip('/') + path

def _signed_headers(body):
    secret = current_app.config['CHATBOT_SERVICE_TOKEN']
    timestamp = str(int(time.time()))
    message = timestamp.encode('utf-8') + b'.' + body
    signature = hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
    return {
        'Content-Type': 'application/json',
        'X-Chatbot-Timestamp': timestamp,
        'X-Chatbot-Signature': signature,
    }

def _unavailable():
    return jsonify(status='error',
                   message='The policy assistant is not available right now.'), 503

@chat.route('/chat/status')
def status():
    if not _service_configured():
        return jsonify(available=False)
    try:
        resp = requests.get(_service_url('/health'), timeout=STATUS_TIMEOUT)
    except requests.RequestException:
        return jsonify(available=False)
    return jsonify(available=resp.status_code == 200)

@chat.route('/chat', methods=['POST'])
@limiter.limit('20 per minute')
def send_message():
    body = request.get_json(silent=True) or {}
    message = (body.get('message') or '').strip()
    if not message:
        return jsonify(status='error', message='message is required'), 400
    message = message[:MAX_MESSAGE_LENGTH]

    if not _service_configured():
        return _unavailable()

    history = []
    for item in (body.get('history') or [])[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        text = (item.get('text') or '').strip()[:MAX_MESSAGE_LENGTH]
        if role in ('user', 'assistant') and text:
            history.append({'role': role, 'text': text})

    payload = json.dumps({'message': message, 'history': history}).encode('utf-8')
    try:
        resp = requests.post(_service_url('/answer'), data=payload,
                             headers=_signed_headers(payload), timeout=ANSWER_TIMEOUT)
    except requests.RequestException:
        return _unavailable()
    if resp.status_code != 200:
        return _unavailable()

    try:
        reply = (resp.json() or {}).get('reply')
    except ValueError:
        reply = None
    if not reply:
        return _unavailable()

    return jsonify(reply=reply)
