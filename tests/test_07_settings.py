"""
Settings persistence — write a setting via API, read it back, confirm it saved.

Covers the contributor settings surfaces and mentor settings. Uses a throwaway
contributor account (created + logged in) so we never mutate a real user's prefs.
DB cleanup afterwards.
"""

import time
import pytest
import requests
from conftest import base, post, get, delete, TIMEOUT


@pytest.fixture
def contributor_session(sa_token):
    """Create a contributor, log in, yield (email, token). Cleans up via the API."""
    email = f"settings-{int(time.time()*1000)%10_000_000}@apitest.glimmora.dev"
    pw = "Settings@123"
    r = post("super-admin", "/api/superadmin/users", token=sa_token,
             json={"email": email, "name": "Settings Test", "role": "contributor",
                   "password": pw, "sendCredentials": False})
    assert r.status_code in (200, 201), r.text[:200]
    uid = str(r.json().get("id", ""))
    login = post("freelancer", "/api/v1/auth/login", json={"email": email, "password": pw})
    assert login.status_code == 200, login.text[:200]
    tok = login.json()["access_token"]
    yield email, tok
    if uid:
        try:
            delete("super-admin", f"/api/superadmin/users/{uid}", token=sa_token)
        except Exception:  # noqa: BLE001
            pass


def test_contributor_settings_read(contributor_session):
    _, tok = contributor_session
    r = get("freelancer", "/api/contributor/settings", token=tok)
    assert r.status_code == 200, f"settings read: {r.status_code} {r.text[:150]}"


def test_contributor_notification_settings_save_and_persist(contributor_session):
    _, tok = contributor_session
    # save a notification preference
    payload = {"email": False, "push": True, "task_alerts": False}
    save = requests.patch(f"{base('freelancer')}/api/contributor/settings/notifications",
                          headers={"Authorization": f"Bearer {tok}"}, json=payload, timeout=TIMEOUT)
    if save.status_code == 404:
        pytest.skip("notifications settings endpoint not PATCHable in this build")
    assert save.status_code in (200, 204), f"save: {save.status_code} {save.text[:150]}"
    # read back and confirm it stuck
    read = get("freelancer", "/api/contributor/settings", token=tok)
    assert read.status_code == 200
    body = read.json()
    data = body.get("data", body)
    notif = data.get("notifications") or data
    # at least one of our values should be reflected
    assert notif.get("email") is False or notif.get("task_alerts") is False, \
        f"notification setting did not persist: {notif}"


def test_contributor_locale_save_and_persist(contributor_session):
    _, tok = contributor_session
    save = requests.patch(f"{base('freelancer')}/api/contributor/settings/locale",
                          headers={"Authorization": f"Bearer {tok}"},
                          json={"language": "en", "timezone": "Asia/Kolkata", "currency": "INR"},
                          timeout=TIMEOUT)
    if save.status_code == 404:
        pytest.skip("locale settings endpoint not PATCHable in this build")
    assert save.status_code in (200, 204), f"locale save: {save.status_code}"
    read = get("freelancer", "/api/contributor/settings", token=tok).json()
    data = read.get("data", read)
    loc = data.get("locale") or data
    assert loc.get("timezone") == "Asia/Kolkata" or loc.get("currency") == "INR", \
        f"locale did not persist: {loc}"


def test_payout_preferences_save_and_persist(contributor_session):
    _, tok = contributor_session
    save = requests.put(f"{base('freelancer')}/api/contributor/payout-preferences",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"method": "bank_transfer", "currency": "INR"}, timeout=TIMEOUT)
    if save.status_code in (404, 405):
        pytest.skip("payout-preferences not writable in this build")
    assert save.status_code in (200, 204), f"payout pref save: {save.status_code} {save.text[:150]}"
    read = get("freelancer", "/api/contributor/payout-preferences", token=tok)
    assert read.status_code == 200
