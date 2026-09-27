"""Offline guards for the not-committed proof: where it may be raised, and the invariants it relies on."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROOF_SITES = {('onboarding/pool_batches.py', 'create'), ('onboarding/pool_commands.py', '_command'),
               ('onboarding/mailbox_update.py', 'update'), ('onboarding/pool_config.py', 'replace'),
               ('onboarding/mailboxes.py', 'import_text')}
FLAG_ALLOWED = {('onboarding/errors.py', '__init__'), ('webui/onboarding_auth.py', 'error_response'),
                ('webui/onboarding_auth.py', '_proves_not_committed'), ('webui/onboarding_auth.py', 'service_error')}
PROOF_CALLERS = {'onboarding/pool_batches.py', 'onboarding/pool_commands.py', 'onboarding/mailbox_update.py',
                 'onboarding/pool_config.py', 'onboarding/mailboxes.py', 'webui/onboarding_pool_routes.py',
                 'webui/onboarding_routes.py'}
PROOF_FUNCTIONS = {'create', 'pause', 'cancel', 'recheck', 'update', 'replace', 'import_text'}
ROUTE_HANDLERS = {('webui/onboarding_pool_routes.py', name) for name in
                  ('import_mailboxes', 'update_mailbox', 'replace_config', 'batches')}
ROUTE_HANDLERS |= {('webui/onboarding_routes.py', name) for name in
                   ('command_route', 'pause_route', 'cancel_route', 'recheck_route')}
IMMUTABLE = {'mailbox_registry': {'email_norm', 'owner_operator_id', 'source_fingerprint'},
             'onboarding_tasks': {'execution_scope'},
             'operation_receipts': {'scope_operator_id', 'action', 'task_id', 'idempotency_key', 'request_hash'},
             'global_configs': set()}
BUMP_COLUMNS = {'status', 'reason_code', 'current_step', 'generation', 'cancel_requested'}


def product_files():
    return sorted(p for folder in ('onboarding', 'webui') for p in (ROOT / folder).rglob('*.py')
                  if '__pycache__' not in p.parts)


def enclosing(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    def function_of(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None
    return function_of


def is_service_error(node):
    return (isinstance(node, ast.Name) and node.id == 'ServiceError') or \
           (isinstance(node, ast.Attribute) and node.attr == 'ServiceError')


def python_violations(source, module):
    tree, found = ast.parse(source), []
    function_of = enclosing(tree)
    for node in ast.walk(tree):
        where = (module, function_of(node))
        names = []
        if isinstance(node, ast.Name): names.append(node.id)
        if isinstance(node, ast.Attribute): names.append(node.attr)
        if isinstance(node, ast.keyword) and node.arg: names.append(node.arg)
        if isinstance(node, ast.arg): names.append(node.arg)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and 'not_committed' in node.value:
            names.append('not_committed')
        if 'not_committed' in names and where not in FLAG_ALLOWED and where not in PROOF_SITES:
            found.append('not_committed outside allowlist at %s:%s' % where)
        if isinstance(node, ast.ClassDef) and any(is_service_error(base) for base in node.bases):
            found.append('ServiceError subclass %s in %s' % (node.name, module))
        if isinstance(node, ast.ImportFrom) and any(alias.name == 'ServiceError' and alias.asname for alias in node.names):
            found.append('aliased ServiceError import in %s' % module)
        if isinstance(node, ast.Call) and is_service_error(node.func):
            if any(k.arg is None for k in node.keywords):
                found.append('ServiceError(**...) in %s' % module)
            flags = [k for k in node.keywords if k.arg == 'not_committed']
            if flags:
                literal = (len(node.args) == 1 and isinstance(node.args[0], ast.Attribute)
                           and node.args[0].attr == 'VERSION_CONFLICT'
                           and isinstance(flags[0].value, ast.Constant) and flags[0].value.value is True)
                if where not in PROOF_SITES or not literal:
                    found.append('non-literal or misplaced proof at %s:%s' % where)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == 'not_committed' \
                        and where != ('onboarding/errors.py', '__init__'):
                    found.append('assignment to not_committed at %s:%s' % where)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'setattr':
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value == 'not_committed':
                found.append('setattr not_committed at %s:%s' % where)
        if isinstance(node, ast.ExceptHandler) and (module, function_of(node)) in ROUTE_HANDLERS:
            if node.type is not None and 'ServiceError' in ast.unparse(node.type):
                found.append('route handler catches ServiceError at %s:%s' % (module, function_of(node)))
    return found


# A table as written in SQL: optional ONLY, optional (quoted) schema, (quoted) name.
TABLE = r'(?:ONLY\s+)?(?:"?\w+"?\s*\.\s*)?"?(\w+)"?'
CLAUSE_END = r'(?=\sWHERE\s|\sRETURNING\s|;|$)'
UPDATE_SET = re.compile(r'\bUPDATE\s+' + TABLE + r'(?:\s+(?:AS\s+)?(?!SET\b)\w+)?\s+SET\s+(.*?)' + CLAUSE_END,
                        re.I | re.S)
UPSERT_SET = re.compile(r'\bINSERT\s+INTO\s+' + TABLE + r'[^;]*?\bON\s+CONFLICT\b[^;]*?\bDO\s+UPDATE\s+SET\s+(.*?)'
                        + CLAUSE_END, re.I | re.S)
DELETE_FROM = re.compile(r'\bDELETE\s+FROM\s+' + TABLE, re.I)
MERGE_INTO = re.compile(r'\bMERGE\s+INTO\s+' + TABLE, re.I)
TRUNCATE = re.compile(r'\bTRUNCATE\b([^;]*)', re.I)
ALTER_DROP = re.compile(r'\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?' + TABLE + r'[^;]*?\bDROP\s+(?:CONSTRAINT|COLUMN)\b',
                        re.I | re.S)
DROP_INDEX = re.compile(r'\bDROP\s+INDEX\b', re.I)
# plpgsql assignment statements (":=" or "="), not comparisons inside IF conditions.
NEW_ASSIGN = re.compile(r'(?:^|;|\bBEGIN\b|\bTHEN\b|\bELSE\b|\bLOOP\b)\s*NEW\s*\.\s*"?(\w+)"?\s*:?=(?!=)', re.I)
# A write whose target table is not a literal: concatenation, f-string or str.format placeholder.
OPEN_TABLE = re.compile(r'(?:^|;)\s*(?:UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|MERGE\s+INTO)\s+(?:ONLY\s+)?$', re.I)
PLACEHOLDER_TABLE = re.compile(r'\b(?:UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|MERGE\s+INTO)\s+(?:ONLY\s+)?\{[^}]*\}',
                               re.I)
ASSIGNED = re.compile(r'(?:^|,)\s*(\([^)]*\)|"?\w+"?(?:\s*\.\s*"?\w+"?)?)\s*=(?!=)', re.S)
APPEND_ONLY = {'global_configs'}
TRIGGER_IMMUTABLE = set().union(*IMMUTABLE.values()) | {'version'}


def assigned_columns(clause):
    columns = set()
    for match in ASSIGNED.finditer(clause):
        target = match.group(1)
        names = target[1:-1].split(',') if target.startswith('(') else [target]
        columns |= {name.strip().split('.')[-1].strip().strip('"').lower() for name in names}
    return columns


def assignment_violations(verb, table, clause, module):
    found, columns = [], assigned_columns(clause)
    if table in APPEND_ONLY:
        found.append('%s modifies append-only %s in %s' % (verb, table, module))
    for column in sorted(columns & IMMUTABLE[table]):
        found.append('%s %s SET %s in %s' % (verb, table, column, module))
    if table in ('mailbox_registry', 'onboarding_tasks') and 'version' in columns and not re.search(
            r'(?:^|,)\s*"?version"?\s*=\s*"?version"?\s*\+\s*1\b', clause, re.I):
        found.append('non-increment version assignment on %s in %s' % (table, module))
    return found


def sql_violations(source, module):
    found = []
    texts = [source] if module.endswith('.sql') else \
        [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    for text in texts:
        for verb, pattern in (('UPDATE', UPDATE_SET), ('UPSERT', UPSERT_SET)):
            for table, clause in pattern.findall(text):
                if table.lower() in IMMUTABLE:
                    found += assignment_violations(verb, table.lower(), clause, module)
        for verb, pattern in (('DELETE FROM', DELETE_FROM), ('MERGE INTO', MERGE_INTO), ('DROP from', ALTER_DROP)):
            for table in pattern.findall(text):
                if table.lower() in IMMUTABLE:
                    found.append('%s %s in %s' % (verb, table.lower(), module))
        for targets in TRUNCATE.findall(text):
            for table in sorted({word.lower() for word in re.findall(r'\w+', targets)} & set(IMMUTABLE)):
                found.append('TRUNCATE %s in %s' % (table, module))
        if DROP_INDEX.search(text):
            found.append('DROP INDEX in %s (uniqueness behind the proof must be re-reviewed)' % module)
        for column in NEW_ASSIGN.findall(text):
            if column.lower() in TRIGGER_IMMUTABLE:
                found.append('trigger assigns NEW.%s in %s' % (column.lower(), module))
        if OPEN_TABLE.search(text) or PLACEHOLDER_TABLE.search(text):
            found.append('write with a non-literal table in %s' % module)
    if not module.endswith('.sql'):
        tree = ast.parse(source)
        function_of = enclosing(tree)
        for node in ast.walk(tree):
            direct = isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == 'SQL')
                or (isinstance(node.func, ast.Name) and node.func.id == 'SQL'))
            if direct and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str) \
                    and re.search(r'\b(UPDATE|DELETE|TRUNCATE|MERGE)\b', node.args[0].value, re.I) \
                    and (module, function_of(node)) != ('onboarding/repository.py', 'bump_task'):
                found.append('dynamic UPDATE/DELETE composition in %s:%s' % (module, function_of(node)))
    return found


def script_violations(text, name):
    count = text.count('not_committed')
    return [] if count == 1 else ['%s mentions not_committed %d times (expected exactly 1 in api())' % (name, count)]


class NotCommittedGuardTests(unittest.TestCase):
    def test_product_python_has_no_guard_violations(self):
        found = []
        for path in product_files():
            module = str(path.relative_to(ROOT))
            found += python_violations(path.read_text(), module) + sql_violations(path.read_text(), module)
        self.assertEqual(found, [])

    def test_every_proof_site_raises_exactly_once(self):
        for module, function in sorted(PROOF_SITES):
            tree = ast.parse((ROOT / module).read_text())
            target = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
            proofs = [n for n in ast.walk(target) if isinstance(n, ast.Call) and is_service_error(n.func)
                      and any(k.arg == 'not_committed' for k in n.keywords)]
            self.assertEqual(len(proofs), 1, module + ':' + function)

    def test_migrations_keep_invariants(self):
        found = []
        for path in sorted((ROOT / 'onboarding' / 'migrations').glob('*.sql')):
            found += sql_violations(path.read_text(), str(path.relative_to(ROOT)))
        self.assertEqual(found, [])

    def test_bump_task_whitelist_is_mutable_columns_only(self):
        tree = ast.parse((ROOT / 'onboarding' / 'repository.py').read_text())
        bump = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'bump_task')
        sets = [n for n in ast.walk(bump) if isinstance(n, ast.Set)]
        self.assertEqual([{e.value for e in s.elts} for s in sets], [BUMP_COLUMNS])
        literals = {n.value for n in ast.walk(bump) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertIn('version=version+1', literals)

    def test_proof_functions_called_only_from_their_routes(self):
        proof_modules = {'pool_batches', 'pool_commands', 'mailbox_update', 'pool_config', 'mailboxes'}
        for path in product_files():
            module = str(path.relative_to(ROOT))
            if module in PROOF_CALLERS:
                continue
            tree = ast.parse(path.read_text())
            # Only names actually bound to onboarding.<proof module>; a legacy dict called
            # `mailboxes` with .update() is not a caller.
            bound = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module == 'onboarding' or (node.module is None and node.level >= 1)):
                    bound |= {alias.asname or alias.name for alias in node.names if alias.name in proof_modules}
                if isinstance(node, ast.ImportFrom) and node.module and node.module.split('.')[-1] in proof_modules \
                        and any(alias.name in PROOF_FUNCTIONS for alias in node.names):
                    self.fail('%s imports a proof function from %s' % (module, node.module))
                if isinstance(node, ast.Import):
                    bound |= {alias.asname or alias.name.split('.')[-1] for alias in node.names
                              if alias.name.startswith('onboarding.') and alias.name.split('.')[-1] in proof_modules}
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr in PROOF_FUNCTIONS and \
                        isinstance(node.value, ast.Name) and node.value.id in bound:
                    self.fail('%s calls %s.%s' % (module, node.value.id, node.attr))

    def test_frontend_scripts_parse_the_field_once(self):
        for name in ('onboarding-pools.js', 'onboarding.js'):
            self.assertEqual(script_violations((ROOT / 'webui' / 'static' / name).read_text(), name), [])

    def test_guards_catch_forbidden_patterns(self):
        bad_python = {
            'subclass': 'class Proof(ServiceError):\n    pass\n',
            'alias': 'from onboarding.errors import ServiceError as SE\n',
            'splat': 'def f(kw):\n    raise ServiceError(ErrorCode.VERSION_CONFLICT, **kw)\n',
            'assign': 'def f(exc):\n    exc.not_committed = True\n',
            'setattr': "def f(exc):\n    setattr(exc, 'not_committed', True)\n",
            'partial': 'import functools\nP = functools.partial(ServiceError, not_committed=True)\n',
            'misplaced': 'def g():\n    raise ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)\n',
        }
        for name, source in bad_python.items():
            with self.subTest(pattern=name):
                self.assertTrue(python_violations(source, 'onboarding/fixture.py'))
        route = 'def batches(request):\n    try:\n        pass\n    except ServiceError:\n        raise\n'
        self.assertTrue(python_violations(route, 'webui/onboarding_pool_routes.py'))
        bad_sql = {
            'delete': "Q = 'DELETE FROM operation_receipts WHERE id=%s'\n",
            'fingerprint': "Q = 'UPDATE mailbox_registry SET source_fingerprint=%s WHERE id=%s'\n",
            'scope': "Q = 'UPDATE onboarding_tasks SET execution_scope=%s WHERE id=%s'\n",
            'version': "Q = 'UPDATE onboarding_tasks SET version=%s WHERE id=%s'\n",
            'dynamic': "from psycopg import sql\nQ = sql.SQL('UPDATE mailbox_registry SET {} WHERE id=%s')\n",
            'upsert': "Q = 'INSERT INTO mailbox_registry(id,email_norm) VALUES(%s,%s) "
                      "ON CONFLICT (email_norm) DO UPDATE SET source_fingerprint=EXCLUDED.source_fingerprint'\n",
            'alias': "Q = 'UPDATE mailbox_registry AS m SET source_fingerprint=%s WHERE m.id=%s'\n",
            'only': "Q = 'UPDATE ONLY onboarding_tasks SET execution_scope=%s WHERE id=%s'\n",
            'qualified': "Q = 'UPDATE \"rf\".\"operation_receipts\" SET request_hash=%s WHERE id=%s'\n",
            'row-value': "Q = 'UPDATE mailbox_registry SET (group_ref, email_norm) = (%s, %s) WHERE id=%s'\n",
            'row-version': "Q = 'UPDATE onboarding_tasks SET (version) = (%s) WHERE id=%s'\n",
            'delete-only': "Q = 'DELETE FROM ONLY onboarding_tasks WHERE id=%s'\n",
            'truncate': "Q = 'TRUNCATE TABLE audit_events, operation_receipts'\n",
            'merge': "Q = 'MERGE INTO global_configs g USING x ON true WHEN MATCHED THEN DELETE'\n",
            'config-update': "Q = 'UPDATE global_configs SET nonsecret_config=%s'\n",
            'concatenated': "Q = 'UPDATE ' + table + ' SET x=%s'\n",
            'fstring': "Q = f'DELETE FROM {table} WHERE id=%s'\n",
            'format': "Q = 'UPDATE {} SET group_ref=%s'.format(table)\n",
            'direct-sql': "from psycopg.sql import SQL\nQ = SQL('DELETE FROM {} WHERE id=%s')\n",
        }
        for name, source in bad_sql.items():
            with self.subTest(pattern=name):
                self.assertTrue(sql_violations(source, 'onboarding/fixture.py'))
        bad_migrations = {
            'drop-constraint': 'ALTER TABLE mailbox_registry DROP CONSTRAINT mailbox_registry_email_norm_key;\n',
            'drop-index': 'DROP INDEX admin_receipt_request;\n',
            'trigger-assign': 'CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN NEW.version := NEW.version + 2; '
                              'RETURN NEW; END $$ LANGUAGE plpgsql;\n',
            'trigger-equals': 'CREATE FUNCTION g() RETURNS trigger AS $$ BEGIN NEW.email_norm = lower(NEW.email_norm); '
                              'RETURN NEW; END $$ LANGUAGE plpgsql;\n',
            'upsert': 'INSERT INTO operation_receipts(id) VALUES (1) ON CONFLICT DO UPDATE SET idempotency_key = 1;\n',
        }
        for name, source in bad_migrations.items():
            with self.subTest(migration=name):
                self.assertTrue(sql_violations(source, 'onboarding/migrations/fixture.sql'))
        allowed = {
            'fixture.py': "Q = 'UPDATE mailbox_registry SET group_ref=%s,version=version+1 WHERE id=%s'\n"
                          "GRANTS = 'SELECT, INSERT, UPDATE'\nACTION = 'update'\n"
                          "S = 'UPDATE onboarding_tasks SET status=%s,version=version+1 WHERE id=%s AND version=%s'\n",
            'fixture.sql': 'CREATE FUNCTION h() RETURNS trigger AS $$ BEGIN IF NEW.version = OLD.version THEN '
                           "NEW.sale_eligibility := 'INELIGIBLE'; END IF; RETURN NEW; END $$ LANGUAGE plpgsql;\n"
                           'ALTER TABLE secret_objects DROP CONSTRAINT secret_objects_kind_check;\n',
        }
        for name, source in allowed.items():
            with self.subTest(allowed=name):
                self.assertEqual(sql_violations(source, 'onboarding/' + ('migrations/' if name.endswith('.sql') else '') + name), [])
        self.assertTrue(script_violations('a.not_committed; b.not_committed', 'fixture.js'))
