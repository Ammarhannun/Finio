"""Reject concurrent uploads before allocating another multipart/body spool."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from starlette.responses import JSONResponse

from api import upload_auth, body_limit


def scope(user='one', path='/analyze', method='POST'):
    return {'type':'http', 'method':method, 'path':path,
            'http_version':'1.1', 'scheme':'http', 'query_string':b'',
            'server':('testserver',80), 'client':('127.0.0.1',12345),
            'asgi':{'version':'3.0', 'spec_version':'2.4'},
            'headers':[(b'authorization', ('Bearer '+user).encode())]}


@pytest.fixture
def authenticate(monkeypatch):
    monkeypatch.setattr(upload_auth,'authenticate',lambda header:SimpleNamespace(user_id=header[7:],token=header[7:]))


@pytest.mark.parametrize('path',['/analyze','/analyze/preview','/analyze/','/analyze/preview/'])
def test_duplicate_user_is_refused_without_reading_or_allocating_another_spool(authenticate,monkeypatch,path):
    original=body_limit.tempfile.SpooledTemporaryFile
    spools=Mock(side_effect=original)
    monkeypatch.setattr(body_limit.tempfile,'SpooledTemporaryFile',spools)
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        async def terminal(s,r,send):await JSONResponse({'ok':True})(s,r,send)
        app=upload_auth.UploadAuthMiddleware(body_limit.BodyLimitMiddleware(terminal,upload_limit=8))
        async def slow_body():
            entered.set();await release.wait()
            return {'type':'http.request','body':b'x','more_body':False}
        async def forbidden_body():raise AssertionError('Read a rejected request body')
        async def discard(message):pass
        first=asyncio.create_task(app(scope(path=path),slow_body,discard))
        try:
            await asyncio.wait_for(entered.wait(),2)
            sent=[]
            async def send(message):sent.append(message)
            await app(scope(path=path),forbidden_body,send)
            assert sent[0]['status']==429
            assert spools.call_count==1
        finally:
            release.set();await first
        assert not app.active_users
    asyncio.run(run())


def test_global_admission_bounds_distinct_users_and_keeps_health_responsive(authenticate):
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        count=0
        async def terminal(s,r,send):
            nonlocal count
            if s['method']=='POST':
                count+=1
                if count==2:entered.set()
                await release.wait()
            await JSONResponse({'ok':True})(s,r,send)
        app=upload_auth.UploadAuthMiddleware(terminal)
        async def forbidden_body():raise AssertionError('Body was read')
        async def discard(message):pass
        first=[asyncio.create_task(app(scope(user),forbidden_body,discard)) for user in ('one','two')]
        try:
            await asyncio.wait_for(entered.wait(),2)
            rejected=[]
            async def send(message):rejected.append(message)
            await app(scope('three'),forbidden_body,send)
            assert rejected[0]['status']==429
            health=[]
            async def health_send(message):health.append(message)
            await app(scope(path='/ready',method='GET'),forbidden_body,health_send)
            assert health[0]['status']==200
        finally:
            release.set();await asyncio.gather(*first)
        assert not app.active_users
    asyncio.run(run())


@pytest.mark.parametrize('failure',['oversize','disconnect','timeout','exception','cancelled'])
def test_failed_uploads_always_release_admission_for_a_retry(authenticate,failure):
    async def run():
        mode=failure
        async def terminal(s,r,send):await JSONResponse({'ok':True})(s,r,send)
        app=upload_auth.UploadAuthMiddleware(body_limit.BodyLimitMiddleware(terminal,upload_limit=8))
        async def receive():
            if mode=='disconnect':return {'type':'http.disconnect'}
            if mode=='timeout':raise TimeoutError()
            if mode=='exception':raise RuntimeError('synthetic failure')
            if mode=='cancelled':raise asyncio.CancelledError()
            return {'type':'http.request','body':b'123456789' if mode=='oversize' else b'x','more_body':False}
        sent=[]
        async def send(message):sent.append(message)
        if failure in ('exception','cancelled'):
            with pytest.raises(RuntimeError if failure=='exception' else asyncio.CancelledError):
                await app(scope(),receive,send)
        else:
            await app(scope(),receive,send)
            if failure=='disconnect':
                assert not sent
            else:
                assert sent[0]['status']==(413 if failure=='oversize' else 408)
        assert not app.active_users
        mode='ok';sent.clear()
        await app(scope(),receive,send)
        assert sent[0]['status']==200
        assert not app.active_users
    asyncio.run(run())


def test_application_admission_runs_before_body_buffering(authenticate,monkeypatch):
    import main
    original=body_limit.tempfile.SpooledTemporaryFile
    spools=Mock(side_effect=original)
    monkeypatch.setattr(body_limit.tempfile,'SpooledTemporaryFile',spools)
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        app=main.app.build_middleware_stack()
        async def slow_body():
            entered.set();await release.wait()
            return {'type':'http.request','body':b'','more_body':False}
        async def forbidden_body():raise AssertionError('Read a rejected request body')
        async def discard(message):pass
        first=asyncio.create_task(app(scope(),slow_body,discard))
        try:
            await asyncio.wait_for(entered.wait(),2)
            sent=[]
            async def send(message):sent.append(message)
            await app(scope(),forbidden_body,send)
            assert sent[0]['status']==429
            assert spools.call_count==1
            assert dict(sent[0]['headers'])[b'cache-control'].startswith(b'no-store')
        finally:
            release.set();await first
    asyncio.run(run())
