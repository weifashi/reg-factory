"""Pure synthetic card CSV parsing; preview is not a persistence authorization.

Only existing synthetic secret types are accepted. No storage, external I/O,
secret references, or plain-text serialization API is provided here.
"""
import csv
from dataclasses import dataclass
import io
import re
import unicodedata

from .errors import ServiceError
from .pool_secret_types import BillingAddress, BillingHolder, CardExpiry, Pan

_HEADER = ('alias', 'pan', 'expiry_month', 'expiry_year', 'holder_name',
           'country', 'address_line1', 'address_line2', 'city', 'region',
           'postal_code', 'account_limit', 'sms_channel_ref')
_OPTIONAL = frozenset(('address_line2', 'region', 'sms_channel_ref'))
_REQUIRED = frozenset(_HEADER) - _OPTIONAL
_POSITIVE_DECIMAL = re.compile(r'[1-9][0-9]*\Z')
_MAX_BYTES = 262144
_MAX_RECORDS = 1000


@dataclass(frozen=True)
class CardImportIssue:
    line: int
    code: str


@dataclass(frozen=True, repr=False)
class ParsedCard:
    line: int
    alias: str
    brand: str
    last4: str
    pan: Pan
    expiry: CardExpiry
    holder: BillingHolder
    address: BillingAddress
    account_limit: int
    sms_channel_ref: str

    def __repr__(self):
        return '<ParsedCard>'


@dataclass(frozen=True, repr=False)
class ParsedCardImport:
    records: tuple[ParsedCard, ...]
    issues: tuple[CardImportIssue, ...]

    def __repr__(self):
        return '<ParsedCardImport>'


def _failure(code):
    return ParsedCardImport((), (CardImportIssue(1, code),))


def _metadata(value, maximum, *, empty=False):
    return ((bool(value) or empty) and len(value) <= maximum
            and not any(unicodedata.category(char).startswith('C') for char in value)
            and sum(char.isdecimal() for char in value) < 12)


def _bounded_decimal(value, maximum):
    # Compare before int(): hostile digit strings must not reach Python's
    # conversion limit or allocate arbitrarily large integers.
    bound = str(maximum)
    return len(value) < len(bound) or (len(value) == len(bound) and value <= bound)


def _parse_row(row, line):
    if (not _metadata(row['alias'], 80)
            or not _metadata(row['sms_channel_ref'], 128, empty=True)
            or any(not _POSITIVE_DECIMAL.fullmatch(row[key])
                   for key in ('expiry_month', 'expiry_year', 'account_limit'))
            or not _bounded_decimal(row['expiry_month'], 12)
            or not _bounded_decimal(row['account_limit'], 10000)):
        return None, 'INVALID_RECORD'
    # Existing CardExpiry permits only 2099. Longer positive years cannot be
    # synthetic, and are rejected without an unbounded int conversion.
    if len(row['expiry_year']) > 4:
        return None, 'SYNTHETIC_DATA_REQUIRED'
    try:
        pan = Pan(row['pan'])
        expiry = CardExpiry(int(row['expiry_month']), int(row['expiry_year']))
        holder = BillingHolder(row['holder_name'])
        address = BillingAddress(row['country'], row['address_line1'],
                                 row['address_line2'], row['city'], row['region'],
                                 row['postal_code'])
    except ServiceError:
        return None, 'SYNTHETIC_DATA_REQUIRED'
    brand = 'visa' if pan.value == '4111111111111111' else 'mastercard'
    return ParsedCard(line, row['alias'], brand, pan.value[-4:], pan, expiry,
                      holder, address, int(row['account_limit']),
                      row['sms_channel_ref']), None


def _business_fields(card):
    # Include all business fields, excluding only source line; never publish
    # this tuple, its PAN, or a plain hash of the imported material.
    return (card.alias, card.brand, card.last4, card.pan, card.expiry,
            card.holder, card.address, card.account_limit, card.sms_channel_ref)


def parse_for_import(csv_text) -> ParsedCardImport:
    """Parse in-memory synthetic CSV; callers must reject non-duplicate issues."""
    if type(csv_text) is not str:
        return _failure('INVALID_INPUT')
    try:
        size = len(csv_text.encode('utf-8'))
    except UnicodeError:
        return _failure('INVALID_INPUT')
    if size > _MAX_BYTES:
        return _failure('INPUT_TOO_LARGE')
    text = csv_text.removeprefix('\ufeff')
    reader = csv.reader(io.StringIO(text, newline=''), strict=True)
    records = []
    issues = []
    by_pan = {}
    try:
        header = next(reader, None)
        if header is None:
            return _failure('EMPTY_IMPORT')
        if (len(header) != len(set(header)) or not _REQUIRED.issubset(header)
                or not set(header).issubset(_HEADER)):
            return _failure('INVALID_HEADER')
        count = 0
        while True:
            line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            count += 1
            if count > _MAX_RECORDS:
                return _failure('TOO_MANY_RECORDS')
            if len(cells) != len(header):
                issues.append(CardImportIssue(line, 'INVALID_RECORD'))
                continue
            row = dict.fromkeys(_OPTIONAL, '')
            row.update(zip(header, cells))
            card, code = _parse_row(row, line)
            if code is not None:
                issues.append(CardImportIssue(line, code))
                continue
            previous = by_pan.get(card.pan.value)
            if previous is not None:
                code = ('DUPLICATE_CARD' if _business_fields(card) == _business_fields(previous)
                        else 'CONFLICTING_CARD')
                issues.append(CardImportIssue(line, code))
                continue
            by_pan[card.pan.value] = card
            records.append(card)
    except csv.Error:
        # A structural error invalidates the entire file, even previously
        # parsed good rows; never expose csv's exception text.
        return _failure('INVALID_CSV')
    if count == 0:
        return _failure('EMPTY_IMPORT')
    return ParsedCardImport(tuple(records), tuple(issues))


def preview(csv_text) -> dict:
    """Return only explicitly allowlisted display metadata, never secret data."""
    parsed = parse_for_import(csv_text)
    return {
        'items': [
            {'line': card.line, 'alias': card.alias, 'brand': card.brand,
             'last4': card.last4, 'account_limit': card.account_limit,
             'sms_configured': bool(card.sms_channel_ref),
             'billing_configured': True, 'expiry_configured': True}
            for card in parsed.records
        ],
        'issues': [{'line': issue.line, 'code': issue.code} for issue in parsed.issues],
        'accepted_count': len(parsed.records),
        'duplicate_count': sum(issue.code == 'DUPLICATE_CARD' for issue in parsed.issues),
        'conflict_count': sum(issue.code == 'CONFLICTING_CARD' for issue in parsed.issues),
        'can_import': bool(parsed.records) and all(
            issue.code == 'DUPLICATE_CARD' for issue in parsed.issues),
    }


def csv_template() -> str:
    """Canonical header only; never include card or billing sample values."""
    return ','.join(_HEADER) + '\n'
