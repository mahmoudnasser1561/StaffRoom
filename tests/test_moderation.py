import hashlib
import hmac
import json
import time
import unittest

from App import create_app, db, mail
from App.models import Comment, ModerationFlag, Post, Role, User


class ModerationApiTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app.config['MODERATION_SERVICE_TOKEN'] = 'test-secret-token'
        self.app.config['FLASKY_ADMIN'] = 'admin@example.com'
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        Role.insert_roles()
        self.client = self.app.test_client(use_cookies=True)

        role = Role.query.filter_by(name='User').first()
        self.author = User(email='author@example.com', username='author',
                           password='cat', confirmed=True, role=role)
        db.session.add(self.author)
        db.session.commit()

        self.post = Post(body='some post body', author=self.author)
        db.session.add(self.post)
        db.session.commit()
        self.post_hash = hashlib.sha256(self.post.body.encode('utf-8')).hexdigest()

        self.comment = Comment(body='some comment body', post=self.post,
                               author=self.author)
        db.session.add(self.comment)
        db.session.commit()
        self.comment_hash = hashlib.sha256(self.comment.body.encode('utf-8')).hexdigest()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def signed_post(self, url, payload, secret='test-secret-token', timestamp=None):
        body = json.dumps(payload).encode('utf-8')
        ts = timestamp if timestamp is not None else str(int(time.time()))
        message = ts.encode('utf-8') + b'.' + body
        signature = hmac.new(secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
        headers = {'X-Moderation-Timestamp': ts, 'X-Moderation-Signature': signature}
        return self.client.post(url, data=body, headers=headers,
                                content_type='application/json')

    # ---- verify ----

    def test_verify_not_found(self):
        r = self.signed_post('/api/v1/moderation/posts/999999/verify',
                             {'content_hash': self.post_hash})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()['status'], 'not_found')

    def test_verify_stale(self):
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/verify',
                             {'content_hash': 'wronghash'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'stale')

    def test_verify_ok(self):
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/verify',
                             {'content_hash': self.post_hash})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'ok')

    def test_verify_comment_route(self):
        r = self.signed_post(f'/api/v1/moderation/comments/{self.comment.id}/verify',
                             {'content_hash': self.comment_hash})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'ok')

    def test_verify_wrong_secret(self):
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/verify',
                             {'content_hash': self.post_hash}, secret='wrong')
        self.assertEqual(r.status_code, 401)

    def test_verify_stale_timestamp_rejected(self):
        old_timestamp = str(int(time.time()) - 120)
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/verify',
                             {'content_hash': self.post_hash}, timestamp=old_timestamp)
        self.assertEqual(r.status_code, 401)

    def test_verify_tampered_body_rejected(self):
        body = json.dumps({'content_hash': self.post_hash}).encode('utf-8')
        ts = str(int(time.time()))
        message = ts.encode('utf-8') + b'.' + body
        signature = hmac.new(b'test-secret-token', message, hashlib.sha256).hexdigest()
        tampered_body = json.dumps({'content_hash': 'wronghash'}).encode('utf-8')
        r = self.client.post(f'/api/v1/moderation/posts/{self.post.id}/verify',
                             data=tampered_body,
                             headers={'X-Moderation-Timestamp': ts,
                                     'X-Moderation-Signature': signature},
                             content_type='application/json')
        self.assertEqual(r.status_code, 401)

    # ---- disable: auth ----

    def test_disable_missing_signature(self):
        r = self.client.post(f'/api/v1/moderation/posts/{self.post.id}/disable',
                             json={'reason': 'bad', 'content_hash': self.post_hash})
        self.assertEqual(r.status_code, 401)

    def test_disable_wrong_secret(self):
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/disable',
                             {'reason': 'bad', 'content_hash': self.post_hash}, secret='wrong')
        self.assertEqual(r.status_code, 401)

    # ---- disable: validation ----

    def test_disable_not_found(self):
        r = self.signed_post('/api/v1/moderation/posts/999999/disable',
                             {'reason': 'bad', 'content_hash': self.post_hash})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()['status'], 'not_found')

    def test_disable_stale_skipped(self):
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/disable',
                             {'reason': 'bad', 'content_hash': 'wronghash'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'stale_skipped')
        db.session.refresh(self.post)
        self.assertFalse(bool(self.post.disabled))
        self.assertEqual(ModerationFlag.query.count(), 0)

    def test_disable_empty_reason_rejected(self):
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/disable',
                             {'reason': '   ', 'content_hash': self.post_hash})
        self.assertEqual(r.status_code, 400)
        db.session.refresh(self.post)
        self.assertFalse(bool(self.post.disabled))

    def test_disable_already_disabled_is_idempotent(self):
        self.post.disabled = True
        db.session.add(self.post)
        db.session.commit()

        with mail.record_messages() as outbox:
            r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/disable',
                                 {'reason': 'again', 'content_hash': self.post_hash})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.get_json()['status'], 'already_disabled')

        self.assertEqual(len(outbox), 0)
        self.assertEqual(
            ModerationFlag.query.filter_by(post_id=self.post.id, comment_id=None).count(), 0)

    # ---- disable: success path (full pipeline) ----

    def test_disable_post_success_creates_flag(self):
        r = self.signed_post(f'/api/v1/moderation/posts/{self.post.id}/disable',
                             {'reason': 'Spam', 'content_hash': self.post_hash})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['status'], 'disabled')

        db.session.refresh(self.post)
        self.assertTrue(self.post.disabled)

        flag = ModerationFlag.query.filter_by(post_id=self.post.id, comment_id=None).first()
        self.assertIsNotNone(flag)
        self.assertEqual(flag.source, 'ai')
        self.assertEqual(flag.reason, 'Spam')
        self.assertEqual(flag.user_id, self.author.id)

    def test_disable_comment_success_sets_both_post_and_comment_id(self):
        r = self.signed_post(f'/api/v1/moderation/comments/{self.comment.id}/disable',
                             {'reason': 'Harassment', 'content_hash': self.comment_hash})
        self.assertEqual(r.status_code, 200)

        db.session.refresh(self.comment)
        self.assertTrue(self.comment.disabled)

        flag = ModerationFlag.query.filter_by(comment_id=self.comment.id).first()
        self.assertIsNotNone(flag)
        self.assertEqual(flag.post_id, self.post.id)
        self.assertEqual(flag.source, 'ai')


class HumanModerationRouteTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        Role.insert_roles()
        self.client = self.app.test_client(use_cookies=True)

        user_role = Role.query.filter_by(name='User').first()
        mod_role = Role.query.filter_by(name='Moderator').first()

        self.author = User(email='author@example.com', username='author',
                           password='cat', confirmed=True, role=user_role)
        self.moderator = User(email='mod@example.com', username='moduser',
                              password='dog', confirmed=True, role=mod_role)
        db.session.add_all([self.author, self.moderator])
        db.session.commit()

        self.post = Post(body='a post', author=self.author)
        db.session.add(self.post)
        db.session.commit()

        self.comment1 = Comment(body='comment one', post=self.post, author=self.author)
        self.comment2 = Comment(body='comment two', post=self.post, author=self.author)
        db.session.add_all([self.comment1, self.comment2])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def login_moderator(self):
        return self.client.post('/auth/login', data={
            'email': 'mod@example.com', 'password': 'dog'
        }, follow_redirects=True)

    def test_moderate_post_disable_and_enable(self):
        self.login_moderator()

        r = self.client.get(f'/moderate/post/disable/{self.post.id}')
        self.assertEqual(r.status_code, 302)
        db.session.refresh(self.post)
        self.assertTrue(self.post.disabled)

        flag = ModerationFlag.query.filter_by(post_id=self.post.id, comment_id=None).first()
        self.assertIsNotNone(flag)
        self.assertEqual(flag.source, 'admin')
        self.assertIsNone(flag.reason)
        self.assertEqual(flag.moderator_id, self.moderator.id)
        self.assertIsNone(flag.overturned_at)

        r = self.client.get(f'/moderate/post/enable/{self.post.id}')
        self.assertEqual(r.status_code, 302)
        db.session.refresh(self.post)
        self.assertFalse(self.post.disabled)

        db.session.refresh(flag)
        self.assertIsNotNone(flag.overturned_at)
        self.assertEqual(flag.overturned_by_id, self.moderator.id)

    def test_moderate_disable_and_enable_comment(self):
        self.login_moderator()

        r = self.client.get(f'/moderate/disable/{self.comment1.id}')
        self.assertEqual(r.status_code, 302)
        db.session.refresh(self.comment1)
        self.assertTrue(self.comment1.disabled)

        flag = ModerationFlag.query.filter_by(comment_id=self.comment1.id).first()
        self.assertIsNotNone(flag)
        self.assertEqual(flag.post_id, self.post.id)

        r = self.client.get(f'/moderate/enable/{self.comment1.id}')
        self.assertEqual(r.status_code, 302)
        db.session.refresh(self.comment1)
        self.assertFalse(self.comment1.disabled)
        db.session.refresh(flag)
        self.assertIsNotNone(flag.overturned_at)

    def test_disabling_comment_and_post_independently_no_cross_contamination(self):
        self.login_moderator()

        self.client.get(f'/moderate/disable/{self.comment1.id}')
        self.client.get(f'/moderate/post/disable/{self.post.id}')

        db.session.refresh(self.comment2)
        self.assertFalse(bool(self.comment2.disabled))

        # Enabling the post must not overturn comment1's flag, and must not
        # touch comment1's own disabled state — this is exactly the bug the
        # explicit comment_id=None filter (Task 1.4) exists to prevent.
        self.client.get(f'/moderate/post/enable/{self.post.id}')

        comment_flag = ModerationFlag.query.filter_by(comment_id=self.comment1.id).first()
        post_flag = ModerationFlag.query.filter_by(post_id=self.post.id, comment_id=None).first()

        self.assertIsNone(comment_flag.overturned_at)
        self.assertIsNotNone(post_flag.overturned_at)

        db.session.refresh(self.comment1)
        self.assertTrue(self.comment1.disabled)
