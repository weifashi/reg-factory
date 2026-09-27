"""Static delivery contract; real browser interactions are separately recorded."""
from pathlib import Path
import unittest

STATIC = Path(__file__).resolve().parents[1] / 'webui' / 'static'

class PageTests(unittest.TestCase):
    def test_login_and_protected_pages_are_separate(self):
        for name in ('onboarding-login.html', 'onboarding-login.js', 'onboarding.html',
                     'onboarding.js', 'onboarding.css'):
            self.assertTrue((STATIC / name).is_file(), name)

    def test_native_form_fallback_never_puts_credentials_in_url(self):
        html = (STATIC / 'onboarding-login.html').read_text()
        self.assertIn('method="post"', html)
        self.assertIn('action="/api/auth/login"', html)

    def test_login_uses_only_local_password_post_no_browser_storage(self):
        script = (STATIC / 'onboarding-login.js').read_text()
        for forbidden in ('localStorage', 'sessionStorage', 'document.cookie', 'innerHTML'):
            self.assertNotIn(forbidden, script)
        self.assertIn('/api/auth/bootstrap', script)
        self.assertIn('/api/auth/login', script)
        self.assertIn("password.value = ''", script)

    def test_control_page_has_only_synthetic_actions_and_explicit_secret_actions(self):
        html = (STATIC / 'onboarding.html').read_text()
        script = (STATIC / 'onboarding.js').read_text()
        self.assertIn('真实流程未接入', html)
        for action in ('pause', 'cancel', 'recheck'):
            self.assertIn(action, script)
        for action in ('keep', 'replace', 'clear'):
            self.assertIn(action, script)
        for forbidden in ('localStorage', 'sessionStorage', 'document.cookie', 'innerHTML',
                          '/api/run', '/api/sms', '/api/assets'):
            self.assertNotIn(forbidden, script)
        self.assertIn('crypto.randomUUID()', script)
        self.assertIn('expected_version', script)
        self.assertIn('COMMIT_UNKNOWN', script)
