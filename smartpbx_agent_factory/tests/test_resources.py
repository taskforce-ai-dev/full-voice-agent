import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.resources import AllocationRegistry, ResourceConflict, derive_resources
from smartpbx_agent_factory.schema import parse_manifest
from smartpbx_agent_factory.catalogue import CapabilityCatalogue


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"
CATALOGUE = Path(__file__).parent / "fixtures" / "approved-provider-catalogue.json"


def fixture_manifest(slug="acme-inquiry"):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["slug"] = slug
    catalogue = CapabilityCatalogue.load(CATALOGUE)
    return parse_manifest(raw, approved_source_roots=(Path.cwd(),), catalogue=catalogue)


def test_resource_allocator_rejects_port_and_hostname_collision():
    registry = AllocationRegistry({"ports": [18080], "hostnames": ["smartpbx-acme.taskforceai.tech"]})
    with pytest.raises(ResourceConflict, match="port|hostname"):
        derive_resources(fixture_manifest(slug="acme"), registry)


def test_resource_allocator_derives_separate_services_and_urls():
    resources = derive_resources(fixture_manifest(), AllocationRegistry())
    assert resources.website_service != resources.smartpbx_service
    assert resources.website_port != resources.smartpbx_port
    assert resources.website_hostname == "demo-acme-inquiry.taskforceai.tech"
    assert resources.smartpbx_hostname == "smartpbx-acme-inquiry.taskforceai.tech"
    assert resources.wss_url.endswith("/ws/v1/smartpbx/media")
    assert resources.status_url.endswith("/smartpbx/status")


def test_registry_reserve_makes_derived_resources_collide():
    registry = AllocationRegistry()
    first = derive_resources(fixture_manifest(), registry)
    registry.reserve(first)
    with pytest.raises(ResourceConflict):
        derive_resources(fixture_manifest(), registry)


@pytest.mark.parametrize(
    "kind",
    [
        "slugs",
        "folders",
        "services",
        "containers",
        "ports",
        "hostnames",
        "wss_headers",
        "ghcr_repositories",
        "ci_identifiers",
        "secret_record_keys",
    ],
)
def test_registry_rejects_every_hard_conflict(kind):
    resources = derive_resources(fixture_manifest(), AllocationRegistry())
    value = {
        "slugs": resources.slug,
        "folders": resources.folder_identity,
        "services": resources.smartpbx_service,
        "containers": resources.smartpbx_service,
        "ports": resources.smartpbx_port,
        "hostnames": resources.smartpbx_hostname,
        "wss_headers": resources.wss_header,
        "ghcr_repositories": resources.ghcr_repository,
        "ci_identifiers": resources.ci_identifier,
        "secret_record_keys": resources.secret_record_key,
    }[kind]
    allocations = {kind: [value]}
    if kind == "ports":
        allocations = {
            "ports": list(range(18080, 19000)) + list(range(19080, 20000)),
        }
    with pytest.raises(ResourceConflict):
        derive_resources(fixture_manifest(), AllocationRegistry(allocations))
