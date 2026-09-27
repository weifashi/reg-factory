"""Offline mailbox compatibility boundary; no persistence or execution authority."""
from dataclasses import dataclass

@dataclass(frozen=True)
class ImportIssue:
    line: int
    code: str

@dataclass(frozen=True, repr=False)
class ParsedMailbox:
    line: int
    email_norm: str
    password: str = ''
    account_password: str = ''
    refresh_token: str = ''
    client_id: str = ''
    provider: str = ''
    mail_api_url: str = ''
    mail_api_key: str = ''
    two_factor: str = ''
    def __repr__(self): return '<ParsedMailbox>'

@dataclass(frozen=True, repr=False)
class ParsedImport:
    records: tuple[ParsedMailbox, ...]
    issues: tuple[ImportIssue, ...]
    def __repr__(self): return '<ParsedImport>'

# The legacy parser is deliberately the only non-stdlib dependency.
import json
import re
import unicodedata
from common import account_records as legacy

MAX_BYTES = 262144
MAX_RECORDS = 1000
_FIELDS = ('password', 'account_password', 'refresh_token', 'client_id', 'provider',
           'mail_api_url', 'mail_api_key', 'two_factor')
_ALIASES = {
    'email': ('email', 'username', 'login'),
    'password': ('password', 'pass', 'pwd'),
    'account_password': ('account_password', 'chatgpt_password', 'login_password'),
    'refresh_token': ('refresh_token', 'refreshToken', 'oauth_refresh_token', 'rt'),
    'client_id': ('client_id', 'clientId', 'app_id', 'appId'),
    'provider': ('provider',),
    'mail_api_url': ('mail_api_url', 'icloud_api_url', 'mailbox_api_url', 'mail_api_base', 'mailbox_url'),
    'mail_api_key': ('mail_api_key', 'icloud_api_key', 'mailbox_api_key'),
    'two_factor': ('two_factor', 'two_fa', '2fa', 'otp_secret', 'twoFactor'),
}
_NESTED = {'email': ('email',), 'refresh_token': ('refresh_token', 'refreshToken'),
           'client_id': ('client_id', 'clientId')}
_TOKEN_KEYS = frozenset(('access_token', 'accessToken', 'at', 'session_token', 'sessionToken',
    'chatgpt_session_token', 'cookies', 'cookie', 'oauth', 'token', 'oauth_credentials',
    'id_token', 'idToken'))
_ALLOWED = frozenset(key for aliases in _ALIASES.values() for key in aliases) | {'credentials', 'source_type'}
_NESTED_ALLOWED = frozenset(key for aliases in _NESTED.values() for key in aliases)


class _Rejected(ValueError):
    """Only fixed codes; never retain caller input or underlying exception text."""


def _controls(value):
    return any(unicodedata.category(char) in {'Cc', 'Cf'} for char in value)


