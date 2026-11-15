"""Google workbench markup contracts for the real unified console renderer."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'webui/static'


class Tree(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.nodes = []
        self.stack = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        node = dict(tag=tag, attrs=dict(attrs), parents=tuple(self.stack))
        self.nodes.append(node)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                break

    def get(self, ident):
        return next((node for node in self.nodes if node['attrs'].get('id') == ident), None)


class GoogleRunLayoutTests(unittest.TestCase):
    def setUp(self):
        from webui.onboarding_console import render_console
        source = (STATIC / 'onboarding-pools.html').read_text()
        composite, _ = render_console((STATIC / 'index.html').read_text(), pool_source=source)
        self.pages = [Tree(source), Tree(composite)]

    def required(self, tree, ident):
        node = tree.get(ident)
        self.assertIsNotNone(node, f'missing Google run DOM: {ident}')
        return node

    def parents(self, node):
        return [parent['attrs'].get('id') for parent in node['parents']]

    def test_run_button_and_mode_have_safe_form_semantics(self):
        for tree in self.pages:
            button = self.required(tree, 'run-google')
            self.assertEqual(button['attrs'].get('type'), 'button')
            self.assertIn('disabled', button['attrs'])
            self.assertIn('batch-form', self.parents(button))
            self.assertNotIn('google-advanced', self.parents(button))
            mode = self.required(tree, 'google-execution-mode')
            options = [n for n in tree.nodes if n['tag'] == 'option' and mode in n['parents']]
            self.assertEqual([n['attrs'].get('value') for n in options], ['synthetic', 'real'])
            self.assertIn('selected', options[0]['attrs'])
            self.assertIn('disabled', options[1]['attrs'])

    def test_advanced_preserves_old_controls_without_nested_forms(self):
        for tree in self.pages:
            advanced = self.required(tree, 'google-advanced')
            self.assertEqual(advanced['tag'], 'details')
            self.assertNotIn('open', advanced['attrs'])
            for ident in ['preflight', 'confirm-batch', 'preflight-result']:
                self.assertIn('google-advanced', self.parents(self.required(tree, ident)))
            self.assertIn('batch-form', self.parents(self.required(tree, 'preflight')))
            self.assertEqual(self.required(tree, 'preflight')['attrs']['type'], 'submit')
            for node in tree.nodes:
                if node['tag'] == 'form':
                    self.assertFalse(any(p['tag'] == 'form' for p in node['parents']))

    def test_logs_are_accessible_and_history_does_not_hide_commands(self):
        for tree in self.pages:
            log = self.required(tree, 'google-log')
            self.assertEqual(log['attrs'].get('role'), 'log')
            self.assertEqual(log['attrs'].get('aria-live'), 'polite')
            for ident in ['google-run-status', 'google-log-state', 'refresh-google-log', 'worker-log-notice']:
                self.assertIn('tasks-card', self.parents(self.required(tree, ident)))
            history = self.required(tree, 'task-history')
            self.assertEqual(history['tag'], 'details')
            self.assertNotIn('open', history['attrs'])
            for ident in ['task-filter-form', 'refresh-tasks', 'task-list', 'task-list-state', 'task-next']:
                self.assertIn('task-history', self.parents(self.required(tree, ident)))
            for ident in ['task-facts', 'pause-task', 'cancel-task', 'recheck-task', 'refresh-task']:
                self.assertNotIn('task-history', self.parents(self.required(tree, ident)))

    def test_both_renderings_keep_unique_ids(self):
        for tree in self.pages:
            ids = [node['attrs']['id'] for node in tree.nodes if 'id' in node['attrs']]
            self.assertEqual(len(ids), len(set(ids)))
            for ident in ['batch-result', 'selected-count', 'requested-count', 'selection', 'command-receipt', 'task-empty']:
                self.required(tree, ident)
