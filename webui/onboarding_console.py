"""Read-only original console integration, without enabling legacy executors.

The source of layout and task metadata remains the original index/scripts module.
No environment, provider, browser profile, subprocess or secret file is read here.
"""
from copy import deepcopy
from html import escape
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from starlette.responses import Response


class _ConsoleHTML(HTMLParser):
    def __init__(self, pool_fragment=None):
        super().__init__(convert_charrefs=False)
        self.pool_fragment = pool_fragment
        self.inserted_pool = False
        self.app_script = False
        self.output = []
        self.styles = {}
        self.specific_styles = []

    def handle_decl(self, declaration):
        self.output.append('<!' + declaration + '>')

    def handle_starttag(self, tag, attrs):
        self._tag(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._tag(tag, attrs, True)

    def _tag(self, tag, attrs, closed):
        attrs = dict(attrs)
        if tag == 'style' or any(key.lower().startswith('on') for key in attrs):
            raise ValueError('inline executable content is not supported')
        if tag == 'link' and attrs.get('rel') == 'icon' and attrs.get('href', '').startswith('data:'):
            return
        if tag == 'body':
            attrs['data-console-mode'] = 'protected'
        style = attrs.pop('style', None)
        if style is not None:
            name = self.styles.setdefault(style, 'console-inline-' + str(len(self.styles)))
            attrs['class'] = (attrs.get('class', '') + ' ' + name).strip()
            if attrs.get('id'):
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', attrs['id']):
                    raise ValueError('unsupported styled element id')
                # Original inline styles outranked ID rules (notably #view-plus).
                # Preserve that precedence without !important: app.js may still
                # select views using element.style.display.
                self.specific_styles.append('#' + attrs['id'] + '.' + name + '{' + style + '}')
        if tag == 'script':
            if attrs.get('src', '').split('?', 1)[0] != '/static/app.js':
                raise ValueError('unexpected script in original console')
            self.output.append('<script src="/static/console-protected.js"></script>')
            self.app_script = True
        rendered = ''.join(' ' + key + ('' if value is None else '="' + escape(value, quote=True) + '"')
                           for key, value in attrs.items())
        self.output.append('<' + tag + rendered + ('/>' if closed else '>'))

    def handle_endtag(self, tag):
        if tag == 'main' and self.pool_fragment is not None:
            if self.inserted_pool:
                raise ValueError('multiple original main elements')
            self.output.append(self.pool_fragment)
            self.inserted_pool = True
        if tag == 'head' and self.pool_fragment is not None:
            self.output.append('<link rel="stylesheet" href="/static/onboarding-shell.css">')
        if tag == 'head':
            self.output.append('<link rel="stylesheet" href="/api/onboarding/console/styles.css">'
                               '<link rel="stylesheet" href="/static/console-protected.css">')
        self.output.append('</' + tag + '>')
        if tag == 'script' and self.app_script and self.pool_fragment is not None:
            self.output.append('<script src="/static/onboarding-pools.js"></script>'
                               '<script src="/static/onboarding-cards.js"></script>')
            self.app_script = False

    def handle_data(self, data):
        self.output.append(data)

    def handle_entityref(self, name):
        self.output.append('&' + name + ';')

    def handle_charref(self, name):
        self.output.append('&#' + name + ';')

    def handle_comment(self, data):
        self.output.append('<!--' + data + '-->')


class _PoolMain(HTMLParser):
    """Extract only template main children, never its independent page shell."""
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.output = []
        self.active = False
        self.found = False
        self.closed = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'main' and attributes.get('id') == 'main':
            if self.found:
                raise ValueError('duplicate pool main')
            self.found = self.active = True
            return
        if self.active:
            if tag in ('script', 'style', 'iframe', 'main') or any(
                    key.lower().startswith('on') or key == 'style' for key in attributes):
                raise ValueError('unsafe pool main content')
            self.output.append(self.get_starttag_text())

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.active:
            if tag == 'main':
                self.active = False
                self.closed = True
            else:
                self.output.append('</' + tag + '>')

    def handle_data(self, data):
        if self.active:
            self.output.append(data)

    def handle_entityref(self, name):
        self.handle_data('&' + name + ';')

    def handle_charref(self, name):
        self.handle_data('&#' + name + ';')

    def handle_comment(self, data):
        self.handle_data('<!--' + data + '-->')


def render_console(source, pool_source=None, pool_mode='synthetic'):
    if pool_mode not in ('synthetic', 'live'):
        raise ValueError('unsupported pool mode')
    fragment = None
    if pool_source is not None:
        pool = _PoolMain()
        pool.feed(pool_source)
        pool.close()
        if not pool.found or not pool.closed or pool.active:
            raise ValueError('pool main is missing or incomplete')
        fragment = ('<section id="view-onboarding" class="onboarding-console" hidden>'
                    '<span id="operator" hidden></span>'
                    '<button id="logout" type="button" hidden></button>'
                    '<div id="main" tabindex="-1" data-pool-mode="' + pool_mode + '">' + ''.join(pool.output) +
                    '</div></section>')
    parser = _ConsoleHTML(fragment)
    parser.feed(source)
    parser.close()
    if fragment is not None and not parser.inserted_pool:
        raise ValueError('original main is missing')
    css = '\n'.join('.' + name + '{' + style + '}' for style, name in parser.styles.items())
    css += '\n' + '\n'.join(parser.specific_styles)
    return ''.join(parser.output), css


_TASK_FIELDS = ('id', 'title', 'category', 'desc', 'warning')
_ARG_FIELDS = ('flag', 'type', 'default', 'help', 'hidden', 'choices', 'labels',
               'countryNames', 'positional')


def catalog(schema):
    """Project already-loaded schema, never import config/read runtime defaults."""
    tasks = []
    for source in schema.SCRIPTS:
        task = {key: deepcopy(source[key]) for key in _TASK_FIELDS if key in source}
        task['args'] = [{key: deepcopy(arg[key]) for key in _ARG_FIELDS if key in arg}
                        for arg in source.get('args', [])]
        # Sensitive entry is disabled in this console; do not deliver defaults
        # if future task schemas accidentally include credentials.
        for arg in task['args']:
            flag = arg.get('flag', '').lower()
            if any(word in flag for word in ('password', 'secret', 'token', 'api-key', 'cookie')):
                if 'default' in arg:
                    arg['default'] = ''
        tasks.append(task)
    links = []
    for source in getattr(schema, 'EXTERNAL_LINKS', []):
        try:
            parsed = urlsplit(source.get('url', ''))
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                continue
            parsed.port
        except ValueError:
            continue
        links.append({key: source[key] for key in ('title', 'desc', 'url') if key in source})
    return {'scripts': tasks, 'links': links, 'embeds': [], 'execution_enabled': False}


def register_routes(app, config, static_dir, schema):
    router = APIRouter()
    static_dir = Path(static_dir)

    def authorize(request):
        from onboarding import security, storage
        from onboarding.errors import ServiceError, ErrorCode
        actor = getattr(request.state, 'onboarding_actor', None)
        if actor is None:
            raise ServiceError(ErrorCode.UNAUTHENTICATED)
        if not config.ready or not config.pool_ready:
            raise ServiceError(ErrorCode.DEPENDENCY_UNAVAILABLE)
        if type(actor) is not security.Actor:
            raise ServiceError(ErrorCode.FORBIDDEN)
        with storage.unit_of_work(config.settings) as conn:
            security.revalidate(conn, actor, 'onboarding:read')

    @router.get('/api/onboarding/console/catalog')
    def console_catalog(request: Request):
        authorize(request)
        return catalog(schema)

    @router.get('/api/onboarding/console/styles.css')
    def console_styles(request: Request):
        authorize(request)
        _, css = render_console((static_dir / 'index.html').read_text(encoding='utf-8'))
        return Response(css, media_type='text/css')

    app.include_router(router)