def _email(value):
    if _controls(value):
        raise _Rejected('INVALID_RECORD')
    value = value.strip().casefold()
    if len(value) > 320 or not legacy.EMAIL_RE.fullmatch(value):
        raise _Rejected('INVALID_RECORD')
    return value


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise _Rejected('DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def _constant(value):
    raise _Rejected('INVALID_JSON')


def _decode(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except _Rejected:
        raise
    except (ValueError, RecursionError):
        raise _Rejected('INVALID_JSON') from None


def _json_record(value, line):
    if type(value) is not dict:
        raise _Rejected('INVALID_JSON')
    nested = value.get('credentials', {})
    if (_TOKEN_KEYS.intersection(value)
            or (isinstance(nested, dict) and _TOKEN_KEYS.intersection(nested))
            or ('source_type' in value and value['source_type'] != 'mailbox')):
        raise _Rejected('UNSUPPORTED_RECORD_TYPE')
    if (set(value) - _ALLOWED or type(nested) is not dict
            or set(nested) - _NESTED_ALLOWED):
        raise _Rejected('INVALID_RECORD')
    canonical = {}
    for field, aliases in _ALIASES.items():
        entries = [value[key] for key in aliases if key in value]
        entries += [nested[key] for key in _NESTED.get(field, ()) if key in nested]
        if any(type(item) is not str for item in entries):
            raise _Rejected('INVALID_RECORD')
        for item in entries:
            try:
                item.encode('utf-8')
            except UnicodeError:
                raise _Rejected('INVALID_RECORD') from None
        if field == 'email':
            entries = [_email(item) for item in entries]
        elif field == 'provider':
            if any(_controls(item) for item in entries):
                raise _Rejected('INVALID_RECORD')
            entries = [item.strip().lower() for item in entries]
        if entries:
            if any(item != entries[0] for item in entries):
                raise _Rejected('INVALID_RECORD')
            canonical[field] = entries[0]
    if canonical.get('provider', '') not in {'', 'outlook', 'graph', 'microsoft', 'icloud'}:
        raise _Rejected('INVALID_RECORD')
    try:
        # Reuse legacy field recognition/defaults/validation, then restore JSON
        # secret strings byte-for-byte: the old mapping parser strips whitespace.
        parsed = legacy.parse_account_line(json.dumps(canonical, ensure_ascii=False))
    except (ValueError, TypeError, RecursionError):
        raise _Rejected('INVALID_RECORD') from None
    for field in _FIELDS:
        if field in canonical and field != 'provider':
            if field == 'client_id' and not canonical[field] and parsed.get('refresh_token'):
                continue  # existing default Graph client-id rule
            parsed[field] = canonical[field]
    return _record(parsed, line)


def _record(parsed, line):
    if parsed.get('source_type') != 'mailbox':
        raise _Rejected('UNSUPPORTED_RECORD_TYPE')
    return ParsedMailbox(line=line, email_norm=_email(parsed['email']),
                         **{field: parsed.get(field, '') for field in _FIELDS})


def _starts_record(line):
    if line.lstrip().startswith(('{', '[')) or legacy.COOKIE_RE.search(line):
        return True
    fields = legacy._split_fields(line)
    return bool(fields and legacy.EMAIL_RE.fullmatch(fields[0]))


def _text_candidates(text):
    lines = text.split('\n')
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = index + 1
        index += 1
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        if (legacy.EMAIL_RE.fullmatch(raw.strip()) and index < len(lines)
                and re.fullmatch(r'https?://[^\s]+', lines[index].strip(), re.IGNORECASE)):
            mapping = {'email': raw.strip(), 'provider': 'icloud', 'mail_api_url': lines[index].strip()}
            index += 1
            third = index
            while third < len(lines) and not lines[third].strip():
                third += 1
            if (third < len(lines) and not lines[third].lstrip().startswith('#')
                    and not _starts_record(lines[third])):
                mapping['two_factor'] = lines[third].strip()
                index = third + 1
            yield line, mapping
        else:
            yield line, raw


def _failure(code):
    return ParsedImport((), (ImportIssue(1, code),))


def parse_for_import(text: str, *, plus_credentials: bool = False) -> ParsedImport:
    if type(text) is not str or type(plus_credentials) is not bool:
        return _failure('INVALID_INPUT')
    try:
        size = len(text.encode('utf-8'))
    except UnicodeError:
        return _failure('INVALID_INPUT')
    if size > MAX_BYTES:
        return _failure('INPUT_TOO_LARGE')
    text = text.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    stripped = text.lstrip()
    candidates = None
    if stripped.startswith('['):
        try:
            value = _decode(text)
        except _Rejected as error:
            return _failure(str(error))
        if type(value) is not list or any(type(item) is not dict for item in value):
            return _failure('INVALID_JSON')
        candidates = list(enumerate(value, start=1))
    elif stripped.startswith('{'):
        # A complete pretty-printed object is one candidate. JSONL/mixed text
        # remains line-oriented; each JSON candidate is strictly decoded below.
        try:
            value = _decode(text)
        except _Rejected as error:
            nonempty = [line for line in text.split('\n') if line.strip()]
            # JSONL is one complete candidate per physical line, including a
            # malformed first object. Do not discard valid neighbors when a
            # following line is plain invalid text rather than another object.
            first_is_line_object = bool(nonempty and nonempty[0].strip().endswith('}'))
            if len(nonempty) == 1 or not first_is_line_object:
                return _failure(str(error))
        else:
            candidates = [(text[:text.index('{')].count('\n') + 1, value)]
    if candidates is None:
        candidates = list(_text_candidates(text))
    if len(candidates) > MAX_RECORDS:
        return _failure('TOO_MANY_RECORDS')
    records, issues, seen = [], [], {}
    for line, candidate in candidates:
        try:
            if type(candidate) is dict:
                record = _json_record(candidate, line)
            elif candidate.lstrip().startswith(('{', '[')):
                record = _json_record(_decode(candidate), line)
            else:
                if '\x00' in candidate or '\ufeff' in candidate:
                    raise _Rejected('INVALID_RECORD')
                if len(legacy._split_fields(candidate)) > 4:
                    raise _Rejected('INVALID_RECORD')
                try:
                    parsed = legacy.parse_account_line(candidate, plus_credentials=plus_credentials)
                except (ValueError, TypeError):
                    raise _Rejected('INVALID_RECORD') from None
                record = _record(parsed, line)
            prior = seen.get(record.email_norm)
            if prior is not None:
                same = all(getattr(prior, field) == getattr(record, field) for field in _FIELDS)
                raise _Rejected('DUPLICATE_EMAIL' if same else 'CONFLICTING_EMAIL')
            seen[record.email_norm] = record
            records.append(record)
        except _Rejected as error:
            issues.append(ImportIssue(line, str(error)))
    return ParsedImport(tuple(records), tuple(issues))


def preview(text: str, source_group: str = '', *, plus_credentials: bool = False) -> dict:
    try:
        valid_group = (type(source_group) is str and len(source_group) <= 128
                       and not _controls(source_group))
        if valid_group:
            source_group.encode('utf-8')
    except UnicodeError:
        valid_group = False
    if not valid_group:
        parsed = _failure('INVALID_INPUT')
    else:
        parsed = parse_for_import(text, plus_credentials=plus_credentials)
    return {'items': [{'line': record.line, 'email': record.email_norm,
                      'provider': record.provider, 'group_ref': source_group} for record in parsed.records],
            'issues': [{'line': issue.line, 'code': issue.code} for issue in parsed.issues],
            'accepted_count': len(parsed.records),
            'duplicate_count': sum(issue.code == 'DUPLICATE_EMAIL' for issue in parsed.issues),
            'conflict_count': sum(issue.code == 'CONFLICTING_EMAIL' for issue in parsed.issues)}


def serialize_mailbox(record: ParsedMailbox) -> str:
    if type(record) is not ParsedMailbox:
        raise ValueError('INVALID_INPUT')
    payload = {'email': record.email_norm}
    for field in _FIELDS:
        value = getattr(record, field)
        if type(value) is not str:
            raise ValueError('INVALID_INPUT')
        if value:
            payload[field] = value
    if type(record.email_norm) is not str:
        raise ValueError('INVALID_INPUT')
    try:
        if type(record.line) is not int or record.line < 1 or _json_record(payload, record.line) != record:
            raise ValueError
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('INVALID_INPUT') from None
