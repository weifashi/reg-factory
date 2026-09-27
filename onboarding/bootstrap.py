"""TTY-only local operator creation; no password arguments or environment inputs."""
import argparse
import getpass
import hmac
import sys
import warnings

from .errors import ErrorCode, ServiceError


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default embeds unknown argv values (possibly a password).
        self.print_usage(sys.stderr)
        self.exit(2, 'invalid arguments\n')


def main(argv=None):
    from . import security, settings, storage
    parser = _Parser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True, parser_class=_Parser)
    create = commands.add_parser('operator-create')
    create.add_argument('--settings', required=True)
    create.add_argument('--permission', action='append', choices=sorted(security.PERMISSIONS), required=True)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print('operator-create requires a local TTY', file=sys.stderr)
        return 2
    try:
        config = settings.load_settings(args.settings, role='app')
        username = input('Local operator username: ')
        # A TTY can still fail to disable echo. Never accept getpass fallback.
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('Local operator password: ')
            confirmation = getpass.getpass('Confirm local operator password: ')
        if not hmac.compare_digest(password.encode('utf-8'), confirmation.encode('utf-8')):
            raise ServiceError(ErrorCode.INVALID_INPUT)
        with storage.unit_of_work(config) as conn:
            security.create_operator(conn, username, password, args.permission)
        print('local operator created')
        return 0
    except ServiceError as exc:
        print('operator creation refused: ' + exc.code.value, file=sys.stderr)
        return 2
    except getpass.GetPassWarning:
        print('operator creation refused: secure terminal input unavailable', file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt, UnicodeError):
        print('operator creation cancelled', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
