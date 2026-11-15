"""Owner-scoped mailbox observations under the explicit live read policy.

The original whitelist DTO, filters and signed keyset cursor are shared, not
its SyntheticPoolPolicy admission. No graph, credential, source file, current
configuration or provider is read. UNKNOWN/history and logical occupancy remain
observations; listing a mailbox never grants permission to use it for live work.
"""
import hmac

from . import live_read, mailbox_read, security, storage
from .errors import ErrorCode, ServiceError
from .request_mac import RequestMac


def _probe(mac, actor):
    return mac.request_digest('live.mailbox.list.keycheck.v1', actor.operator_id, b'live-mailbox:stable-key')


def list_page(settings, actor, *, platform=None, search='', group_ref=None,
              health=None, occupied=None, cursor=None, limit=50, policy, mac):
    """Observe one bounded owner page, returning only after confirmed COMMIT.

    READ COMMITTED pagination is not a frozen cross-request snapshot. An expired
    logical hold remains occupied; even an unoccupied row is not live admission.
    This never refreshes idle sessions, changes state or consumes a credential.
    """
    live_read._context(settings, actor, policy)
    if type(mac) is not RequestMac:
        raise ServiceError(ErrorCode.INVALID_INPUT)
    try:
        filters = mailbox_read._filters(platform, search, group_ref, health, occupied, limit)
        if cursor is not None and (type(cursor) is not str or len(cursor) > 512):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise ServiceError(ErrorCode.INVALID_INPUT) from None
    policy._validate()
    probe = _probe(mac, actor)
    with storage.unit_of_work(settings) as conn:
        policy._connection(conn)
        security.revalidate(conn, actor, 'onboarding:read')
        # Pass original input to the shared canonicalizer: Unicode lowercase
        # expansion may legitimately grow after its original length validation.
        digest = mailbox_read._list_filter_mac(settings, actor.operator_id,
            platform=platform, search=search, group_ref=group_ref, health=health,
            occupied=occupied, limit=limit, mac=mac)
        anchor = ((None, None) if cursor is None else
                  mailbox_read._decode_list_cursor(cursor, actor.operator_id, digest, mac=mac))
        params = {**filters, 'owner': actor.operator_id, 'anchor_time': anchor[0],
                  'anchor_id': anchor[1], 'fetch_limit': limit + 1}
        rows = conn.execute(mailbox_read._DATA_SQL, params).fetchall()
        # A corrupt sentinel is an error, not permission to omit it from history.
        items = tuple(mailbox_read._mailbox_dto(row, filters['platform']) for row in rows)
        next_cursor = None
        if len(items) > limit:
            last = rows[limit - 1]
            next_cursor = mailbox_read._encode_list_cursor(actor.operator_id, digest,
                last[10], str(last[0]), mac=mac)
        result = mailbox_read.Page(items[:limit], next_cursor)
        policy._connection(conn)
        if not hmac.compare_digest(probe, _probe(mac, actor)):
            raise ServiceError(ErrorCode.SECRET_UNAVAILABLE)
        security.revalidate(conn, actor, 'onboarding:read')
    return result
