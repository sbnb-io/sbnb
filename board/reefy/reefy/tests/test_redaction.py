"""Unit tests for recognizable secret redaction in device logs."""

import unittest

import _bootstrap  # noqa: F401
from reefy.redaction import REDACTED, redact_log_message


class RedactionTests(unittest.TestCase):
    def assert_redacted(self, message, secret):
        rendered = redact_log_message(message)
        self.assertIn(REDACTED, rendered)
        self.assertNotIn(secret, rendered)

    def test_sensitive_assignments_and_structured_values(self):
        cases = [
            ('password=synthetic-pass', 'synthetic-pass'),
            ('"access_token": "synthetic-token"', 'synthetic-token'),
            ("{'client_secret': 'synthetic-client'}", 'synthetic-client'),
            ('https://sample-user:synthetic-url-pass@example.invalid/path',
             'synthetic-url-pass'),
            ('https://:synthetic-empty-user-pass@example.invalid/path',
             'synthetic-empty-user-pass'),
            ('Authorization: Bearer synthetic-bearer', 'synthetic-bearer'),
            ('token=synthetic-query&mode=test', 'synthetic-query'),
            ('credentials=["synthetic-first", {"nested": "synthetic-second"}]',
             'synthetic-first'),
            ('credentials=["synthetic-first", {"nested": "synthetic-second"}]',
             'synthetic-second'),
            ('worker --token synthetic-cli-token --mode safe',
             'synthetic-cli-token'),
            ('worker --tokens ["synthetic-cli-one", '
             '{"nested": "synthetic-cli-two"}] --mode safe',
             'synthetic-cli-one'),
            ('worker --tokens ["synthetic-cli-one", '
             '{"nested": "synthetic-cli-two"}] --mode safe',
             'synthetic-cli-two'),
            ('AWS_SECRET_ACCESS_KEY=synthetic-aws-secret',
             'synthetic-aws-secret'),
            ('WIFI_PSK=synthetic-wifi-secret', 'synthetic-wifi-secret'),
            ('{"auth": "synthetic-docker-auth"}',
             'synthetic-docker-auth'),
            (r'payload="{\"client_secret\":\"synthetic-escaped\"}"',
             'synthetic-escaped'),
            ("credentials=('synthetic-tuple-one', 'synthetic-tuple-two')",
             'synthetic-tuple-one'),
            ("credentials=('synthetic-tuple-one', 'synthetic-tuple-two')",
             'synthetic-tuple-two'),
            ('https://download.invalid/file?X-Amz-Signature=synthetic-signed'
             '&AWSAccessKeyId=synthetic-access-id', 'synthetic-signed'),
            ('https://download.invalid/file?X-Amz-Signature=synthetic-signed'
             '&AWSAccessKeyId=synthetic-access-id', 'synthetic-access-id'),
        ]
        for message, secret in cases:
            with self.subTest(kind=message.split(':', 1)[0]):
                self.assert_redacted(message, secret)

    def test_sensitive_headers_are_redacted_as_a_whole(self):
        messages = [
            'Authorization: Digest response=synthetic-digest, '
            'nonce=synthetic-nonce',
            'Proxy-Authorization: Custom synthetic-proxy-value',
            'Cookie: sid=synthetic-cookie-one; refresh=synthetic-cookie-two',
            'Set-Cookie: sid=synthetic-set-cookie; HttpOnly',
        ]
        for message in messages:
            with self.subTest(header=message.split(':', 1)[0]):
                rendered = redact_log_message(message)
                self.assertEqual(rendered.count(REDACTED), 1)
                self.assertNotIn('synthetic-', rendered)

    def test_jwt_shaped_value_is_redacted(self):
        token = 'abcdefgh.ijklmnop.qrstuvwx'
        self.assert_redacted(f'received {token}', token)

    def test_complete_and_unterminated_private_keys_are_redacted(self):
        complete = (
            '-----BEGIN PRIVATE KEY-----\nsynthetic-key-body\n'
            '-----END PRIVATE KEY-----')
        unterminated = (
            '-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-open-key')
        self.assert_redacted(complete, 'synthetic-key-body')
        self.assert_redacted(unterminated, 'synthetic-open-key')

    def test_benign_diagnostics_are_unchanged(self):
        messages = [
            'token count=3; passwordless login is disabled; secretary ready',
            'basic storage health check passed',
        ]
        for message in messages:
            self.assertEqual(redact_log_message(message), message)

    def test_failing_string_conversion_returns_sanitized_marker(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError('synthetic-unprintable-secret')

        self.assertEqual(redact_log_message(Unprintable()), REDACTED)

    def test_structured_cli_value_preserves_following_flags(self):
        message = (
            'worker --tokens ["synthetic-cli-one", '
            '{"nested": "synthetic-cli-two"}] --mode safe')
        self.assertEqual(
            redact_log_message(message),
            'worker --tokens [REDACTED] --mode safe')

    def test_redaction_is_idempotent(self):
        messages = [
            'api_key=synthetic-key',
            'Authorization: Bearer synthetic-bearer-value',
            'credentials=["synthetic-first", "synthetic-second"]',
            'worker --token synthetic-cli-token',
            'worker --tokens ["synthetic-cli-one", "synthetic-cli-two"]',
            r'payload="{\"client_secret\":\"synthetic-escaped\"}"',
            "credentials=('synthetic-tuple-one', 'synthetic-tuple-two')",
            'https://:synthetic-pass@example.invalid/path',
        ]
        for message in messages:
            with self.subTest(message=message.split('=', 1)[0]):
                once = redact_log_message(message)
                self.assertEqual(redact_log_message(once), once)
