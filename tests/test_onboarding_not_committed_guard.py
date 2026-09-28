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
# Only the routes call the proof functions; mailboxes.py re-exports update without calling it.
PROOF_CALLERS = {'webui/onboarding_pool_routes.py', 'webui/onboarding_routes.py'}
PROOF_MODULES = {'pool_batches', 'pool_commands', 'mailbox_update', 'pool_config', 'mailboxes'}
FACADES = {('onboarding/mailboxes.py', 'mailbox_update', 'update')}
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
BUMP_SQL = {'{}=%s', 'version=version+1', 'updated_at=clock_timestamp()', ',',
            'UPDATE onboarding_tasks SET {} WHERE id=%s AND version=%s RETURNING id'}


def bump_sql_problems(function):
    """bump_task may compose only its reviewed fragments; its one identifier is the whitelisted loop key."""
    found, parents = [], {}
    for node in ast.walk(function):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    def called(node, name):
        return isinstance(node, ast.Call) and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            or (isinstance(node.func, ast.Name) and node.func.id == name))
    identifiers = []
    for node in ast.walk(function):
        if called(node, 'SQL') and not (len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
                                        and node.args[0].value in BUMP_SQL):
            found.append('unreviewed SQL fragment in bump_task: ' + ast.unparse(node))
        if called(node, 'Identifier'):
            identifiers.append(node)
        # The whitelist check covers `changes` only if nothing rewrites it afterwards.
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] \
            if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else node.targets if isinstance(node, ast.Delete) else []
        for target in targets:
            if (isinstance(target, ast.Name) and target.id == 'changes') or \
                    (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == 'changes'):
                found.append('bump_task rewrites changes: ' + ast.unparse(node).splitlines()[0])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == 'changes' and node.func.attr in ('update', 'setdefault', 'pop', 'popitem',
                                                                           'clear', '__setitem__', '__delitem__'):
            found.append('bump_task rewrites changes: ' + ast.unparse(node))
    for node in identifiers:
        comprehension = parents.get(node)
        while comprehension is not None and not isinstance(comprehension, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            comprehension = parents.get(comprehension)
        generators = comprehension.generators if comprehension is not None else []
        if not (len(node.args) == 1 and isinstance(node.args[0], ast.Name) and node.args[0].id == 'key'
                and len(generators) == 1 and isinstance(generators[0].target, ast.Name) and generators[0].target.id == 'key'
                and isinstance(generators[0].iter, ast.Name) and generators[0].iter.id == 'changes'):
            found.append('column identifier not taken from `for key in changes`: ' + ast.unparse(node))
    if len(identifiers) > 1:
        found.append('bump_task builds %d identifiers (expected one)' % len(identifiers))
    return found


def bump_definitions(tree):
    return sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'bump_task'
               for node in ast.walk(tree))


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
        # An alias defeats every by-name check in this guard.
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and is_service_error(node.value):
            found.append('ServiceError assigned to an alias at %s:%s' % where)
        # A dynamic attribute name can set the flag without ever spelling it.
        if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == 'setattr')
                                           or (isinstance(node.func, ast.Attribute) and node.func.attr == '__setattr__')):
            if not any(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args[:2]):
                found.append('attribute written by a dynamic name at %s:%s' % where)
            elif any(isinstance(arg, ast.Constant) and arg.value == '__dict__' for arg in node.args[:2]):
                found.append('instance __dict__ written at %s:%s' % where)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                if (isinstance(target, ast.Subscript) and is_instance_dict(target.value)) or \
                        (isinstance(target, ast.Attribute) and target.attr == '__dict__'):
                    found.append('instance __dict__ written at %s:%s' % where)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ('update', 'setdefault', '__setitem__') and is_instance_dict(node.func.value):
            found.append('instance __dict__ written at %s:%s' % where)
        # The proof must reach the exception handler untouched: no except of any kind here.
        if isinstance(node, ast.ExceptHandler) and (module, function_of(node)) in ROUTE_HANDLERS:
            found.append('route handler catches exceptions at %s:%s' % (module, function_of(node)))
    return found


