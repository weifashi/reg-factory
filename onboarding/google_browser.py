"""Conservative ordinary Google form driver; selectors are fixture-tested only.

This is not a verified production Google selector catalogue. No CAPTCHA solving,
OTP provisioning, Cloud resource operations, payment entry or trial activation is
implemented here. A worker supplies durable intent consumption and per-action
fencing callbacks; both must explicitly return True. The intent closure must bind
task, generation, configuration and the immutable target/material reference to
the durable effect_digest; action-name matching alone is not authorization. The
caller must supply material resolved from that same reference. Do not derive the
digest from passwords, OTPs or other low-entropy secrets. Browser login is separate
from gcloud authentication. UNKNOWN submissions must be reconciled by the worker,
never retried by constructing another driver around the same pending operation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GoogleMaterial:
    email: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    full_name: str = field(default='', repr=False)
    country: str = field(default='', repr=False)
    address_line1: str = field(default='', repr=False)
    city: str = field(default='', repr=False)
    region: str = field(default='', repr=False)
    postal_code: str = field(default='', repr=False)


@dataclass(frozen=True)
class BrowserObservation:
    status: str
    reason: str = ''
    missing_fields: tuple[str, ...] = ()


Callback = Callable[[str], Awaitable[bool]]
_CONTACT_FIELDS = {
    'full_name': 'input[autocomplete="name"]',
    'country': 'input[autocomplete="country-name"], select[autocomplete="country"], select[autocomplete="country-name"]',
    'address_line1': 'input[autocomplete="address-line1"], textarea[autocomplete="street-address"]',
    'city': 'input[autocomplete="address-level2"]',
    'region': 'input[autocomplete="address-level1"], select[autocomplete="address-level1"]',
    'postal_code': 'input[autocomplete="postal-code"]',
}
_LOGIN = {
    'EMAIL': ('email', 'input#identifierId', '#identifierNext'),
    'PASSWORD': ('password', 'input[name="Passwd"][type="password"]', '#passwordNext'),
}
# Fixture-validated current-account markers, not a verified live selector map.
# A generic [data-email] account-list entry is deliberately not authoritative.
_ACCOUNT_IDENTITY = ('#profileIdentifier, #profileIdentifier [data-email], '
                     '[data-email][aria-selected="true"], [data-email][aria-current="true"]')
_CHALLENGE = ('iframe[title*="captcha" i], iframe[src*="recaptcha" i], '
              'iframe[src*="hcaptcha" i], input[autocomplete="one-time-code"], '
              'input#idvPin, input[name="totpPin"], input[type="tel"]')


class GoogleBrowserDriver:
    """Single-page, single-worker driver. Pause retains the existing context.

    Callbacks see only fixed action identifiers, never field values. No implicit
    callback permits exist. Calling advance concurrently is prohibited, and each
    login stage may be submitted at most once per driver lifetime.
    """
    def __init__(self, page, *, guard: Callback, intent: Callback,
                 observation_timeout: float = 3.0):
        if not callable(guard) or not callable(intent):
            raise TypeError('guard and durable intent callbacks are required')
        if not 0 < observation_timeout <= 30:
            raise ValueError('invalid observation timeout')
        self.page = page
        self.guard = guard
        self.intent = intent
        self.observation_timeout = observation_timeout
        self._paused = False
        self._unknown = False
        self._submitted: set[str] = set()
        self._advancing = False

    async def _visible(self, selector):
        locator = self.page.locator(selector)
        # Multiple visible matches are not a uniquely identified form control.
        matches = []
        for index in range(await locator.count()):
            item = locator.nth(index)
            if await item.is_visible():
                matches.append(item)
        return matches[0] if len(matches) == 1 else None

    def _location(self):
        try:
            parsed = urlsplit(self.page.url)
            if (parsed.scheme != 'https' or parsed.username or parsed.password
                    or parsed.port not in (None, 443)
                    or parsed.hostname not in ('accounts.google.com', 'console.cloud.google.com')):
                return None
            return parsed.hostname, parsed.path
        except (ValueError, TypeError):
            return None

    async def observe(self) -> BrowserObservation:
        """Read-only observation; does not turn a pending intent into success."""
        if self.page.is_closed():
            return BrowserObservation('UNKNOWN', 'PAGE_CLOSED')
        location = self._location()
        if location is None:
            return BrowserObservation('BLOCKED', 'UNAPPROVED_ORIGIN')
        host, path = location
        # Any visible challenge is enough; never require a unique challenge.
        challenges = self.page.locator(_CHALLENGE)
        for i in range(await challenges.count()):
            if await challenges.nth(i).is_visible():
                return BrowserObservation('WAIT_HUMAN', 'VERIFICATION_REQUIRED')
        if host == 'accounts.google.com':
            if '/challenge/' in path and path.rstrip('/').split('/')[-1] != 'pwd':
                return BrowserObservation('WAIT_HUMAN', 'VERIFICATION_REQUIRED')
            for status, (_, selector, _) in _LOGIN.items():
                if await self._visible(selector) is not None:
                    return BrowserObservation(status)
        if host == 'console.cloud.google.com' and path.rstrip('/') in ('/freetrial', '/billing/signup'):
            if any([await self._visible(s) is not None for s in _CONTACT_FIELDS.values()]):
                return BrowserObservation('CONTACT')
        return BrowserObservation('WAIT_HUMAN', 'UNRECOGNIZED_PAGE')

    def pause(self) -> None:
        self._paused = True

    async def resume(self) -> BrowserObservation:
        self._paused = False
        if self._unknown:
            return BrowserObservation('UNKNOWN', 'RECONCILIATION_REQUIRED')
        return await self.observe()

    async def _permit(self, action: str, expected_status: str):
        if await self.guard(action) is not True:
            raise PermissionError('browser action guard denied')
        if self._paused:
            raise PermissionError('browser session paused')
        current = await self.observe()
        if current.status != expected_status:
            raise PermissionError('browser page changed before action')

    async def advance(self, material: GoogleMaterial) -> BrowserObservation:
        if self._advancing:
            raise RuntimeError('browser step already running')
        if self._unknown:
            return BrowserObservation('UNKNOWN', 'RECONCILIATION_REQUIRED')
        if self._paused:
            return BrowserObservation('PAUSED')
        self._advancing = True
        try:
            observed = await self.observe()
            if observed.status in _LOGIN:
                return await self._login(observed.status, material)
            if observed.status == 'CONTACT':
                return await self._contacts(material)
            return observed
        finally:
            self._advancing = False

    async def _password_account_matches(self, expected: str) -> bool:
        """Read current visible identity; never infer aliases or cache a match.

        Account values remain local to this comparison and never reach an
        observation or an exception. Explicit selected-account attributes and
        the visible profile identifier must agree if both supply an email.
        """
        if type(expected) is not str or not expected.strip():
            return False
        try:
            location = self._location()
            if location is None or location[0] != 'accounts.google.com':
                return False
            values = set()
            markers = self.page.locator(_ACCOUNT_IDENTITY)
            for index in range(await markers.count()):
                marker = markers.nth(index)
                if not await marker.is_visible():
                    continue
                attribute = await marker.get_attribute('data-email')
                if attribute is not None:
                    if not attribute.strip():
                        return False
                    values.add(attribute.strip())
                if await marker.get_attribute('id') == 'profileIdentifier':
                    text = (await marker.inner_text()).strip()
                    if attribute is None or '@' in text:
                        if not text:
                            return False
                        values.add(text)
            return values == {expected.strip()}
        except Exception:
            return False

    async def _login(self, status, material):
        if status in self._submitted:
            self._unknown = True
            return BrowserObservation('UNKNOWN', 'SUBMIT_ALREADY_ATTEMPTED')
        name, selector, next_selector = _LOGIN[status]
        value = getattr(material, name)
        if not value:
            return BrowserObservation('WAIT_INPUT', missing_fields=(name,))
        if status == 'PASSWORD':
            if type(material.email) is not str or not material.email.strip():
                return BrowserObservation('WAIT_INPUT', missing_fields=('email',))
            if not await self._password_account_matches(material.email):
                return BrowserObservation('WAIT_HUMAN', 'ACCOUNT_IDENTITY_UNCONFIRMED')
        if await self._visible(next_selector) is None:
            return BrowserObservation('WAIT_HUMAN', 'UNRECOGNIZED_SUBMIT_CONTROL')
        action = f'google.login.{name}'
        await self._permit(action + '.fill', status)
        control = await self._visible(selector)
        if control is None:
            return BrowserObservation('WAIT_HUMAN', 'FORM_CHANGED')
        if status == 'PASSWORD' and not await self._password_account_matches(material.email):
            return BrowserObservation('WAIT_HUMAN', 'ACCOUNT_IDENTITY_UNCONFIRMED')
        try:
            await control.fill(value, timeout=3000)
        except Exception:
            return BrowserObservation('WAIT_HUMAN', 'FIELD_INTERACTION_FAILED')
        await self._permit(action + '.submit', status)
        if status == 'PASSWORD' and not await self._password_account_matches(material.email):
            return BrowserObservation('WAIT_HUMAN', 'ACCOUNT_IDENTITY_UNCONFIRMED')
        # Lock before awaiting the journal: failure can mean a committed intent
        # whose acknowledgement was lost. Never blindly consume/click again.
        self._submitted.add(status)
        self._unknown = True
        if await self.intent(action + '.submit') is not True:
            raise PermissionError('durable browser intent denied')
        # Recheck page after persistence; callbacks may yield to human navigation.
        if self._paused or (await self.observe()).status != status:
            return BrowserObservation('UNKNOWN', 'PAGE_CHANGED_AFTER_INTENT')
        try:
            submit = await self._visible(next_selector)
            if submit is None:
                return BrowserObservation('UNKNOWN', 'SUBMIT_CONTROL_CHANGED')
            if status == 'PASSWORD' and not await self._password_account_matches(material.email):
                return BrowserObservation('UNKNOWN', 'ACCOUNT_CHANGED_AFTER_INTENT')
            await submit.click(timeout=3000)
            deadline = asyncio.get_running_loop().time() + self.observation_timeout
            while asyncio.get_running_loop().time() < deadline:
                after = await self.observe()
                recognized_progress = (after.status in ('EMAIL', 'PASSWORD', 'CONTACT')
                                       or (after.status == 'WAIT_HUMAN'
                                           and after.reason == 'VERIFICATION_REQUIRED'))
                if after.status != status and recognized_progress:
                    self._unknown = False
                    return after
                await asyncio.sleep(.05)
        except Exception:
            pass  # Raw Playwright exception text can contain secrets or URLs.
        return BrowserObservation('UNKNOWN', 'SUBMIT_RESULT_UNCONFIRMED')

    async def _contacts(self, material):
        controls = {name: item for name, selector in _CONTACT_FIELDS.items()
                    if (item := await self._visible(selector)) is not None}
        missing = tuple(name for name in controls if not getattr(material, name))
        if missing:
            return BrowserObservation('WAIT_INPUT', missing_fields=missing)
        for name, control in controls.items():
            await self._permit(f'google.contact.{name}.fill', 'CONTACT')
            try:
                value = getattr(material, name)
                if await control.evaluate('(el) => el.tagName') == 'SELECT':
                    await control.select_option(label=value, timeout=3000)
                else:
                    await control.fill(value, timeout=3000)
            except Exception:
                return BrowserObservation('WAIT_HUMAN', 'FIELD_INTERACTION_FAILED')
        # No contact submit, trial activation, or payment control is clicked.
        return BrowserObservation('FILLED', 'CONTACT_FIELDS_FILLED_NOT_SUBMITTED')
