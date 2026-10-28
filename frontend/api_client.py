"""Per-Streamlit-session authentication; tokens never go to another origin."""
from urllib.parse import urlsplit
import requests as _requests
import streamlit as st

DEFAULT_API_BASE_URL = 'http://127.0.0.1:8000'


def transport_headers(token=None):
    if token is None:
        token=st.session_state.get('identity_token','')
        origin=st.session_state.get('identity_token_origin')
        if token and origin != _origin(st.session_state.get('identity_api_base',DEFAULT_API_BASE_URL)):
            return {}
    return {'Authorization':'Bearer '+token} if token else {}


def _origin(url):
    parsed=urlsplit(url)
    return parsed.scheme,parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80)


class APIRequests:
    def _call(self,method,url,**kwargs):
        base=st.session_state.get('identity_api_base',DEFAULT_API_BASE_URL)
        headers=dict(kwargs.pop('headers',{}) or {})
        if _origin(url)==_origin(base):
            headers.update(transport_headers())
            # Never forward session credentials on an HTTP redirect.
            kwargs['allow_redirects']=False
        else:
            headers={key:value for key,value in headers.items() if key.casefold() not in ('authorization','cookie')}
        if headers:
            kwargs['headers']=headers
        response=getattr(_requests,method)(url,**kwargs)
        if response.status_code==401:
            st.session_state.pop('identity_checked_at',None)
        return response

    def get(self,url,**kwargs): return self._call('get',url,**kwargs)
    def post(self,url,**kwargs): return self._call('post',url,**kwargs)
    def put(self,url,**kwargs): return self._call('put',url,**kwargs)
    def patch(self,url,**kwargs): return self._call('patch',url,**kwargs)
    def delete(self,url,**kwargs): return self._call('delete',url,**kwargs)
    def __getattr__(self,name): return getattr(_requests,name)


api_requests=APIRequests()
