"""Deterministic collision-safe resource derivation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .model import AgentManifest
from .schema import ManifestError, validate_slug


class ResourceConflict(ValueError):
    """Raised when a derived resource is already allocated."""


@dataclass(frozen=True)
class DerivedResources:
    slug: str
    python_identifier: str
    website_service: str
    smartpbx_service: str
    website_port: int
    smartpbx_port: int
    website_hostname: str
    smartpbx_hostname: str
    wss_url: str
    wss_header: str
    status_url: str
    ghcr_repository: str
    ci_identifier: str


class AllocationRegistry:
    def __init__(self, allocations: Mapping[str, Iterable[object]] | None = None) -> None:
        allocations = allocations or {}
        self._ports = {int(port) for port in allocations.get("ports", ())}
        self._hostnames = {str(hostname).lower() for hostname in allocations.get("hostnames", ())}
        self._services = {str(service) for service in allocations.get("services", ())}

    def next_free_port(self, *, start: int, end: int, exclude: Iterable[int] = ()) -> int:
        excluded = set(exclude)
        for port in range(start, end + 1):
            if port not in self._ports and port not in excluded:
                return port
        raise ResourceConflict(f"no free port in range {start}-{end}")

    def require_free_hostname(self, hostname: str) -> None:
        if hostname.lower() in self._hostnames:
            raise ResourceConflict(f"hostname already allocated: {hostname}")

    def require_free_service(self, service: str) -> None:
        if service in self._services:
            raise ResourceConflict(f"service already allocated: {service}")

    def reserve(self, resources: DerivedResources) -> None:
        if resources.website_port in self._ports or resources.smartpbx_port in self._ports:
            raise ResourceConflict("port already allocated")
        if resources.website_hostname.lower() in self._hostnames or resources.smartpbx_hostname.lower() in self._hostnames:
            raise ResourceConflict("hostname already allocated")
        if resources.website_service in self._services or resources.smartpbx_service in self._services:
            raise ResourceConflict("service already allocated")
        self._ports.update((resources.website_port, resources.smartpbx_port))
        self._hostnames.update((resources.website_hostname.lower(), resources.smartpbx_hostname.lower()))
        self._services.update((resources.website_service, resources.smartpbx_service))


def _header_slug(agent_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "-", agent_name).strip("-")[:48]
    if not value:
        raise ManifestError("agent_name cannot produce a valid authentication header")
    return value


def derive_resources(manifest: AgentManifest, registry: AllocationRegistry) -> DerivedResources:
    slug = validate_slug(manifest.slug)
    website_port = registry.next_free_port(start=18080, end=18999)
    smartpbx_port = registry.next_free_port(start=19080, end=19999, exclude={website_port})
    smartpbx_hostname = f"smartpbx-{slug}.taskforceai.tech"
    website_hostname = f"demo-{slug}.taskforceai.tech"
    website_service = f"smartpbx-{slug}-website"
    smartpbx_service = f"smartpbx-{slug}"
    registry.require_free_hostname(smartpbx_hostname)
    registry.require_free_hostname(website_hostname)
    registry.require_free_service(smartpbx_service)
    registry.require_free_service(website_service)
    return DerivedResources(
        slug=slug,
        python_identifier=slug.replace("-", "_"),
        website_service=website_service,
        smartpbx_service=smartpbx_service,
        website_port=website_port,
        smartpbx_port=smartpbx_port,
        website_hostname=website_hostname,
        smartpbx_hostname=smartpbx_hostname,
        wss_url=f"wss://{smartpbx_hostname}/ws/v1/smartpbx/media",
        wss_header=f"X-{_header_slug(manifest.agent_name)}-SmartPBX-Token",
        status_url=f"https://{smartpbx_hostname}/smartpbx/status",
        ghcr_repository=f"ghcr.io/taskforce-ai-dev/smartpbx-{slug}",
        ci_identifier=f"smartpbx-{slug}",
    )
