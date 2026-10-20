"""Bound request bodies before multipart parsing, including chunked uploads."""
import tempfile
import anyio
from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    def __init__(self, app, upload_limit):
        self.app, self.upload_limit = app, upload_limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PUT', 'PATCH'):
            return await self.app(scope, receive, send)
        limit = self.upload_limit if scope['path'] in ('/analyze', '/analyze/preview') else 256 * 1024
        length = dict(scope['headers']).get(b'content-length', b'0')
        if not length.isdigit() or int(length) > limit:
            return await JSONResponse({'detail': 'Request too large.'}, status_code=413)(scope, receive, send)
        # Keep only 1 MB in memory, with a hard cap on the temporary file too.
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024) as body:
            size = 0
            try:
                with anyio.fail_after(30):
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            return
                        chunk = message.get('body', b'')
                        size += len(chunk)
                        if size > limit:
                            return await JSONResponse({'detail': 'Request too large.'}, status_code=413)(scope, receive, send)
                        await anyio.to_thread.run_sync(body.write, chunk)
                        if not message.get('more_body', False):
                            break
            except TimeoutError:
                return await JSONResponse({'detail': 'Upload timed out. Please try again.'}, status_code=408)(scope, receive, send)
            await anyio.to_thread.run_sync(body.seek, 0)
            remaining = size
            delivered = False
            async def replay():
                nonlocal remaining, delivered
                if delivered:
                    return await receive()
                chunk = await anyio.to_thread.run_sync(body.read, 64 * 1024)
                remaining -= len(chunk)
                delivered = remaining == 0
                return {'type': 'http.request', 'body': chunk, 'more_body': not delivered}
            await self.app(scope, replay, send)
