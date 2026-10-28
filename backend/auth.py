"""Server-derived identity: loopback-only local mode or revocable password sessions."""
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
from time import monotonic
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse, Response


@dataclass(frozen=True)
class Identity:
    user_id: int
    username: str
    mode: str


def auth_mode():
    mode = os.getenv('ECHOMIND_AUTH_MODE', 'local')
    if mode not in ('local', 'password'):
        raise ValueError('ECHOMIND_AUTH_MODE must be local or password')
    return mode


def local_user_id():
    user = int(os.getenv('ECHOMIND_LOCAL_USER_ID', '1'))
    if user < 1:
        raise ValueError('Invalid local user identity')
    return user


def allowed_origins():
    values = os.getenv('ECHOMIND_ALLOWED_ORIGINS', 'http://localhost:8501,http://127.0.0.1:8501,http://localhost:8000,http://127.0.0.1:8000')
    return [value.strip().rstrip('/') for value in values.split(',') if value.strip()]


def hash_password(password):
    if not 12 <= len(password) <= 1024:
        raise ValueError('Password must have 12..1024 characters')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=131072, r=8, p=1, maxmem=256*1024*1024, dklen=32)
    return 'scrypt$131072$8$1$' + salt.hex() + '$' + digest.hex()


def verify_password(password, stored):
    try:
        algorithm, n, r, p, salt, expected = stored.split('$')
        if (algorithm, n, r, p) != ('scrypt', '131072', '8', '1') or len(password) > 1024:
            return False
        if len(salt) != 32 or len(expected) != 64:
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=256*1024*1024, dklen=32)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


class AuthStore:
    def __init__(self, pool):
        self.pool = pool
        self.hash_slots = asyncio.Semaphore(2)
        self.attempts = {}
        self.dummy_hash = None

    async def initialize(self):
        await self.pool.execute((Path(__file__).parent/'migrations/007_auth.sql').read_text(encoding='utf-8'))
        self.dummy_hash = await asyncio.to_thread(hash_password, secrets.token_urlsafe(32))

    async def create_account(self, username, password, user_id=None):
        username = username.strip().casefold()
        if not re.fullmatch(r'[a-z0-9_.-]{3,64}', username):
            raise ValueError('Username must be 3..64 ASCII letters, digits, dot, underscore or dash')
        password_hash = await asyncio.to_thread(hash_password, password)
        async with self.pool.acquire() as conn, conn.transaction():
            if user_id is None:
                # Identity IDs follow the database allocator, never a caller-selected remote ID.
                await conn.execute('LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE')
                sequence = await conn.fetchval("SELECT pg_get_serial_sequence('users','user_id')")
                maximum = await conn.fetchval('SELECT COALESCE(max(user_id),0) FROM users')
                allocated = await conn.fetchval('SELECT nextval($1::regclass)',sequence)
                if allocated <= maximum:
                    await conn.execute('SELECT setval($1::regclass,$2,true)',sequence,maximum)
                user_id = await conn.fetchval('INSERT INTO users DEFAULT VALUES RETURNING user_id')
            elif not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM users WHERE user_id=$1)', user_id):
                raise LookupError('Existing user not found')
            await conn.execute('INSERT INTO auth_accounts(user_id,username,password_hash) VALUES($1,$2,$3)', user_id,username,password_hash)
        return user_id

    async def login(self, username, password, peer):
        now = monotonic()
        if len(self.attempts) > 2000:
            self.attempts = {key:value for key,value in self.attempts.items() if now-value[0]<300}
        if len(self.attempts)>=2000 and peer not in self.attempts:
            raise HTTPException(429,'登录请求繁忙，请稍后重试')
        start, count = self.attempts.get(peer, (now, 0))
        if now-start>=300:
            start, count = now, 0
        if count>=10:
            raise HTTPException(429, '登录尝试过多，请稍后重试')
        self.attempts[peer] = (start,count+1)
        row = await self.pool.fetchrow('SELECT user_id,username,password_hash,disabled FROM auth_accounts WHERE username=$1', username.strip().casefold())
        async with self.hash_slots:
            valid = await asyncio.to_thread(verify_password, password, row['password_hash'] if row else self.dummy_hash)
        if not row or not valid or row['disabled']:
            raise HTTPException(401,'用户名或密码不正确')
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('DELETE FROM auth_sessions WHERE expires_at<=now()')
            await conn.execute('''DELETE FROM auth_sessions WHERE token_hash IN
                (SELECT token_hash FROM auth_sessions WHERE user_id=$1 ORDER BY created_at DESC OFFSET 4)''',row['user_id'])
            expires = await conn.fetchval('''INSERT INTO auth_sessions(token_hash,user_id,expires_at)
                VALUES($1,$2,now()+interval '12 hours') RETURNING expires_at''', token_hash,row['user_id'])
        return {'access_token':token,'token_type':'bearer','expires_at':expires.isoformat(),
                'user':{'user_id':row['user_id'],'username':row['username'],'mode':'password'}}

    async def resolve(self, token):
        if not token or len(token)>128:
            return None
        row = await self.pool.fetchrow('''SELECT a.user_id,a.username FROM auth_sessions s JOIN auth_accounts a USING(user_id)
            WHERE s.token_hash=$1 AND s.expires_at>now() AND NOT a.disabled''',hashlib.sha256(token.encode()).hexdigest())
        return Identity(row['user_id'],row['username'],'password') if row else None

    async def revoke(self, token_hash):
        await self.pool.execute('DELETE FROM auth_sessions WHERE token_hash=$1', token_hash)