def is_instance_dict(node):
    return (isinstance(node, ast.Attribute) and node.attr == '__dict__') or \
           (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'vars')


def proof_site_violations(source, module, function):
    tree = ast.parse(source)
    target = next((n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == function), None)
    if target is None:
        return ['%s:%s not found' % (module, function)]
    # Count every call passing the flag, not only calls spelled ServiceError(...).
    calls = [n for n in ast.walk(target) if isinstance(n, ast.Call) and any(k.arg == 'not_committed' for k in n.keywords)]
    found = [] if len(calls) == 1 else ['%s:%s passes not_committed %d times' % (module, function, len(calls))]
    found += ['%s:%s passes not_committed to a non-ServiceError call' % (module, function)
              for call in calls if not is_service_error(call.func)]
    return found


def caller_violations(source, module):
    if module in PROOF_CALLERS:
        return []
    tree, found, bound, reexported = ast.parse(source), [], set(), set()
    facades = {(origin, name) for owner, origin, name in FACADES if owner == module}
    for node in ast.walk(tree):
        # Only names actually bound to onboarding.<proof module>; a legacy dict called
        # `mailboxes` with .update() is not a caller.
        if isinstance(node, ast.ImportFrom) and (node.module == 'onboarding' or (node.module is None and node.level >= 1)):
            bound |= {alias.asname or alias.name for alias in node.names if alias.name in PROOF_MODULES}
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split('.')[-1] in PROOF_MODULES:
            for alias in node.names:
                if alias.name not in PROOF_FUNCTIONS:
                    continue
                if (node.module.split('.')[-1], alias.name) in facades:
                    reexported.add(alias.asname or alias.name)
                else:
                    found.append('%s imports %s from %s' % (module, alias.name, node.module))
        if isinstance(node, ast.Import):
            bound |= {alias.asname or alias.name.split('.')[-1] for alias in node.names
                      if alias.name.startswith('onboarding.') and alias.name.split('.')[-1] in PROOF_MODULES}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in PROOF_FUNCTIONS and \
                isinstance(node.value, ast.Name) and node.value.id in bound:
            found.append('%s calls %s.%s' % (module, node.value.id, node.attr))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in reexported:
            found.append('%s calls the re-exported %s' % (module, node.func.id))
    return found


# A table as written in SQL: optional ONLY, optional (quoted) schema, (quoted) name.
TABLE = r'(?:ONLY\s+)?(?:"?\w+"?\s*\.\s*)?"?(\w+)"?'
UPDATE_HEAD = re.compile(r'\bUPDATE\s+' + TABLE + r'(?:\s*\*)?(?:\s+(?:AS\s+)?(?!SET\b)"?(\w+)"?)?\s+SET\b', re.I)
# Any UPDATE of a table; a protected one that UPDATE_HEAD cannot parse is reported, never skipped.
UPDATE_ANY = re.compile(r'\bUPDATE\s+' + TABLE, re.I)
UPSERT_HEAD = re.compile(r'\bINSERT\s+INTO\s+' + TABLE + r'[^;]*?\bON\s+CONFLICT\b[^;]*?\bDO\s+UPDATE\s+SET\b',
                         re.I | re.S)
