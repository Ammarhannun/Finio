"""Authenticate and admit statement uploads before reading or spooling a body."""
from threading import Lock

import anyio
from fastapi import HTTPException
from starlette.responses import JSONResponse

from api.deps import authenticate


class UploadAuthMiddleware:
    def __init__(self, app, max_uploads=2):
        self.app = app
        self.max_uploads = max_uploads
        self.active_users = set()
        self.lock = Lock()

    async def __call__(self, scope, receive, send):
        if (scope['type'] == 'http' and scope['method'] == 'POST'
                and scope['path'].rstrip('/') in ('/analyze', '/analyze/preview')):
            headers = [value for key, value in scope['headers'] if key.lower() == b'authorization']
            if len(headers) > 1:
                return await JSONResponse({'detail': 'Use one Authorization header.'}, status_code=401)(scope, receive, send)
            header = headers[0] if headers else None
            try:
                user = await anyio.to_thread.run_sync(
                    authenticate, header.decode('latin-1') if header else None)
            except HTTPException as exc:
                return await JSONResponse({'detail': exc.detail}, status_code=exc.status_code)(
                    scope, receive, send)
            # Route dependencies run after multipart parsing. Admission belongs
            # here so rejected requests cannot consume a body spool first.
            with self.lock:
                busy = user.user_id in self.active_users or len(self.active_users) >= self.max_uploads
                if not busy:
                    self.active_users.add(user.user_id)
            if busy:
                return await JSONResponse(
                    {'detail': 'An upload is already processing. Please try again shortly.'},
                    status_code=429)(scope, receive, send)
            scope.setdefault('state', {})['upload_user'] = user
            try:
                return await self.app(scope, receive, send)
            finally:
                # This also runs for malformed bodies, timeouts, disconnects,
                # exceptions and cancellation. Keep cleanup synchronous.
                with self.lock:
                    self.active_users.discard(user.user_id)
        await self.app(scope, receive, send)
