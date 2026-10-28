"""Local mode keeps the existing workspace; password mode derives its user from login."""
import time
import streamlit as st
from api_client import api_requests as requests, _origin


def clear_identity_state():
    for key in list(st.session_state):
        del st.session_state[key]
    st.query_params.clear()


def _require_safe_local_listener(mode):
    if mode == 'local' and st.get_option('server.address') not in ('127.0.0.1', 'localhost', '::1'):
        st.error('本机模式只允许前端监听127.0.0.1、localhost或::1。请绑定回环地址；远程部署请启用password认证。')
        st.stop()


def bootstrap_identity(api_base):
    previous=st.session_state.get('identity_api_base')
    if previous and _origin(previous)!=_origin(api_base):
        clear_identity_state()
    st.session_state.identity_api_base=api_base
    cached=st.session_state.get('identity_info')
    if cached and cached.get('mode') in ('local', 'password') and time.monotonic()-st.session_state.get('identity_checked_at',0)<60:
        _require_safe_local_listener(cached['mode'])
        return cached['user_id']
    try:
        status=requests.get(api_base+'/auth/status',timeout=5)
        status.raise_for_status()
        mode=status.json()['mode']
        if mode not in ('local', 'password'):
            raise ValueError('Unknown identity mode')
        _require_safe_local_listener(mode)
        if mode=='password' and not st.session_state.get('identity_token'):
            st.title('登录 EchoMind')
            with st.form('login_form'):
                username=st.text_input('用户名',max_chars=64)
                password=st.text_input('密码',type='password',max_chars=1024)
                submit=st.form_submit_button('登录',type='primary')
            if submit:
                response=requests.post(api_base+'/auth/login',json={'username':username,'password':password},timeout=15)
                if response.ok:
                    token=response.json()['access_token']
                    clear_identity_state()
                    st.session_state.identity_token=token
                    st.session_state.identity_token_origin=_origin(api_base)
                    st.session_state.identity_api_base=api_base
                    st.rerun()
                else:
                    st.error('登录失败，请检查账号密码或稍后重试。')
            st.stop()
        response=requests.get(api_base+'/auth/me',timeout=5)
        if response.status_code==401:
            clear_identity_state()
            st.rerun()
        response.raise_for_status()
        identity=response.json()
        _require_safe_local_listener(identity.get('mode', mode))
        if cached and cached['user_id'] != identity['user_id']:
            # A server-side local-identity change must never keep the old workspace.
            token=st.session_state.get('identity_token')
            origin=st.session_state.get('identity_token_origin')
            clear_identity_state()
            if token:
                st.session_state.identity_token=token
                st.session_state.identity_token_origin=origin
            st.session_state.identity_api_base=api_base
        st.session_state.identity_info=identity
        st.session_state.identity_checked_at=time.monotonic()
        return identity['user_id']
    except (requests.RequestException,KeyError,ValueError):
        st.error('身份服务暂时不可用，请确认后端已启动。')
        st.stop()


def render_identity():
    identity=st.session_state.get('identity_info',{})
    if identity.get('mode')=='password':
        st.caption('当前账号：'+identity.get('username',''))
        if st.button('退出登录',key='identity_logout'):
            try:
                response=requests.post(st.session_state.identity_api_base+'/auth/logout',timeout=5)
                response.raise_for_status()
            except requests.RequestException:
                st.error('退出请求未完成，请重试。')
                return
            clear_identity_state()
            st.rerun()
