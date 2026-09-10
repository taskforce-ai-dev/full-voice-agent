import json
from pathlib import Path

import pytest

from smartpbx_agent_factory.resources import AllocationRegistry, ResourceConflict, derive_resources
from smartpbx_agent_factory.schema import parse_manifest


FIXTURE = Path(__file__).parent / "fixtures" / "acme-minimal.json"


def fixture_manifest(slug="acme-inquiry"):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["slug"] = slug
    return parse_manifest(raw)


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
