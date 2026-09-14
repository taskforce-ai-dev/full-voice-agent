from __future__ import annotations
import logging
import pytest

@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "exception"])
async def test_privacy_safe_dashboard_transferred_redacts_success_and_exception(monkeypatch, caplog, mode):
    import booking_api, dashboard_client
    class Response:
        status=200
        async def __aenter__(self): return self
        async def __aexit__(self,*_): return False
        async def text(self): raise AssertionError("no body")
    class Session:
        def post(self,*_,**kwargs):
            if mode=="exception": raise RuntimeError("DASH-SECRET")
            self.payload=kwargs["json"]; return Response()
    session=Session()
    async def get_session(): return session
    monkeypatch.setattr(booking_api,"get_session",get_session)
    monkeypatch.setattr(dashboard_client,"_ENABLED",True); monkeypatch.setattr(dashboard_client,"_announced",False)
    monkeypatch.setattr(dashboard_client,"DASHBOARD_API_URL","https://x"); monkeypatch.setattr(dashboard_client,"DASHBOARD_API_KEY","KEY-SECRET"); monkeypatch.setattr(dashboard_client,"DASHBOARD_AGENT_ID","agent")
    with caplog.at_level(logging.INFO): await dashboard_client.send_call_transferred("CALL-SECRET","PHONE-SECRET","REASON-SECRET",privacy_safe=True,transfer_target="human_support")
    if mode=="success": assert session.payload["call"]["status"]=="transferred"
    assert "CALL-SECRET" not in caplog.text and "PHONE-SECRET" not in caplog.text and "DASH-SECRET" not in caplog.text
