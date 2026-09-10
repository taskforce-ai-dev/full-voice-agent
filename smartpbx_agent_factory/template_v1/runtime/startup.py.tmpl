"""Generated one-time ASGI composition for the reviewed runtime candidate."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from provider_runtime import bind_provider_adapter, load_provider_profile
from server import build_service_app, load_runtime
from smartpbx_gateway import CarrierIngressSettings


@dataclass(frozen=True)
class RuntimeConfiguration:
    token: str
    account_id: str
    auth_header_name: str
    max_calls: int
    product_profile_path: str
    knowledge_dir: str
    provider_profile_path: str

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "RuntimeConfiguration":
        names = (
            "SMARTPBX_WS_TOKEN", "SMARTPBX_ACCOUNT_ID", "SMARTPBX_AUTH_HEADER_NAME",
            "SMARTPBX_PRODUCT_PROFILE_PATH", "SMARTPBX_KNOWLEDGE_DIR", "SMARTPBX_PROVIDER_PROFILE_PATH",
        )
        values = {name: environ.get(name, "") for name in names}
        missing = [name for name in names if not values[name]]
        if missing:
            raise RuntimeError("missing required runtime configuration: " + ", ".join(missing))
        try:
            max_calls = int(environ.get("SMARTPBX_MAX_CALLS", "4"))
        except ValueError as error:
            raise RuntimeError("SMARTPBX_MAX_CALLS must be an integer") from error
        return cls(
            token=values["SMARTPBX_WS_TOKEN"], account_id=values["SMARTPBX_ACCOUNT_ID"],
            auth_header_name=values["SMARTPBX_AUTH_HEADER_NAME"], max_calls=max_calls,
            product_profile_path=values["SMARTPBX_PRODUCT_PROFILE_PATH"], knowledge_dir=values["SMARTPBX_KNOWLEDGE_DIR"],
            provider_profile_path=values["SMARTPBX_PROVIDER_PROFILE_PATH"],
        )


def create_app():
    config = RuntimeConfiguration.from_environ(os.environ)
    ingress = CarrierIngressSettings(
        enabled=True, token=config.token, account_id=config.account_id,
        auth_header_name=config.auth_header_name, max_calls=config.max_calls,
    )
    provider_profile = load_provider_profile(config.provider_profile_path)
    provider_adapter = bind_provider_adapter(provider_profile, os.environ)
    return build_service_app(load_runtime(
        ingress=ingress, product_profile_path=config.product_profile_path,
        provider_adapter=provider_adapter,
    ))


app = create_app()
