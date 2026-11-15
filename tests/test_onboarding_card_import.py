"""Pure synthetic card CSV contract; never contacts external systems."""
import ast
import builtins
import csv
from dataclasses import FrozenInstanceError, fields, is_dataclass
import importlib
import importlib.util
import io
import inspect
import json
import os
import pathlib
import socket
import subprocess
import unittest
from unittest.mock import patch

from onboarding import card_import as subject
from onboarding.pool_secret_types import BillingAddress, BillingHolder, CardExpiry, Pan

HEADER = ('alias', 'pan', 'expiry_month', 'expiry_year', 'holder_name', 'country',
          'address_line1', 'address_line2', 'city', 'region', 'postal_code',
          'account_limit', 'sms_channel_ref')
FIXTURE = dict(zip(HEADER, ('fixture-card', '4111111111111111', '12', '2099',
    'fixture:holder', 'US', 'fixture:line', '', 'fixture:city', 'fixture:region',
    'fixture:postal', '2', 'fixture:sms')))


def document(*rows, header=HEADER, newline='\n'):
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator=newline)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(key, '') for key in header])
    return output.getvalue()


def changed(**kwargs):
    return dict(FIXTURE, **kwargs)


class CardImportTests(unittest.TestCase):
    def issues(self, text):
        return [(issue.line, issue.code) for issue in subject.parse_for_import(text).issues]

    def assert_issue(self, text, code, line=1):
        self.assertEqual(self.issues(text), [(line, code)])

    def test_module_exists(self):
        self.assertIsNotNone(importlib.util.find_spec('onboarding.card_import'))

    def test_public_contract_exists(self):
        for name in ('parse_for_import', 'preview', 'csv_template', 'ParsedCard',
                     'ParsedCardImport', 'CardImportIssue'):
            self.assertTrue(hasattr(subject, name), name)

    def test_complete_row_and_typed_payloads(self):
        parsed = subject.parse_for_import(document(FIXTURE))
        self.assertEqual(parsed.issues, ())
        self.assertIsInstance(parsed.records, tuple)
        self.assertIsInstance(parsed.issues, tuple)
        card, = parsed.records
        self.assertEqual((card.line, card.alias, card.brand, card.last4,
                          card.account_limit, card.sms_channel_ref),
                         (2, 'fixture-card', 'visa', '1111', 2, 'fixture:sms'))
        self.assertEqual(card.pan, Pan(FIXTURE['pan']))
        self.assertEqual(card.expiry, CardExpiry(12, 2099))
        self.assertEqual(card.holder, BillingHolder('fixture:holder'))
        self.assertEqual(card.address, BillingAddress('US', 'fixture:line', '',
                         'fixture:city', 'fixture:region', 'fixture:postal'))

    def test_both_synthetic_brands(self):
        parsed = subject.parse_for_import(document(FIXTURE, changed(pan='5555555555554444')))
        self.assertEqual([(c.brand, c.last4) for c in parsed.records],
                         [('visa', '1111'), ('mastercard', '4444')])

    def test_reordered_header(self):
        self.assertEqual(subject.parse_for_import(document(FIXTURE)),
                         subject.parse_for_import(document(FIXTURE, header=HEADER[::-1])))

    def test_optional_columns_are_empty_without_inventing_address(self):
        header = tuple(x for x in HEADER if x not in ('address_line2', 'sms_channel_ref'))
        card, = subject.parse_for_import(document(FIXTURE, header=header)).records
        self.assertEqual(card.address.line2, '')
        self.assertEqual(card.sms_channel_ref, '')
        header = tuple(x for x in HEADER if x != 'region')
        self.assert_issue(document(FIXTURE, header=header), 'SYNTHETIC_DATA_REQUIRED', 2)

    def test_bom_crlf_and_quoted_comma(self):
        parsed = subject.parse_for_import('\ufeff' + document(changed(alias='fixture, card'), newline='\r\n'))
        self.assertEqual(parsed.issues, ())
        self.assertEqual(parsed.records[0].alias, 'fixture, card')

    def test_template_has_only_full_canonical_header(self):
        self.assertEqual(subject.csv_template(), ','.join(HEADER) + '\n')
        self.assert_issue(subject.csv_template(), 'EMPTY_IMPORT')

    def test_empty_input(self):
        for index, value in enumerate(('', '\ufeff')):
            with self.subTest(case=index):
                self.assert_issue(value, 'EMPTY_IMPORT')

    def test_invalid_header(self):
        headers = [HEADER + ('unknown',), HEADER + ('alias',),
                   ('Alias',) + HEADER[1:], (' alias',) + HEADER[1:], (),
                   ('# comment',), ('\ufeffalias',) + HEADER[1:]]
        headers.extend(tuple(key for key in HEADER if key != removed)
                       for removed in HEADER if removed not in ('address_line2', 'region', 'sms_channel_ref'))
        for index, header in enumerate(headers):
            with self.subTest(case=index):
                text = document(FIXTURE, header=header)
                if index == 6:
                    text = '\ufeff' + text
                self.assert_issue(text, 'INVALID_HEADER')

    def test_record_column_counts_comments_and_blank_lines(self):
        for index, row in enumerate(('\n', '# comment\n', 'a,b\n', ','.join(['x'] * 14) + '\n')):
            with self.subTest(case=index):
                self.assert_issue(subject.csv_template() + row, 'INVALID_RECORD', 2)

    def test_malformed_csv_fails_closed_after_valid_row(self):
        text = document(FIXTURE) + '"unterminated\n'
        parsed = subject.parse_for_import(text)
        self.assertEqual(parsed.records, ())
        self.assert_issue(text, 'INVALID_CSV')
        self.assertFalse(subject.preview(text)['can_import'])
        self.assert_issue(subject.csv_template() + '"bad"suffix\n', 'INVALID_CSV')
        self.assert_issue('"unterminated', 'INVALID_CSV')

    def test_strict_input_types_and_utf8(self):
        class StringSubclass(str):
            pass
        for index, value in enumerate((None, b'', 1, [], {}, StringSubclass(''), '\ud800')):
            with self.subTest(case=index):
                self.assert_issue(value, 'INVALID_INPUT')
                self.assertFalse(subject.preview(value)['can_import'])

    def test_utf8_byte_limit_and_exact_boundary(self):
        base = document(FIXTURE)
        # An invalid row pads to the boundary without hitting csv's field limit.
        remaining = 262144 - len(base.encode())
        padding = ('x,' * ((remaining - 1) // 2)) + ('x' if remaining % 2 == 0 else '') + '\n'
        exact = base + padding
        self.assertEqual(len(exact.encode()), 262144)
        self.assert_issue(exact, 'INVALID_RECORD', 3)
        self.assert_issue(exact + 'x', 'INPUT_TOO_LARGE')
        self.assert_issue('é' * 131072 + 'x', 'INPUT_TOO_LARGE')
        self.assertEqual(subject.parse_for_import(exact + 'x').records, ())

    def test_thousand_record_boundary_counts_blank_rows(self):
        thousand = document(*([FIXTURE] * 1000))
        view = subject.preview(thousand)
        self.assertTrue(view['can_import'])
        self.assertEqual(view['duplicate_count'], 999)
        extra = thousand + '\n'
        self.assert_issue(extra, 'TOO_MANY_RECORDS')
        self.assertEqual(subject.parse_for_import(extra).records, ())
        self.assert_issue(subject.csv_template() + '\n' * 1001, 'TOO_MANY_RECORDS')

    def test_csv_field_limit_is_never_changed(self):
        old_limit = csv.field_size_limit()
        with patch.object(csv, 'field_size_limit', side_effect=AssertionError('global mutation')):
            subject.parse_for_import(document(FIXTURE))
            parsed = subject.parse_for_import(subject.csv_template() + 'x' * 140000)
        self.assertEqual(parsed.records, ())
        self.assertEqual(parsed.issues[0].code, 'INVALID_CSV')
        self.assertEqual(csv.field_size_limit(), old_limit)

    def test_ascii_decimal_syntax(self):
        invalid = ('', '0', '01', '00', ' 1', '1 ', '+1', '-1', '1.0', '1e0', '１', '١', '1_0', '1\n')
        for field in ('expiry_month', 'expiry_year', 'account_limit'):
            for index, value in enumerate(invalid):
                with self.subTest(field=field, case=index):
                    self.assert_issue(document(changed(**{field: value})), 'INVALID_RECORD', 2)

    def test_account_limit_range(self):
        for index, value in enumerate(('0', '10001', '9' * 5000)):
            with self.subTest(case=index):
                self.assert_issue(document(changed(account_limit=value)), 'INVALID_RECORD', 2)
        for value in ('1', '10000'):
            self.assertTrue(subject.preview(document(changed(account_limit=value)))['can_import'])

    def test_month_range_is_invalid_record(self):
        for value in ('0', '13', '9' * 5000):
            self.assert_issue(document(changed(expiry_month=value)), 'INVALID_RECORD', 2)

    def test_expiry_uses_existing_typed_policy(self):
        for index, values in enumerate((dict(expiry_year='2098'), dict(expiry_year='9' * 5000))):
            with self.subTest(case=index):
                self.assert_issue(document(changed(**values)), 'SYNTHETIC_DATA_REQUIRED', 2)
        for month in range(1, 13):
            self.assertTrue(subject.preview(document(changed(expiry_month=str(month))))['can_import'])

    def test_metadata_bounds_and_unicode_categories(self):
        for field, maximum in (('alias', 80), ('sms_channel_ref', 128)):
            self.assertTrue(subject.preview(document(changed(**{field: 'é' * maximum})))['can_import'])
            self.assert_issue(document(changed(**{field: 'a' * (maximum + 1)})), 'INVALID_RECORD', 2)
            for index, value in enumerate(('\x00', '\n', '\r', '\t', '\x7f', '\u200b', '\ue000', '\u0378')):
                with self.subTest(field=field, case=index):
                    self.assert_issue(document(changed(**{field: 'fixture' + value})), 'INVALID_RECORD', 2)
        self.assert_issue(document(changed(alias='')), 'INVALID_RECORD', 2)
        self.assertTrue(subject.preview(document(changed(sms_channel_ref='')))['can_import'])

    def test_metadata_rejects_accumulated_unicode_decimal_digits(self):
        for field in ('alias', 'sms_channel_ref'):
            for index, value in enumerate(('1234-5678-9012', '１２３４ ５６７８ ９０１２', '١٢٣٤-٥٦٧٨-٩٠١٢', FIXTURE['pan'])):
                with self.subTest(field=field, case=index):
                    self.assert_issue(document(changed(**{field: value})), 'INVALID_RECORD', 2)
            self.assertTrue(subject.preview(document(changed(**{field: '12345-678901'})))['can_import'])

    def test_no_value_trimming(self):
        card, = subject.parse_for_import(document(changed(alias=' fixture ', sms_channel_ref=' ref '))).records
        self.assertEqual(card.alias, ' fixture ')
        self.assertEqual(card.sms_channel_ref, ' ref ')
        self.assert_issue(document(changed(pan=' ' + FIXTURE['pan'])), 'SYNTHETIC_DATA_REQUIRED', 2)

    def test_typed_allowlists_are_not_expanded(self):
        changes = dict(pan='fixture:not-allowed', holder_name='fixture:other', country='CA',
                       address_line1='fixture:other', address_line2='fixture:other',
                       city='fixture:other', region='fixture:other', postal_code='fixture:other')
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assert_issue(document(changed(**{field: value})), 'SYNTHETIC_DATA_REQUIRED', 2)
                if field != 'address_line2':
                    self.assert_issue(document(changed(**{field: ''})), 'SYNTHETIC_DATA_REQUIRED', 2)
        self.assertTrue(subject.preview(document(changed(address_line2='fixture:line2')))['can_import'])

    def test_frozen_exact_dataclass_shapes_and_hidden_reprs(self):
        parsed = subject.parse_for_import(document(FIXTURE))
        card, = parsed.records
        issue = subject.CardImportIssue(2, 'INVALID_RECORD')
        for value in (parsed, card, issue):
            self.assertTrue(is_dataclass(value))
            with self.assertRaises(FrozenInstanceError):
                value.line = 9
        self.assertEqual([f.name for f in fields(card)], ['line', 'alias', 'brand', 'last4', 'pan',
            'expiry', 'holder', 'address', 'account_limit', 'sms_channel_ref'])
        self.assertEqual([f.name for f in fields(parsed)], ['records', 'issues'])
        self.assertEqual([f.name for f in fields(issue)], ['line', 'code'])
        self.assertEqual(repr(card), '<ParsedCard>')
        self.assertEqual(repr(parsed), '<ParsedCardImport>')
        nested = repr({'parsed': parsed, 'cards': parsed.records})
        for secret in (FIXTURE['pan'], 'fixture:holder', 'fixture:line', 'fixture:sms', '2099'):
            self.assertNotIn(secret, nested)
        for obj in (parsed, card):
            for method in ('to_dict', 'as_dict', 'serialize', 'to_json'):
                self.assertFalse(hasattr(obj, method))

    def test_exact_preview_schema_and_no_secrets(self):
        view = subject.preview(document(FIXTURE))
        self.assertEqual(set(view), {'items', 'issues', 'accepted_count', 'duplicate_count', 'conflict_count', 'can_import'})
        self.assertEqual(view['items'], [dict(line=2, alias='fixture-card', brand='visa',
            last4='1111', account_limit=2, sms_configured=True, billing_configured=True, expiry_configured=True)])
        self.assertEqual(view['issues'], [])
        self.assertEqual((view['accepted_count'], view['duplicate_count'], view['conflict_count'], view['can_import']), (1, 0, 0, True))
        rendered = json.dumps(view)
        for secret in (FIXTURE['pan'], 'fixture:holder', 'fixture:line', 'fixture:city',
                       'fixture:region', 'fixture:postal', 'fixture:sms', '2099'):
            self.assertNotIn(secret, rendered)
        self.assertFalse(subject.preview(document(changed(sms_channel_ref='')))['items'][0]['sms_configured'])

    def test_duplicate_keeps_first_and_allows_import(self):
        view = subject.preview(document(FIXTURE, FIXTURE, FIXTURE))
        self.assertTrue(view['can_import'])
        self.assertEqual(view['accepted_count'], 1)
        self.assertEqual(view['duplicate_count'], 2)
        self.assertEqual(view['items'][0]['line'], 2)
        self.assertEqual(view['issues'], [dict(line=3, code='DUPLICATE_CARD'), dict(line=4, code='DUPLICATE_CARD')])

    def test_conflicts_compare_every_variable_business_field(self):
        for field, value in dict(alias='another', expiry_month='1', address_line2='fixture:line2',
                                 account_limit='3', sms_channel_ref='another').items():
            with self.subTest(field=field):
                view = subject.preview(document(FIXTURE, changed(**{field: value})))
                self.assertFalse(view['can_import'])
                self.assertEqual(view['accepted_count'], 1)
                self.assertEqual(view['conflict_count'], 1)
                self.assertEqual(view['issues'], [dict(line=3, code='CONFLICTING_CARD')])

    def test_bad_row_preserves_other_rows_but_blocks_whole_import(self):
        view = subject.preview(document(FIXTURE, changed(alias=''), changed(pan='5555555555554444')))
        self.assertEqual(view['accepted_count'], 2)
        self.assertFalse(view['can_import'])
        self.assertEqual(view['issues'], [dict(line=3, code='INVALID_RECORD')])

    def test_physical_record_start_line_after_multiline_row(self):
        text = document(changed(alias='fixture\ncard'), FIXTURE, FIXTURE)
        view = subject.preview(text)
        self.assertEqual(view['items'][0]['line'], 4)
        self.assertEqual(view['issues'], [dict(line=2, code='INVALID_RECORD'), dict(line=5, code='DUPLICATE_CARD')])

    def test_error_preview_never_echoes_input(self):
        view = subject.preview(document(changed(alias='fixture\nsecret', pan='fixture:rejected-secret')))
        self.assertEqual(view['items'], [])
        self.assertEqual(view['issues'], [dict(line=2, code='INVALID_RECORD')])
        self.assertNotIn('secret', json.dumps(view))

    def test_imports_are_only_stdlib_and_existing_pure_types(self):
        tree = ast.parse(inspect.getsource(subject))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add('.' * node.level + (node.module or ''))
        self.assertLessEqual(imports, {'csv', 'dataclasses', 'io', 're', 'unicodedata',
                                      '.errors', '.pool_secret_types'})

    def test_pure_reload_and_execution_forbid_external_io_and_database_imports(self):
        original_import = builtins.__import__
        blocked = ('psycopg', 'psycopg2', 'sqlite3', 'sqlalchemy', 'requests', 'httpx',
                   'onboarding.store', 'onboarding.db', 'onboarding.pool_secret_store',
                   'store', 'db', 'pool_secret_store', 'pool_repository')
        def guarded_import(name, *args, **kwargs):
            if any(name == prefix or name.startswith(prefix + '.') for prefix in blocked):
                raise AssertionError('forbidden dependency')
            return original_import(name, *args, **kwargs)
        with patch('builtins.open', side_effect=AssertionError('file I/O')), \
             patch.object(pathlib.Path, 'open', side_effect=AssertionError('path I/O')), \
             patch.object(os, 'open', side_effect=AssertionError('raw file I/O')), \
             patch.object(os, 'system', side_effect=AssertionError('shell I/O')), \
             patch.object(socket, 'socket', side_effect=AssertionError('network I/O')), \
             patch.object(socket, 'create_connection', side_effect=AssertionError('network I/O')), \
             patch.object(subprocess, 'Popen', side_effect=AssertionError('process I/O')), \
             patch('builtins.__import__', side_effect=guarded_import):
            importlib.reload(subject)
            self.assertTrue(subject.preview(document(FIXTURE))['can_import'])
            self.assertEqual(len(subject.parse_for_import(document(FIXTURE)).records), 1)
            self.assertEqual(subject.csv_template(), ','.join(HEADER) + '\n')


if __name__ == '__main__':
    unittest.main()
