"""Local intercepted HTML only; never connects to a Google account or backend."""
import importlib
import importlib.util
import unittest
import shutil
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright


class GoogleBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.google_browser'),
                             'Google ordinary browser adapter is not implemented')
        self.m = importlib.import_module('onboarding.google_browser')
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True,
            executable_path=shutil.which('google-chrome') or shutil.which('chromium'), args=['--no-sandbox', '--disable-background-networking'])
        self.context = await self.browser.new_context(service_workers='block')
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        self.page.set_default_timeout(2000)
        self.events = []
        async def guard(action):
            self.events.append(('guard', action))
            return True
        async def intent(action):
            self.events.append(('intent', action))
            return True
        self.guard, self.intent = guard, intent

    async def asyncTearDown(self):
        if hasattr(self, 'browser'):
            await self.browser.close()
        if hasattr(self, 'p'):
            await self.p.stop()

    async def fixture(self, html, url='https://accounts.google.com/v3/signin/identifier'):
        await self.page.route(url, lambda route: route.fulfill(body=html, content_type='text/html'))
        await self.page.goto(url)
        return self.m.GoogleBrowserDriver(self.page, guard=self.guard, intent=self.intent,
                                           observation_timeout=.1)

    async def test_email_guard_intent_then_password_transition(self):
        driver = await self.fixture('''<input id="identifierId" type="email"><button id="identifierNext"
            onclick="document.body.innerHTML='<input name=Passwd type=password><button id=passwordNext>Next</button>'">Next</button>''')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test'))
        self.assertEqual(result.status, 'PASSWORD')
        self.assertEqual(self.events, [('guard','google.login.email.fill'),
            ('guard','google.login.email.submit'), ('intent','google.login.email.submit')])

    async def test_missing_secret_waits_without_actions(self):
        driver = await self.fixture('<input name="Passwd" type="password"><button id="passwordNext">Next</button>')
        self.assertEqual((await driver.advance(self.m.GoogleMaterial())).status, 'WAIT_INPUT')
        self.assertEqual(self.events, [])

    async def test_challenge_preserves_context_and_no_writes(self):
        driver = await self.fixture('<input id="identifierId"><iframe title="reCAPTCHA"></iframe>')
        self.assertEqual((await driver.advance(self.m.GoogleMaterial(email='x'))).status, 'WAIT_HUMAN')
        self.assertEqual(self.events, [])
        self.assertFalse(self.page.is_closed())

    async def test_untrusted_origin_never_fills(self):
        driver = await self.fixture('<input id="identifierId">', 'https://accounts.google.com.evil.invalid/')
        self.assertEqual((await driver.advance(self.m.GoogleMaterial(email='secret'))).status, 'BLOCKED')
        self.assertEqual(self.events, [])

    async def test_guard_denial_prevents_fill(self):
        driver = await self.fixture('<input id="identifierId"><button id="identifierNext">Next</button>')
        driver.guard = AsyncMock(side_effect=PermissionError('denied'))
        with self.assertRaises(PermissionError):
            await driver.advance(self.m.GoogleMaterial(email='secret'))
        self.assertEqual(await self.page.locator('#identifierId').input_value(), '')

    async def test_guard_denial_before_click_does_not_create_intent(self):
        driver = await self.fixture('<input id="identifierId"><button id="identifierNext">Next</button>')
        async def deny_submit(action):
            if action.endswith('submit'):
                raise PermissionError('denied')
            return True
        driver.guard = deny_submit
        with self.assertRaises(PermissionError):
            await driver.advance(self.m.GoogleMaterial(email='authorized@example.test'))
        self.assertEqual(self.events, [])

    async def test_intent_failure_prevents_click(self):
        driver = await self.fixture('<input id="identifierId"><button id="identifierNext" onclick="window.clicked=true">Next</button>')
        driver.intent = AsyncMock(side_effect=RuntimeError('persistence unavailable'))
        with self.assertRaises(RuntimeError):
            await driver.advance(self.m.GoogleMaterial(email='authorized@example.test'))
        self.assertIsNone(await self.page.evaluate('window.clicked'))

    async def test_unchanged_submit_unknown_and_no_retry(self):
        driver = await self.fixture('<input id="identifierId"><button id="identifierNext" onclick="window.clicks=(window.clicks||0)+1">Next</button>')
        material = self.m.GoogleMaterial(email='authorized@example.test')
        self.assertEqual((await driver.advance(material)).status, 'UNKNOWN')
        self.assertEqual((await driver.advance(material)).status, 'UNKNOWN')
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_pause_resume_does_not_close_or_create_page(self):
        driver = await self.fixture('<input id="identifierId">')
        driver.pause()
        self.assertEqual((await driver.advance(self.m.GoogleMaterial(email='x'))).status, 'PAUSED')
        self.assertEqual(self.events, [])
        self.assertEqual((await driver.resume()).status, 'EMAIL')
        self.assertEqual(len(self.context.pages), 1)

    async def test_contacts_fill_only_and_missing_data_is_not_fabricated(self):
        driver = await self.fixture('<input autocomplete="name"><input autocomplete="address-line1"><button onclick="window.paid=true">Start free</button>',
                                    'https://console.cloud.google.com/freetrial')
        incomplete = self.m.GoogleMaterial(full_name='Authorized Person')
        self.assertEqual((await driver.advance(incomplete)).status, 'WAIT_INPUT')
        self.assertEqual(self.events, [])
        complete = self.m.GoogleMaterial(full_name='Authorized Person', address_line1='Real authorized address')
        self.assertEqual((await driver.advance(complete)).status, 'FILLED')
        self.assertEqual(await self.page.locator('[autocomplete=name]').input_value(), 'Authorized Person')
        self.assertIsNone(await self.page.evaluate('window.paid'))
        self.assertTrue(all(kind == 'guard' for kind, _ in self.events))

    async def test_callbacks_required_and_material_repr_is_redacted(self):
        with self.assertRaises(TypeError):
            self.m.GoogleBrowserDriver(self.page)
        material = self.m.GoogleMaterial(email='hidden-email', password='hidden-password', full_name='hidden-name')
        for secret in ('hidden-email', 'hidden-password', 'hidden-name'):
            self.assertNotIn(secret, repr(material))

    async def test_password_challenge_path_is_not_unknown_challenge(self):
        driver = await self.fixture('<input name="Passwd" type="password"><button id="passwordNext">Next</button>',
                                    'https://accounts.google.com/v3/signin/challenge/pwd')
        self.assertEqual((await driver.observe()).status, 'PASSWORD')

    async def test_none_or_false_callbacks_fail_closed(self):
        driver = await self.fixture('<input id="identifierId"><button id="identifierNext" onclick="window.clicked=true">Next</button>')
        for value in (None, False):
            driver.guard = AsyncMock(return_value=value)
            with self.assertRaises(PermissionError):
                await driver.advance(self.m.GoogleMaterial(email='secret'))
            self.assertEqual(await self.page.locator('#identifierId').input_value(), '')
        driver.guard = self.guard
        driver.intent = AsyncMock(return_value=None)
        with self.assertRaises(PermissionError):
            await driver.advance(self.m.GoogleMaterial(email='authorized@example.test'))
        self.assertIsNone(await self.page.evaluate('window.clicked'))

    async def test_guard_navigation_prevents_secret_fill(self):
        driver = await self.fixture('<input id="identifierId"><button id="identifierNext">Next</button>')
        async def navigate_elsewhere(action):
            await self.page.route('https://untrusted.invalid/', lambda route: route.fulfill(body='<input id="identifierId">', content_type='text/html'))
            await self.page.goto('https://untrusted.invalid/')
            return True
        driver.guard = navigate_elsewhere
        with self.assertRaises(PermissionError):
            await driver.advance(self.m.GoogleMaterial(email='secret'))
        self.assertEqual(await self.page.locator('#identifierId').input_value(), '')

    async def test_click_exception_is_unknown_and_redacted_without_retry(self):
        driver = await self.fixture('<input id="identifierId"><button id="identifierNext">Next</button>')
        original = driver._visible
        bad_button = type('Button', (), {'click': AsyncMock(side_effect=TimeoutError('secret-value'))})()
        async def control(selector):
            if selector == '#identifierNext':
                return bad_button
            return await original(selector)
        driver._visible = control
        result = await driver.advance(self.m.GoogleMaterial(email='secret-value'))
        self.assertEqual(result.status, 'UNKNOWN')
        self.assertNotIn('secret-value', repr(result))
        self.assertEqual((await driver.resume()).status, 'UNKNOWN')
        self.assertEqual((await driver.advance(self.m.GoogleMaterial(email='x'))).status, 'UNKNOWN')
        bad_button.click.assert_awaited_once()

    async def test_each_contact_field_requires_explicit_guard(self):
        driver = await self.fixture('<input autocomplete="name"><input autocomplete="address-line1">',
                                    'https://console.cloud.google.com/freetrial')
        async def first_only(action):
            self.events.append(action)
            return action == 'google.contact.full_name.fill'
        driver.guard = first_only
        with self.assertRaises(PermissionError):
            await driver.advance(self.m.GoogleMaterial(full_name='Authorized', address_line1='Do not fill'))
        self.assertEqual(await self.page.locator('[autocomplete="address-line1"]').input_value(), '')
        self.assertEqual(self.events, ['google.contact.full_name.fill', 'google.contact.address_line1.fill'])

    async def test_contact_select_uses_authorized_label_only(self):
        driver = await self.fixture('<select autocomplete="country"><option>Select</option><option value="NZ">New Zealand</option></select>',
                                    'https://console.cloud.google.com/freetrial')
        result = await driver.advance(self.m.GoogleMaterial(country='New Zealand'))
        self.assertEqual(result.status, 'FILLED')
        self.assertEqual(await self.page.locator('select').input_value(), 'NZ')
        self.assertEqual(self.events, [('guard', 'google.contact.country.fill')])

    async def test_unrecognized_after_submit_is_unknown_not_successful_progress(self):
        driver = await self.fixture('''<input id="identifierId"><button id="identifierNext"
            onclick="document.body.innerHTML='<p>Unrecognized result</p>'">Next</button>''')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test'))
        self.assertEqual(result.status, 'UNKNOWN')
        self.assertEqual((await driver.resume()).status, 'UNKNOWN')

    async def password_fixture(self, identity):
        return await self.fixture(identity + '''<input name="Passwd" type="password">
            <button id="passwordNext" onclick="window.clicks=(window.clicks||0)+1;
            document.body.innerHTML='<input autocomplete=one-time-code>'">Next</button>''',
            'https://accounts.google.com/v3/signin/challenge/pwd')

    async def test_password_requires_exact_visible_target_account(self):
        driver = await self.password_fixture('<div id="profileIdentifier">authorized@example.test</div>')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(result.reason, 'VERIFICATION_REQUIRED')
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)
        self.assertEqual(self.events, [('guard', 'google.login.password.fill'),
            ('guard', 'google.login.password.submit'), ('intent', 'google.login.password.submit')])

    async def test_password_wrong_account_does_not_fill_or_consume(self):
        driver = await self.password_fixture('<div id="profileIdentifier">other@example.test</div>')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(result.reason, 'ACCOUNT_IDENTITY_UNCONFIRMED')
        self.assertEqual(await self.page.locator('input[name=Passwd]').input_value(), '')
        self.assertEqual(self.events, [])
        self.assertIsNone(await self.page.evaluate('window.clicks'))
        for secret in ('other@example.test', 'authorized@example.test', 'private-password'):
            self.assertNotIn(secret, repr(result))

    async def test_password_missing_identity_waits_without_writes(self):
        driver = await self.password_fixture('')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(self.events, [])
        self.assertEqual(await self.page.locator('input[name=Passwd]').input_value(), '')

    async def test_password_requires_target_email_material(self):
        driver = await self.password_fixture('<div id="profileIdentifier">authorized@example.test</div>')
        result = await driver.advance(self.m.GoogleMaterial(password='private-password'))
        self.assertEqual(result.status, 'WAIT_INPUT')
        self.assertEqual(result.missing_fields, ('email',))
        self.assertEqual(self.events, [])
        self.assertEqual(await self.page.locator('input[name=Passwd]').input_value(), '')

    async def test_password_conflicting_visible_account_markers_fail_closed(self):
        driver = await self.password_fixture('''<div id="profileIdentifier">authorized@example.test</div>
            <div data-email="other@example.test" aria-selected="true">Other</div>''')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(self.events, [])
        self.assertEqual(await self.page.locator('input[name=Passwd]').input_value(), '')

    async def test_password_does_not_treat_unselected_account_list_as_current(self):
        driver = await self.password_fixture('<div data-email="authorized@example.test">Authorized</div>')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(self.events, [])

    async def test_password_accepts_explicit_selected_data_email(self):
        driver = await self.password_fixture('<div data-email="authorized@example.test" aria-selected="true">Authorized</div>')
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.reason, 'VERIFICATION_REQUIRED')
        self.assertEqual(await self.page.evaluate('window.clicks'), 1)

    async def test_password_guard_account_switch_prevents_fill(self):
        driver = await self.password_fixture('<div id="profileIdentifier">authorized@example.test</div>')
        async def switch(action):
            await self.page.locator('#profileIdentifier').evaluate('(el) => el.textContent="other@example.test"')
            return True
        driver.guard = switch
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(await self.page.locator('input[name=Passwd]').input_value(), '')
        self.assertEqual(self.events, [])
        self.assertIsNone(await self.page.evaluate('window.clicks'))

    async def test_password_submit_guard_account_switch_prevents_intent(self):
        driver = await self.password_fixture('<div id="profileIdentifier">authorized@example.test</div>')
        async def switch(action):
            if action.endswith('submit'):
                await self.page.locator('#profileIdentifier').evaluate('(el) => el.textContent="other@example.test"')
            return True
        driver.guard = switch
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(self.events, [])
        self.assertIsNone(await self.page.evaluate('window.clicks'))

    async def test_password_intent_account_switch_is_unknown_and_cannot_retry(self):
        driver = await self.password_fixture('<div id="profileIdentifier">authorized@example.test</div>')
        async def switch(action):
            self.events.append(('intent', action))
            await self.page.locator('#profileIdentifier').evaluate('(el) => el.textContent="other@example.test"')
            return True
        driver.intent = switch
        material = self.m.GoogleMaterial(email='authorized@example.test', password='private-password')
        self.assertEqual((await driver.advance(material)).status, 'UNKNOWN')
        self.assertIsNone(await self.page.evaluate('window.clicks'))
        self.assertEqual((await driver.resume()).status, 'UNKNOWN')
        self.assertEqual((await driver.advance(material)).status, 'UNKNOWN')
        self.assertEqual(sum(kind == 'intent' for kind, _ in self.events), 1)

    async def test_password_resume_rechecks_account_before_any_write(self):
        driver = await self.password_fixture('<div id="profileIdentifier">authorized@example.test</div>')
        self.assertEqual((await driver.observe()).status, 'PASSWORD')
        driver.pause()
        await self.page.locator('#profileIdentifier').evaluate('(el) => el.textContent="other@example.test"')
        await driver.resume()  # Read-only page observation does not authorize typing.
        result = await driver.advance(self.m.GoogleMaterial(email='authorized@example.test', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(self.events, [])
        self.assertEqual(await self.page.locator('input[name=Passwd]').input_value(), '')

    async def test_password_does_not_infer_gmail_alias_equivalence(self):
        driver = await self.password_fixture('<div id="profileIdentifier">authorized@gmail.com</div>')
        result = await driver.advance(self.m.GoogleMaterial(email='author.ized+tag@gmail.com', password='private-password'))
        self.assertEqual(result.status, 'WAIT_HUMAN')
        self.assertEqual(self.events, [])
