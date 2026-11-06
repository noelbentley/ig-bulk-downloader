"""Anonymous CDN connection reuse for a strictly sequential engine instance."""
import threading


class BorrowedSession:
    def __init__(self, pool):
        self.pool = pool
        self._desktop_underlying_session = pool.session

    def __enter__(self):
        if not self.pool.borrow_lock.acquire(blocking=False):
            raise RuntimeError("Media session cannot be borrowed concurrently")
        # Preserve native per-request cookie behavior; only the TCP pools survive.
        self.pool.session.cookies.clear()
        self.pool.session.cookies.update(self.pool.cookies)
        return self.pool.session

    def __exit__(self, kind, value, traceback):
        self.pool.session.cookies.clear()
        self.pool.session.cookies.update(self.pool.cookies)
        self.pool.borrow_lock.release()
        if kind is not None:
            self.pool.close()
        return False


class AnonymousMediaPool:
    def __init__(self, mode="pooled"):
        if mode not in ("pooled", "alternate"):
            raise ValueError("Invalid connection mode")
        self.mode = mode
        self.session = None
        self.cookies = None
        self.active = False
        self.pooled_request = False
        self.sequence = 0
        self.borrow_lock = threading.Lock()
        self.pending_response = None

    def install(self, context):
        create_anonymous = context.get_anonymous_session
        def anonymous():
            if not self.active or not self.pooled_request:
                session = create_anonymous()
                session._desktop_media_mode = "fresh"
                return session
            if self.session is None:
                self.session = create_anonymous()
                self.session._desktop_media_mode = "pooled"
                self.cookies = self.session.cookies.get_dict().copy()
            return BorrowedSession(self)
        context.get_anonymous_session = anonymous
        for name in ("get_raw", "head"):
            original = getattr(context, name, None)
            if original:
                def media(*args, _original=original, _get=name == "get_raw", **kwargs):
                    # The sequential engine has finished the prior file at this boundary.
                    # Close skipped/unconsumed responses so they cannot strand pool leases.
                    self.close_response()
                    previous = self.active, self.pooled_request
                    self.active = True
                    if _get:
                        self.sequence += 1
                    self.pooled_request = self.mode == "pooled" or self.sequence % 2 == 0
                    try:
                        response = _original(*args, **kwargs)
                        if _get and self.pooled_request:
                            self.pending_response = response
                        return response
                    finally:
                        self.active, self.pooled_request = previous
                setattr(context, name, media)

    def close_response(self):
        if self.pending_response is not None:
            self.pending_response.close()
            self.pending_response = None

    def close(self):
        self.close_response()
        if self.session is not None:
            self.session.close()
            self.session = None