DELETE_FROM = re.compile(r'\bDELETE\s+FROM\s+' + TABLE, re.I)
MERGE_INTO = re.compile(r'\bMERGE\s+INTO\s+' + TABLE, re.I)
TRUNCATE = re.compile(r'\bTRUNCATE\b([^;]*)', re.I)
DROP_TABLE = re.compile(r'\bDROP\s+TABLE\b([^;]*)', re.I)
ALTER_TABLE = re.compile(r'\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?' + TABLE + r'([^;]*)', re.I | re.S)
# Dropping a constraint or a column (COLUMN is optional in SQL); DROP NOT NULL/DEFAULT/IDENTITY/EXPRESSION
# only relax a column definition.
DROPPING = re.compile(r'\bDROP\s+(?!NOT\s+NULL\b|DEFAULT\b|IDENTITY\b|EXPRESSION\b)', re.I)
DROP_INDEX = re.compile(r'\bDROP\s+INDEX\b', re.I)
# plpgsql assignment statements (":=" or "="), not comparisons inside IF conditions.
NEW_ASSIGN = re.compile(r'(?:^|;|\bBEGIN\b|\bTHEN\b|\bELSE\b|\bLOOP\b)\s*NEW\s*\.\s*"?(\w+)"?\s*:?=(?!=)', re.I)
# The whole INTO target list, so `INTO NEW.status, NEW.version` checks every target.
INTO_TARGETS = re.compile(r'\bINTO\s+(?:STRICT\s+)?((?:NEW\s*\.\s*"?\w+"?|[\w"]+)(?:\s*,\s*(?:NEW\s*\.\s*"?\w+"?|[\w"]+))*)', re.I)
NEW_FIELD = re.compile(r'NEW\s*\.\s*"?(\w+)"?', re.I)
# Row-lock clauses are reads: `FOR UPDATE ' + mode` must not look like an UPDATE of table {}.
LOCK_CLAUSE = re.compile(r'\bFOR\s+(?:NO\s+KEY\s+)?UPDATE\b', re.I)
# A write whose target table is not a literal: concatenation, f-string or str.format placeholder.
OPEN_TABLE = re.compile(r'(?:^|;)\s*(?:UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|MERGE\s+INTO)\s+(?:ONLY\s+)?$', re.I)
PLACEHOLDER_TABLE = re.compile(r'\b(?:UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|MERGE\s+INTO)\s+(?:ONLY\s+)?'
                               r'(?:\{[^}]*\}|%s|%\(\w+\)s)', re.I)
PLACEHOLDER = re.compile(r'\{[^}]*\}|%s|%\(\w+\)s')
WRITE_WORD = re.compile(r'\b(?:UPDATE|DELETE|TRUNCATE|MERGE)\b', re.I)
APPEND_ONLY = {'global_configs'}
TRIGGER_IMMUTABLE = set().union(*IMMUTABLE.values()) | {'version'}
BUMP_TASK = ('onboarding/repository.py', 'bump_task')


def quote_end(text, index):
    """Index of the quote closing the literal opened at index, or None when it never closes.

    E'...' strings take backslash escapes; a doubled quote is always an escaped quote.
    """
    char = text[index]
    backslash = char == "'" and index > 0 and text[index - 1] in 'eE' and not (
        index > 1 and (text[index - 2].isalnum() or text[index - 2] == '_'))
    end = index + 1
    while end < len(text):
        if backslash and text[end] == '\\':
            end += 2
        elif text[end] == char and text[end + 1:end + 2] == char:
            end += 2
        elif text[end] == char:
            return end
        else:
            end += 1
    return None


def scan(text, start=0):
    """(index, char, depth) outside quoted strings, quoted identifiers and dollar-quoted bodies.

    depth counts ( and [. An unterminated quote or dollar body yields one final (index, '', -1):
    a negative depth always means the text cannot be parsed reliably.
    """
    depth, index = 0, start
    while index < len(text):
        char = text[index]
        if char in "'\"":
            end = quote_end(text, index)
            if end is None:
                yield index, '', -1
                return
            index = end + 1
            continue
        dollar = re.match(r'\$(\w*)\$', text[index:])
        if dollar:
            close = text.find(dollar.group(0), index + len(dollar.group(0)))
            if close < 0:
                yield index, '', -1
                return
            index = close + len(dollar.group(0))
            continue
        depth += (char in '([') - (char in ')]')
        yield index, char, depth
        index += 1


def strip_comments(text):
    """Drop -- and /* */ comments outside quoted strings and identifiers; '--' inside a literal stays.

    Dollar-quoted bodies are function code, so comments inside them are dropped as well.
    """
    kept, index = [], 0
    while index < len(text):
        char = text[index]
        if char in "'\"":
            end = quote_end(text, index)
            end = len(text) - 1 if end is None else end
            kept.append(text[index:end + 1]); index = end + 1
        elif text.startswith('--', index):
            close = text.find('\n', index)
            index = len(text) if close < 0 else close
            kept.append(' ')
        elif text.startswith('/*', index):
            close = text.find('*/', index + 2)
            index = len(text) if close < 0 else close + 2
            kept.append(' ')
        else:
            kept.append(char); index += 1
    return ''.join(kept)


