import streamlit as st
from api_client import api_requests as requests


def render_operations(api_base,user_id):
    if not st.button('查看近24小时运行概况',key='operations_summary_open',type='tertiary'):
        return
    try:
        response=requests.get(api_base+'/operations/summary',params={'user_id':user_id},timeout=8)
        response.raise_for_status()
        data=response.json()
    except requests.RequestException:
        st.warning('运行概况暂不可用，请稍后重试。')
        return
    st.write(f"回答 {data['runs']} 次 · 模型调用 {data['model_calls']} 次")
    if data.get('p95_ms') is not None:
        st.caption(f"总耗时 P50 {data['p50_ms']/1000:.1f} 秒 · P95 {data['p95_ms']/1000:.1f} 秒")
    st.caption(f"服务报告的模型Token：{data['reported_total_tokens']}；不等同于计费金额。")
    if data.get('sample_capped'):
        st.caption('仅统计最近500次回答。')
    for alert in data.get('alerts',[]):
        st.warning(alert['message'])