def require_matching_user(request, supplied_user_id):
    identity = getattr(request.state,'identity',None)
    if identity is None:
        raise HTTPException(401,'需要有效身份')
    if identity.user_id != supplied_user_id:
        raise HTTPException(403,'不能访问其他用户的数据')
    return identity.user_id


class IdentityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope,receive,send)
        request = Request(scope)
        async def deny(code, message):
            return await JSONResponse({'detail':message},status_code=code)(scope,receive,send)
        try:
            mode = auth_mode()
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') not in allowed_origins():
                return await deny(403,'请求来源不允许')
            if mode=='local':
                peer = scope.get('client') or ('',0)
                try:
                    loopback = ipaddress.ip_address(peer[0]).is_loopback
                except ValueError:
                    loopback = False
                host = urlsplit('http://'+request.headers.get('host','')).hostname
                if not loopback or host not in ('localhost','127.0.0.1','::1') or request.headers.get('sec-fetch-site')=='cross-site':
                    return await deny(403,'本机模式只允许本机访问；多人部署请配置账号模式')
                identity = Identity(local_user_id(),'本机用户','local')
            else:
                identity = None
            public = request.url.path in ('/auth/status','/auth/login','/health','/openapi.json','/docs','/docs/oauth2-redirect')
            if public or request.method=='OPTIONS':
                return await self.app(scope,receive,send)
            if mode=='password':
                scheme,_,token = request.headers.get('authorization','').partition(' ')
                if scheme.casefold()!='bearer':
                    return await deny(401,'请先登录')
                identity = await request.app.state.auth_store.resolve(token)
                if identity is None:
                    return await deny(401,'登录已失效，请重新登录')
                scope.setdefault('state',{})['auth_token_hash'] = hashlib.sha256(token.encode()).hexdigest()
            scope.setdefault('state',{})['identity'] = identity
            values = parse_qsl(scope.get('query_string',b'').decode('utf-8'),keep_blank_values=True)
            for key,value in values:
                if key=='user_id' and value != str(identity.user_id):
                    return await deny(403,'不能访问其他用户的数据')
            values = [(key,value) for key,value in values if key!='user_id'] + [('user_id',str(identity.user_id))]
            scope['query_string'] = urlencode(values).encode()
        except (ValueError,UnicodeError):
            return await deny(503,'身份配置不可用')
        return await self.app(scope,receive,send)


router = APIRouter(prefix='/auth',tags=['identity'])

class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1,max_length=64)
    password: str = Field(min_length=1,max_length=1024)

@router.get('/status')
async def status():
    return {'mode':auth_mode(),'login_required':auth_mode()=='password'}

@router.post('/login')
async def login(payload:Login,request:Request):
    if auth_mode()!='password':
        raise HTTPException(409,'本机模式无需登录')
    response = JSONResponse(await request.app.state.auth_store.login(payload.username,payload.password,request.client.host))
    response.headers['Cache-Control']='no-store'
    return response

@router.get('/me')
async def me(request:Request):
    identity = request.state.identity
    return {'user_id':identity.user_id,'username':identity.username,'mode':identity.mode}

@router.post('/logout',status_code=204)
async def logout(request:Request):
    token_hash = getattr(request.state,'auth_token_hash',None)
    if token_hash:
        await request.app.state.auth_store.revoke(token_hash)
    return Response(status_code=204)