def set_clause(text, start):
    """The assignment list after SET, up to a top-level WHERE/RETURNING/FROM or the statement end."""
    end = len(text)
    for index, char, depth in scan(text, start):
        if depth <= 0 and (char == ';' or re.match(r'\s(?:WHERE|RETURNING|FROM)\b', text[index:index + 11], re.I)):
            end = index
            break
    return text[start:end].strip()


def split_top(text, separator):
    """Top-level parts, or None when the brackets do not balance."""
    parts, begin, depth = [], 0, 0
    for index, char, depth in scan(text):
        if depth < 0:
            return None
        if char == separator and depth == 0:
            parts.append(text[begin:index]); begin = index + 1
    return None if depth != 0 else parts + [text[begin:]]


def assignments(clause):
    """(target, value) per top-level SET item, or None when the list cannot be parsed."""
    parts, pairs = split_top(clause, ','), []
    if parts is None:
        return None
    for part in parts:
        target = None
        for index, char, depth in scan(part):
            if char == '=' and depth == 0 and part[index - 1:index] not in ('<', '>', '!', '=') \
                    and part[index + 1:index + 2] != '=':
                target, part = part[:index].strip(), part[index + 1:]
                break
        pairs.append((target, part.strip()))
    return pairs


def target_columns(target):
    names = target.strip()[1:-1].split(',') if target.strip().startswith('(') else [target]
    return {name.strip().split('.')[-1].strip().strip('"').lower() for name in names}


def assignment_violations(verb, table, clause, module, whitelisted_list=False, alias=None):
    found, pairs = [], assignments(clause)
    if table in APPEND_ONLY:
        found.append('%s modifies append-only %s in %s' % (verb, table, module))
    if pairs is None:
        return found + ['%s %s with an unparseable SET list in %s' % (verb, table, module)]
    # Only literal column targets can be checked; bump_task alone may pass its whitelisted {} list.
    dynamic = not clause or any(
        (target is None and PLACEHOLDER.search(value) and not (whitelisted_list and value == '{}'))
        or (target is not None and PLACEHOLDER.search(target)) for target, value in pairs)
    if dynamic:
        found.append('%s %s with a non-literal column list in %s' % (verb, table, module))
    columns = set().union(*(target_columns(target) for target, _ in pairs if target is not None))
    for column in sorted(columns & IMMUTABLE[table]):
        found.append('%s %s SET %s in %s' % (verb, table, column, module))
    # version may only read its own row: version+1, <table>.version+1 or <alias>.version+1 (never EXCLUDED).
    own = {table} | ({alias.lower()} if alias else set())
    increments = [re.fullmatch(r'(?:"?(\w+)"?\s*\.\s*)?"?version"?\s*\+\s*1', value, re.I)
                  for target, value in pairs if target is not None and target_columns(target) == {'version'}]
    if table in ('mailbox_registry', 'onboarding_tasks') and 'version' in columns and not (
            increments and all(match and (match.group(1) is None or match.group(1).lower() in own) for match in increments)):
        found.append('non-increment version assignment on %s in %s' % (table, module))
    return found


