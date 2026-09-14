import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, patch

from App import create_app


class ChatTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()

    def tearDown(self):
        self.app_context.pop()

    def configure_service(self, url='http://chatbot-service', token='test-token'):
        self.app.config['CHATBOT_SERVICE_URL'] = url
        self.app.config['CHATBOT_SERVICE_TOKEN'] = token

    # ---- /chat/status ----

    def test_status_unavailable_when_not_configured(self):
        self.configure_service(url='', token='')
        r = self.client.get('/chat/status')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['available'])

    def test_status_unavailable_when_unreachable(self):
        self.configure_service(url='http://127.0.0.1:1')
        r = self.client.get('/chat/status')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['available'])

    def test_status_available_when_service_healthy(self):
        self.configure_service()
        mock_response = MagicMock(status_code=200)
        with patch('App.chat.views.requests.get', return_value=mock_response):
            r = self.client.get('/chat/status')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['available'])

    # ---- POST /chat: validation ----

    def test_empty_message_rejected(self):
        r = self.client.post('/chat', json={'message': '   '})
        self.assertEqual(r.status_code, 400)

    def test_missing_message_rejected(self):
        r = self.client.post('/chat', json={})
        self.assertEqual(r.status_code, 400)

    def test_validation_takes_precedence_over_unavailability(self):
        self.configure_service(url='', token='')
        r = self.client.post('/chat', json={'message': ''})
        self.assertEqual(r.status_code, 400)

    # ---- POST /chat: unavailable ----

    def test_send_message_unavailable_when_not_configured(self):
        self.configure_service(url='', token='')
        r = self.client.post('/chat', json={'message': 'What are the rules?'})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()['status'], 'error')

    def test_send_message_unavailable_when_unreachable(self):
        self.configure_service(url='http://127.0.0.1:1')
        r = self.client.post('/chat', json={'message': 'What are the rules?'})
        self.assertEqual(r.status_code, 503)

    def test_send_message_unavailable_when_service_returns_bad_response(self):
        self.configure_service()
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {}
        with patch('App.chat.views.requests.post', return_value=mock_response):
            r = self.client.post('/chat', json={'message': 'What are the rules?'})
        self.assertEqual(r.status_code, 503)

    def test_send_message_unavailable_when_service_errors(self):
        self.configure_service()
        mock_response = MagicMock(status_code=500)
        with patch('App.chat.views.requests.post', return_value=mock_response):
            r = self.client.post('/chat', json={'message': 'What are the rules?'})
        self.assertEqual(r.status_code, 503)

    # ---- POST /chat: success + sanitization ----

    def test_send_message_success(self):
        self.configure_service()
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {'reply': 'You can post markdown updates.'}
        with patch('App.chat.views.requests.post', return_value=mock_response):
            r = self.client.post('/chat', json={'message': 'What can I post?'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['reply'], 'You can post markdown updates.')

    def test_outbound_request_is_correctly_signed(self):
        self.configure_service()
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {'reply': 'ok'}

        with patch('App.chat.views.requests.post', return_value=mock_response) as mock_post:
            self.client.post('/chat', json={'message': 'hi'})

        sent_body = mock_post.call_args.kwargs['data']
        headers = mock_post.call_args.kwargs['headers']
        timestamp = headers['X-Chatbot-Timestamp']
        message = timestamp.encode('utf-8') + b'.' + sent_body
        expected = hmac.new(b'test-token', message, hashlib.sha256).hexdigest()
        self.assertEqual(headers['X-Chatbot-Signature'], expected)

    def test_message_and_history_are_capped_before_forwarding(self):
        self.configure_service()
        long_message = 'x' * 1000
        long_history = [{'role': 'user', 'text': 'y' * 1000} for _ in range(20)]

        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {'reply': 'ok'}

        with patch('App.chat.views.requests.post', return_value=mock_response) as mock_post:
            r = self.client.post('/chat', json={'message': long_message, 'history': long_history})

        self.assertEqual(r.status_code, 200)
        sent = json.loads(mock_post.call_args.kwargs['data'])
        self.assertEqual(len(sent['message']), 500)
        self.assertLessEqual(len(sent['history']), 6)
        for item in sent['history']:
            self.assertLessEqual(len(item['text']), 500)

    def test_malformed_history_items_are_dropped(self):
        self.configure_service()
        history = [
            'not a dict',
            {'role': 'system', 'text': 'wrong role'},
            {'role': 'user', 'text': ''},
            {'role': 'assistant', 'text': 'kept'},
        ]
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {'reply': 'ok'}

        with patch('App.chat.views.requests.post', return_value=mock_response) as mock_post:
            r = self.client.post('/chat', json={'message': 'hi', 'history': history})

        self.assertEqual(r.status_code, 200)
        sent = json.loads(mock_post.call_args.kwargs['data'])
        self.assertEqual(sent['history'], [{'role': 'assistant', 'text': 'kept'}])


if __name__ == '__main__':
    unittest.main()
