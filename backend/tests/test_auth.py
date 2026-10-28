"""Identity is derived from the server; query/form IDs cannot impersonate a user."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from auth import AuthStore,Identity,IdentityMiddleware,hash_password,verify_password,require_matching_user,router


class AuthMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI,Request,Form
        self.app=FastAPI()
        self.app.include_router(router)
        self.app.add_middleware(IdentityMiddleware)
        @self.app.get('/private')
        async def private(request:Request,user_id:int):
            return {'user_id':user_id,'identity':request.state.identity.user_id}
        @self.app.post('/form')
        async def form(request:Request,user_id:int=Form(...)):
            return {'user_id':require_matching_user(request,user_id)}
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,client=('127.0.0.1',123)),base_url='http://localhost')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_local_identity_injected_and_spoofed_query_or_form_rejected(self):
        with patch.dict(os.environ,{'ECHOMIND_AUTH_MODE':'local','ECHOMIND_LOCAL_USER_ID':'1'}):
            self.assertEqual((await self.client.get('/private')).json(),{'user_id':1,'identity':1})
            for url in ('/private?user_id=2','/private?user_id=1&user_id=2','/private?user_id=x'):
                self.assertEqual((await self.client.get(url)).status_code,403)
            self.assertEqual((await self.client.post('/form',data={'user_id':2})).status_code,403)
            self.assertEqual((await self.client.post('/form',data={'user_id':1})).status_code,200)

    async def test_local_rejects_remote_peer_rebinding_and_cross_site_origin(self):
        import httpx
        with patch.dict(os.environ,{'ECHOMIND_AUTH_MODE':'local'}):
            for headers in ({'Host':'attacker.example'},{'Origin':'https://attacker.example'},{'Sec-Fetch-Site':'cross-site'}):
                self.assertEqual((await self.client.get('/private',headers=headers)).status_code,403)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app,client=('203.0.113.5',123)),base_url='http://localhost') as remote:
                self.assertEqual((await remote.get('/private')).status_code,403)

    async def test_password_mode_requires_bearer_and_checks_identity(self):
        self.app.state.auth_store=SimpleNamespace(resolve=AsyncMock(return_value=Identity(2,'user2','password')))
        with patch.dict(os.environ,{'ECHOMIND_AUTH_MODE':'password'}):
            self.assertEqual((await self.client.get('/auth/status')).status_code,200)
            self.assertEqual((await self.client.get('/private')).status_code,401)
            self.assertEqual((await self.client.get('/private',headers={'Authorization':'Bearer session'})).json()['user_id'],2)
            self.assertEqual((await self.client.get('/private?user_id=1',headers={'Authorization':'Bearer session'})).status_code,403)
            self.app.state.auth_store.resolve.return_value=None
            self.assertEqual((await self.client.get('/private',headers={'Authorization':'Bearer expired'})).status_code,401)

    async def test_unknown_auth_mode_fails_closed(self):
        with patch.dict(os.environ,{'ECHOMIND_AUTH_MODE':'typo'}):
            self.assertEqual((await self.client.get('/private')).status_code,503)


class PasswordTests(unittest.TestCase):
    def test_salted_hash_and_invalid_parameters(self):
        password='Long passphrase for tests'
        stored=hash_password(password)
        self.assertTrue(verify_password(password,stored))
        self.assertFalse(verify_password('incorrect',stored))
        self.assertFalse(verify_password(password,stored.replace('$131072$','$1048576$')))
        self.assertFalse(verify_password(password,'invalid'))
        self.assertNotIn(password,stored)
        with self.assertRaises(ValueError): hash_password('short')


@unittest.skipUnless(os.getenv('ECHOMIND_TEST_DATABASE_URL'),'Set test DSN for isolated authentication tests')
class AuthStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncpg
        self.schema='auth_test_'+uuid4().hex
        self.admin=await asyncpg.connect(os.environ['ECHOMIND_TEST_DATABASE_URL'])
        await self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.pool=await asyncpg.create_pool(os.environ['ECHOMIND_TEST_DATABASE_URL'],min_size=1,max_size=3,server_settings={'search_path':self.schema})
        await self.pool.execute('CREATE TABLE users(user_id SERIAL PRIMARY KEY); INSERT INTO users DEFAULT VALUES')
        self.store=AuthStore(self.pool)
        await self.store.initialize()

    async def asyncTearDown(self):
        await self.pool.close()
        await self.admin.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.admin.close()

    async def test_login_expiry_revocation_unknown_user_and_password_storage(self):
        from fastapi import HTTPException
        user=await self.store.create_account('owner','Long passphrase for tests',1)
        response=await self.store.login('owner','Long passphrase for tests','local-test')
        token=response['access_token']
        self.assertEqual((await self.store.resolve(token)).user_id,user)
        row=await self.pool.fetchrow('SELECT * FROM auth_sessions')
        self.assertNotEqual(row['token_hash'].strip(),token)
        await self.store.revoke(row['token_hash'])
        self.assertIsNone(await self.store.resolve(token))
        response=await self.store.login('owner','Long passphrase for tests','other-peer')
        await self.pool.execute("UPDATE auth_sessions SET expires_at=now()-interval '1 second'")
        self.assertIsNone(await self.store.resolve(response['access_token']))
        with self.assertRaises(HTTPException): await self.store.login('unknown','wrong','peer')
        with self.assertRaises(HTTPException): await self.store.login('owner','wrong','peer')

    async def test_login_rate_limit_and_new_user_isolation(self):
        from fastapi import HTTPException
        second=await self.store.create_account('other-user','Another long test password')
        self.assertNotEqual(second,1)
        self.store.attempts['peer']=(__import__('time').monotonic(),10)
        with self.assertRaises(HTTPException) as caught: await self.store.login('other-user','Another long test password','peer')
        self.assertEqual(caught.exception.status_code,429)