def flatten_add(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return flatten_add(node.left) + flatten_add(node.right)
    return [node]


def rebuilt(node):
    """String text with {} for every non-literal part, or None when nothing literal is known."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return ''.join(part.value if isinstance(part, ast.Constant) else '{}' for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        parts = flatten_add(node)
        texts = [rebuilt(part) if is_text(part) else None for part in parts]
        if any(text is not None for text in texts):
            return ''.join('{}' if text is None else text for text in texts)
    return None


def is_text(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)) or isinstance(node, ast.JoinedStr)


def text_nodes(node):
    """The nodes a rebuilt text consumed: the '+' skeleton, its literal parts and f-string literals.
    Non-literal operands (calls, % formatting, f-string expressions) stay uncovered and are scanned."""
    if isinstance(node, ast.JoinedStr):
        return [node] + [part for part in node.values if isinstance(part, ast.Constant)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [node] + [child for side in (node.left, node.right) for child in
                         (text_nodes(side) if is_text(side) or (isinstance(side, ast.BinOp) and isinstance(side.op, ast.Add)) else [])]
    return [node]


def sql_texts(source, module):
    """(text, function) pairs; f-strings and '+' chains are rebuilt whole, with {} for non-literals."""
    if module.endswith('.sql'):
        return [(source, None)]
    tree = ast.parse(source)
    function_of, covered, texts = enclosing(tree), set(), []
    for node in ast.walk(tree):
        if isinstance(node, (ast.JoinedStr, ast.BinOp)) and node not in covered:
            text = rebuilt(node)
            if text is not None:
                texts.append((text, function_of(node)))
                covered.update(text_nodes(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in covered:
            texts.append((node.value, function_of(node)))
    return texts


def module_strings(tree):
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            text = rebuilt(node.value)
            if text is not None:
                values[node.targets[0].id] = text
    return values


def sql_violations(source, module):
    found = []
    for raw, function in sql_texts(source, module):
        text = LOCK_CLAUSE.sub('FOR LOCK', strip_comments(raw))
        heads = {match.start() for match in UPDATE_HEAD.finditer(text)}
        for match in UPDATE_ANY.finditer(text):
            if match.group(1).lower() in IMMUTABLE and match.start() not in heads:
                found.append('unrecognized UPDATE head for %s in %s' % (match.group(1).lower(), module))
        for verb, pattern in (('UPDATE', UPDATE_HEAD), ('UPSERT', UPSERT_HEAD)):
            for match in pattern.finditer(text):
                table = match.group(1).lower()
                if table in IMMUTABLE:
                    alias = match.group(2) if verb == 'UPDATE' else None
                    found += assignment_violations(verb, table, set_clause(text, match.end()), module,
                                                   whitelisted_list=(module, function) == BUMP_TASK, alias=alias)
        for verb, pattern in (('DELETE FROM', DELETE_FROM), ('MERGE INTO', MERGE_INTO)):
            for table in pattern.findall(text):
                if table.lower() in IMMUTABLE:
                    found.append('%s %s in %s' % (verb, table.lower(), module))
        for table, actions in ALTER_TABLE.findall(text):
            if table.lower() in IMMUTABLE and DROPPING.search(actions):
                found.append('ALTER TABLE %s drops a constraint or column in %s' % (table.lower(), module))
        for verb, pattern in (('TRUNCATE', TRUNCATE), ('DROP TABLE', DROP_TABLE)):
            for targets in pattern.findall(text):
                words = {word.lower() for word in re.findall(r'\w+', targets)}
                for table in sorted(words & set(IMMUTABLE)):
                    found.append('%s %s in %s' % (verb, table, module))
                if verb == 'TRUNCATE' and 'cascade' in words:
                    found.append('TRUNCATE ... CASCADE can reach the proof tables in %s' % module)
        if DROP_INDEX.search(text):
            found.append('DROP INDEX in %s (uniqueness behind the proof must be re-reviewed)' % module)
        assigned = NEW_ASSIGN.findall(text)
        for targets in INTO_TARGETS.findall(text):
            assigned += NEW_FIELD.findall(targets)
        for column in assigned:
            if column.lower() in TRIGGER_IMMUTABLE:
                found.append('trigger assigns NEW.%s in %s' % (column.lower(), module))
        if OPEN_TABLE.search(text) or PLACEHOLDER_TABLE.search(text):
            found.append('write with a non-literal table in %s' % module)
    if not module.endswith('.sql'):
        tree = ast.parse(source)
        function_of, constants = enclosing(tree), module_strings(tree)
        for node in ast.walk(tree):
            direct = isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Attribute) and node.func.attr == 'SQL')
                or (isinstance(node.func, ast.Name) and node.func.id == 'SQL'))
            if not direct or not node.args or (module, function_of(node)) == BUMP_TASK:
                continue
            first = node.args[0]
            text = constants.get(first.id) if isinstance(first, ast.Name) else rebuilt(first)
            if text is None or WRITE_WORD.search(text):
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
            self.assertEqual(proof_site_violations((ROOT / module).read_text(), module, function), [])

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
        self.assertEqual(bump_sql_problems(bump), [])
        for name, extra in (('fragment', "    assignments += [sql.SQL('execution_scope=%s')]\n"),
                            ('identifier', "    assignments += [sql.SQL('{}=%s').format(sql.Identifier('execution_scope'))]\n"),
                            ('bare-sql', "    assignments += [SQL('execution_scope=%s')]\n"),
                            ('rewritten-changes', "    changes['execution_scope'] = 'pool'\n"
                                                  "    assignments = [sql.SQL('{}=%s').format(sql.Identifier(key)) for key in changes]\n"),
                            ('rebound-changes', "    changes = dict(changes, execution_scope='pool')\n"
                                                "    assignments = [sql.SQL('{}=%s').format(sql.Identifier(key)) for key in changes]\n"),
                            ('literal-keys', "    assignments = [sql.SQL('{}=%s').format(sql.Identifier(key)) for key in ('execution_scope',)]\n")):
            with self.subTest(smuggled=name):
                source = 'def bump_task(conn, task, **changes):\n    assignments = []\n' + extra
                self.assertTrue(bump_sql_problems(ast.parse(source).body[0]))
        self.assertEqual(bump_definitions(tree), 1)
        twice = 'def bump_task(conn, task, **changes):\n    pass\n\n\ndef bump_task(conn, task, **changes):\n    pass\n'
        self.assertEqual(bump_definitions(ast.parse(twice)), 2)

    def test_proof_functions_called_only_from_their_routes(self):
        found = []
        for path in product_files():
            found += caller_violations(path.read_text(), str(path.relative_to(ROOT)))
        self.assertEqual(found, [])

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
        more_python = {
            'assign-alias': 'from onboarding.errors import ServiceError\nSE = ServiceError\n',
            'attribute-alias': 'from onboarding import errors\nSE = errors.ServiceError\n',
            'object-setattr': 'def f(exc, name):\n    object.__setattr__(exc, name, True)\n',
            'dynamic-setattr': 'def f(exc, name):\n    setattr(exc, name, True)\n',
            'dict-write': "def f(exc):\n    exc.__dict__['not_' + 'committed'] = True\n",
            'vars-write': 'def f(exc, key):\n    vars(exc)[key] = True\n',
            'dict-update': 'def f(exc, values):\n    exc.__dict__.update(values)\n',
            'dict-replace': 'def f(exc, flags):\n    exc.__dict__ = {**exc.__dict__, **flags}\n',
            'dict-merge': 'def f(exc, flags):\n    exc.__dict__ |= flags\n',
            'setattr-dict': "def f(exc, flags):\n    setattr(exc, '__dict__', flags)\n",
        }
        for name, source in more_python.items():
            with self.subTest(pattern=name):
                self.assertTrue(python_violations(source, 'onboarding/fixture.py'))
        for clause in ('except ServiceError:', 'except Exception:', 'except:', 'except (ValueError, BaseException):'):
            with self.subTest(route=clause):
                route = 'def batches(request):\n    try:\n        pass\n    ' + clause + '\n        raise\n'
                self.assertTrue(python_violations(route, 'webui/onboarding_pool_routes.py'))
        twice = ('from onboarding.errors import ErrorCode, ServiceError\nSE = ServiceError\n'
                 'def update(stale):\n    if stale:\n        raise ServiceError(ErrorCode.VERSION_CONFLICT, not_committed=True)\n'
                 '    raise SE(ErrorCode.VERSION_CONFLICT, not_committed=True)\n')
        self.assertTrue(proof_site_violations(twice, 'onboarding/mailbox_update.py', 'update'))
        callers = {
            ('onboarding/pool_batches.py', 'proof module calls another'):
                'from . import pool_commands\ndef f(*args):\n    return pool_commands.pause(*args)\n',
            ('onboarding/pool_batches.py', 'imports a proof function'): 'from .pool_config import replace\n',
            ('onboarding/mailboxes.py', 'facade called'): 'from .mailbox_update import update\ndef g():\n    return update()\n',
        }
        for (module, name), source in callers.items():
            with self.subTest(caller=name):
                self.assertTrue(caller_violations(source, module))
        self.assertEqual(caller_violations('from .mailbox_update import update\n', 'onboarding/mailboxes.py'), [])
        frozen = "class K:\n    def __init__(self, directory):\n        object.__setattr__(self, 'directory', directory)\n"
        self.assertEqual(python_violations(frozen, 'onboarding/fixture.py'), [])
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
            'fstring-column': "Q = f'UPDATE mailbox_registry SET {col}=%s WHERE id=%s'\n",
            'format-column': "Q = 'UPDATE mailbox_registry SET {}=%s WHERE id=%s'.format(col)\n",
            'concat-column': "Q = 'UPDATE mailbox_registry SET ' + col + '=%s WHERE id=%s'\n",
            'percent-column': "Q = 'UPDATE mailbox_registry SET %s=%%s WHERE id=%%s' % col\n",
            'indirect-sql': "from psycopg import sql\nQ = 'UPDATE onboarding_tasks SET status=%s WHERE id=%s'\nS = sql.SQL(Q)\n",
            'unresolved-sql': "from psycopg import sql\ndef f(query):\n    return sql.SQL(query)\n",
            'subquery-where': "Q = 'UPDATE mailbox_registry SET group_ref=(SELECT g FROM x WHERE y=1), email_norm=%s WHERE id=%s'\n",
            'comment-hidden': "Q = 'UPDATE mailbox_registry SET group_ref=%s, -- keep\\n email_norm=%s WHERE id=%s'\n",
            'format-then-concat': "Q = 'DELETE FROM operation_receipts WHERE id={}'.format(x) + ' RETURNING id'\n",
            'percent-then-concat': "Q = 'UPDATE mailbox_registry SET email_norm=%s' % v + ' WHERE id=%s'\n",
            'fstring-interpolated': "Q = f\"{conn.execute('DELETE FROM operation_receipts WHERE id=%s', (x,))}\"\n",
            'quoted-dynamic-column': "Q = 'UPDATE mailbox_registry SET \\\"{}\\\"=%s WHERE id=%s'\n",
            'row-dynamic-column': "Q = 'UPDATE mailbox_registry SET (group_ref,{})=(%s,%s) WHERE id=%s'\n",
            'percent-table': "Q = 'UPDATE %s SET group_ref=%%s' % table\n",
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
            'comment-before-assign': 'CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN\n  -- keep it\n  NEW.version := 1; '
                                     'RETURN NEW; END $$ LANGUAGE plpgsql;\n',
            'select-into': 'CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN SELECT 1 INTO NEW.version; '
                           'RETURN NEW; END $$ LANGUAGE plpgsql;\n',
            'truncate-cascade': 'TRUNCATE operators CASCADE;\n',
            'drop-table': 'DROP TABLE IF EXISTS operation_receipts;\n',
            'drop-column-implicit': 'ALTER TABLE mailbox_registry DROP source_fingerprint;\n',
            'into-second-target': 'CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN SELECT 1, 2 INTO NEW.status, NEW.version; '
                                  'RETURN NEW; END $$ LANGUAGE plpgsql;\n',
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
                           'ALTER TABLE secret_objects DROP CONSTRAINT secret_objects_kind_check;\n'
                           'ALTER TABLE operation_receipts ALTER COLUMN task_id DROP NOT NULL;\n',
            'grants.py': "from psycopg import sql\nGRANTS = 'SELECT, INSERT'\n"
                         "S = sql.SQL('GRANT ' + GRANTS + ' ON {} TO rf_onboarding_app')\n",
        }
        for name, source in allowed.items():
            with self.subTest(allowed=name):
                self.assertEqual(sql_violations(source, 'onboarding/' + ('migrations/' if name.endswith('.sql') else '') + name), [])
        bump = "from psycopg import sql\ndef bump_task(fields):\n    return sql.SQL('UPDATE onboarding_tasks SET {}, version=version+1 WHERE id=%s')\n"
        self.assertEqual(sql_violations(bump, 'onboarding/repository.py'), [])
        self.assertTrue(sql_violations(bump.replace('bump_task', 'other_task'), 'onboarding/repository.py'))
        # The bump_task exemption covers only its whitelisted {} column list, not literal assignments.
        smuggled = bump.replace('SET {}, version', 'SET {}, execution_scope=%s, version')
        self.assertTrue(sql_violations(smuggled, 'onboarding/repository.py'))
        must_not_flag = {
            'value-side-function': "Q = 'UPDATE mailbox_registry SET group_ref=jsonb_set(x,%s,%s,true) WHERE id=%s'\n",
            'row-lock-suffix': "Q = 'SELECT id FROM onboarding_tasks WHERE id=%s FOR UPDATE ' + mode\n",
            'array-value': "Q = 'UPDATE mailbox_registry SET group_ref=ARRAY[%s,%s]::text WHERE id=%s'\n",
            'alias-version': "Q = 'UPDATE onboarding_tasks t SET version=t.version+1 WHERE t.id=%s'\n",
            'table-version': "Q = 'UPDATE onboarding_tasks SET version=onboarding_tasks.version+1 WHERE id=%s'\n",
            'quoted-alias-version': 'Q = \'UPDATE onboarding_tasks "t" SET version="t".version+1 WHERE "t".id=%s\'\n',
        }
        must_flag = {
            'excluded-version': "Q = 'INSERT INTO onboarding_tasks(id) VALUES(%s) ON CONFLICT (id) DO UPDATE SET version=EXCLUDED.version+1'\n",
            'other-table-version': "Q = 'UPDATE onboarding_tasks SET version=s.version+1 FROM src s WHERE onboarding_tasks.id=s.id'\n",
            'quoted-paren': 'Q = "UPDATE mailbox_registry SET group_ref=\')\', email_norm=%s WHERE id=%s"\n',
            'quoted-where': 'Q = "UPDATE mailbox_registry SET group_ref=\' WHERE \', email_norm=%s WHERE id=%s"\n',
            'e-string-escape': 'Q = "UPDATE mailbox_registry SET group_ref=E\'\\\\\'\', email_norm=%s WHERE id=%s"\n',
            'dash-literal': 'Q = "UPDATE mailbox_registry SET group_ref=\'--\', email_norm=%s WHERE id=%s"\n',
            'unclosed-paren': "Q = 'UPDATE mailbox_registry SET group_ref=lower(%s, email_norm=%s WHERE id=%s'\n",
            'extra-close-paren': "Q = 'UPDATE mailbox_registry SET group_ref=lower(%s)), email_norm=%s WHERE id=%s'\n",
            'unterminated-quote': 'Q = "UPDATE mailbox_registry SET group_ref=\'x, email_norm=%s WHERE id=%s"\n',
            'quoted-alias': 'Q = \'UPDATE mailbox_registry "m" SET email_norm=%s WHERE "m".id=%s\'\n',
            'as-quoted-alias': 'Q = \'UPDATE mailbox_registry AS "m" SET email_norm=%s WHERE "m".id=%s\'\n',
            'star-head': "Q = 'UPDATE mailbox_registry * SET email_norm=%s WHERE id=%s'\n",
            'unrecognized-head': "Q = 'UPDATE mailbox_registry m WITH x SET email_norm=%s'\n",
        }
        for name, source in must_flag.items():
            with self.subTest(pattern=name):
                self.assertTrue(sql_violations(source, 'onboarding/fixture.py'))
        for name, source in must_not_flag.items():
            with self.subTest(allowed=name):
                self.assertEqual(sql_violations(source, 'onboarding/fixture.py'), [])
        self.assertTrue(script_violations('a.not_committed; b.not_committed', 'fixture.js'))
