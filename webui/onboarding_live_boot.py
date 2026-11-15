"""Explicit trusted local composition for the existing control panel.

No migration, credential generation, login, provider request or startup adoption.
Absence of a binding permits observation/configuration only. Source import and
execution bindings are supplied by a private local file, never by a Web actor,
request body, editable .env, mailbox selection or current Google SDK defaults.
"""
from pathlib import Path
from uuid import UUID

from onboarding import storage
from onboarding.google_auth_runtime import GoogleAuthConfig, _ACCOUNT
from onboarding.keyring import Keyring
from onboarding.live_executor import validate_dependencies
from onboarding.live_input_vault import LiveInputVault, LivePoolPolicy
from onboarding.live_key_store import ServiceAccountKeyStore
from onboarding.live_mailbox_source import OwnedMailboxSource
from onboarding.live_manager import LiveRunManager
from onboarding.request_mac import RequestMac
from onboarding.sub2api_probe import _json, _private_bytes, load_config as load_sub2api_config


def _path(value):
    if (type(value) is not str or not value.startswith('/') or '\x00' in value
            or '..' in value.split('/') or str(Path(value)) != value or value == '/'):
        raise ValueError


def load_binding(path):
    try:
        value = _json(_private_bytes(path))
        if type(value) is not dict or set(value) != {'owner_id','data_root','mailbox_id','sub2api_config_file'}:
            raise ValueError
        owner = value['owner_id']
        if type(owner) is not str or str(UUID(owner)) != owner:
            raise ValueError
        _path(value['data_root'])
        mailbox, target = value['mailbox_id'], value['sub2api_config_file']
        if mailbox is None:
            if target is not None:
                raise ValueError
        else:
            if type(mailbox) is not str or str(UUID(mailbox)) != mailbox:
                raise ValueError
            _path(target)
        return value
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, AttributeError):
        raise ValueError('LIVE_BINDING_UNAVAILABLE') from None


def _base(settings, keyring, mac):
    if type(keyring) is not Keyring or type(mac) is not RequestMac or keyring.directory != mac.directory:
        raise ValueError('LIVE_DEPENDENCIES_UNAVAILABLE')
    keyring.__post_init__()
    mac._key()
    policy = LivePoolPolicy(settings)
    vault = LiveInputVault(keyring, policy)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
    return policy, vault


def prepare(settings, keyring, mac, binding_path=None):
    policy, vault = _base(settings, keyring, mac)
    result = dict(live_policy=policy, live_vault=vault, live_source=None,
                  live_manager=None, live_mailbox_id=None)
    if binding_path is None:
        return result
    binding = load_binding(binding_path)
    source = OwnedMailboxSource(Path(binding['data_root']), binding['owner_id'], _ACCOUNT)
    result['live_source'] = source
    if binding['mailbox_id'] is None:
        return result
    target = load_sub2api_config(binding['sub2api_config_file'], allow_loopback_http=True)
    auth = GoogleAuthConfig(_ACCOUNT)
    keys = ServiceAccountKeyStore(settings, keyring)
    validate_dependencies(settings, key_store=keys, google_auth_config=auth, sub2api_config=target)
    manager = LiveRunManager(settings, binding['mailbox_id'], source=source, policy=policy, mac=mac,
                            key_store=keys, google_auth_config=auth, sub2api_config=target)
    result.update(live_mailbox_id=binding['mailbox_id'], live_manager=manager)
    return result
