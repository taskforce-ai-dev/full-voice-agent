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
    folder_identity: str = ""
    secret_record_key: str = ""


class AllocationRegistry:
    def __init__(self, allocations: Mapping[str, Iterable[object]] | None = None) -> None:
        allocations = allocations or {}
        self._ports = {int(port) for port in allocations.get("ports", ())}
        self._hostnames = {str(hostname).lower() for hostname in allocations.get("hostnames", ())}
        self._services = {str(service) for service in allocations.get("services", ())}
        self._containers = {str(container) for container in allocations.get("containers", ())}
        self._slugs = {str(slug) for slug in allocations.get("slugs", ())}
        self._folders = {str(folder) for folder in allocations.get("folders", ())}
        self._wss_headers = {str(header).lower() for header in allocations.get("wss_headers", ())}
        self._ghcr_repositories = {str(repository).lower() for repository in allocations.get("ghcr_repositories", ())}
        self._ci_identifiers = {str(identifier) for identifier in allocations.get("ci_identifiers", ())}
        self._secret_record_keys = {str(key) for key in allocations.get("secret_record_keys", ())}

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

    def require_free(self, value: str, allocated: set[str], label: str) -> None:
        if value.lower() in {item.lower() for item in allocated}:
            raise ResourceConflict(f"{label} already allocated: {value}")

    def reserve(self, resources: DerivedResources) -> None:
        if resources.website_port in self._ports or resources.smartpbx_port in self._ports:
            raise ResourceConflict("port already allocated")
        if resources.website_hostname.lower() in self._hostnames or resources.smartpbx_hostname.lower() in self._hostnames:
            raise ResourceConflict("hostname already allocated")
        if resources.website_service in self._services or resources.smartpbx_service in self._services:
            raise ResourceConflict("service already allocated")
        if resources.website_service in self._containers or resources.smartpbx_service in self._containers:
            raise ResourceConflict("container already allocated")
        if resources.slug in self._slugs or resources.folder_identity in self._folders:
            raise ResourceConflict("slug or folder already allocated")
        if resources.wss_header.lower() in self._wss_headers:
            raise ResourceConflict("WSS header already allocated")
        if resources.ghcr_repository.lower() in self._ghcr_repositories:
            raise ResourceConflict("GHCR repository already allocated")
        if resources.ci_identifier in self._ci_identifiers:
            raise ResourceConflict("CI identifier already allocated")
        if resources.secret_record_key in self._secret_record_keys:
            raise ResourceConflict("secret record key already allocated")
        self._ports.update((resources.website_port, resources.smartpbx_port))
        self._hostnames.update((resources.website_hostname.lower(), resources.smartpbx_hostname.lower()))
        self._services.update((resources.website_service, resources.smartpbx_service))
        self._containers.update((resources.website_service, resources.smartpbx_service))
        self._slugs.add(resources.slug)
        self._folders.add(resources.folder_identity)
        self._wss_headers.add(resources.wss_header.lower())
        self._ghcr_repositories.add(resources.ghcr_repository.lower())
        self._ci_identifiers.add(resources.ci_identifier)
        self._secret_record_keys.add(resources.secret_record_key)


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
    folder_identity = f"SmartPBX Agents/{slug}"
    secret_record_key = f"agents/{slug}/secrets.sops.yaml"
    registry.require_free_hostname(smartpbx_hostname)
    registry.require_free_hostname(website_hostname)
    registry.require_free_service(smartpbx_service)
    registry.require_free_service(website_service)
    registry.require_free(smartpbx_service, registry._containers, "container")
    registry.require_free(website_service, registry._containers, "container")
    registry.require_free(slug, registry._slugs, "slug")
    registry.require_free(folder_identity, registry._folders, "folder")
    registry.require_free(f"X-{_header_slug(manifest.agent_name)}-SmartPBX-Token", registry._wss_headers, "WSS header")
    registry.require_free(f"ghcr.io/taskforce-ai-dev/smartpbx-{slug}", registry._ghcr_repositories, "GHCR repository")
    registry.require_free(f"smartpbx-{slug}", registry._ci_identifiers, "CI identifier")
    registry.require_free(secret_record_key, registry._secret_record_keys, "secret record key")
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
        folder_identity=folder_identity,
        secret_record_key=secret_record_key,
    )
