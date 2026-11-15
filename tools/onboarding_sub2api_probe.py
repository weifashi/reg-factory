"""Secret-redacted CLI for bounded admin connectivity only, not execution."""
import argparse
import json
from pathlib import Path
import sys

# Make a direct script invocation independent of a caller's PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from onboarding.sub2api_probe import empty_result, run_probe


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's message can repeat an unknown option containing a secret.
        raise ValueError('INVALID_ARGUMENTS')


def main(argv=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False, add_help=False)
    parser.add_argument('--config', required=True)
    parser.add_argument('--group', required=True)
    parser.add_argument('--allow-loopback-http', action='store_true')
    try:
        args = parser.parse_args(argv)
        result = run_probe(args.config, args.group, allow_loopback_http=args.allow_loopback_http)
    except ValueError:
        result = empty_result('INVALID_ARGUMENTS')
    except (Exception, KeyboardInterrupt):
        result = empty_result('INTERNAL_ERROR')
    print(json.dumps(result, sort_keys=True))
    return 0 if result['reason_code'] == 'READY' else 1


if __name__ == '__main__':
    sys.exit(main())
