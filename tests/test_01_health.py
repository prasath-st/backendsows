"""Every role backend is up and serving its OpenAPI spec with real routes."""

import pytest
import requests
from conftest import BACKENDS, TIMEOUT

EXPECTED_MIN_ROUTES = {
    "mentor": 20,
    "super-admin": 40,
    "enterprise": 80,
    "freelancer": 80,
    "reviewer": 20,
}


@pytest.mark.parametrize("role,url", list(BACKENDS.items()))
def test_backend_openapi_reachable(role, url):
    r = requests.get(f"{url}/openapi.json", timeout=TIMEOUT)
    assert r.status_code == 200, f"{role} openapi not 200: {r.status_code}"
    spec = r.json()
    assert "paths" in spec and spec["paths"], f"{role} has no routes"


@pytest.mark.parametrize("role,url", list(BACKENDS.items()))
def test_backend_has_expected_route_volume(role, url):
    spec = requests.get(f"{url}/openapi.json", timeout=TIMEOUT).json()
    n = len(spec.get("paths", {}))
    assert n >= EXPECTED_MIN_ROUTES[role], f"{role} only {n} routes (< {EXPECTED_MIN_ROUTES[role]})"


@pytest.mark.parametrize("role,url", list(BACKENDS.items()))
def test_every_backend_exposes_login(role, url):
    spec = requests.get(f"{url}/openapi.json", timeout=TIMEOUT).json()
    assert "/api/v1/auth/login" in spec["paths"], f"{role} missing /api/v1/auth/login"
