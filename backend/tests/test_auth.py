from uuid import uuid4

import jwt

from app.core.config import get_settings
from app.core.security import hash_password, verify_password


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("s3cret")
    assert hashed != "s3cret"
    assert verify_password("s3cret", hashed)
    assert not verify_password("wrong", hashed)


def test_login_token_has_tenant_and_role(api) -> None:
    response = api.post("/auth/login", json={"email": "acme.admin@example.com", "password": "test-password"})
    assert response.status_code == 200
    body = response.json()
    claims = jwt.decode(body["access_token"], get_settings().jwt_secret, algorithms=["HS256"])
    assert claims["role"] == "admin"
    assert claims["tenant_id"] == body["user"]["tenant_id"]
    assert body["user"]["tenant_slug"] == "acme"


def test_login_rejects_bad_password(api) -> None:
    response = api.post("/auth/login", json={"email": "acme.user@example.com", "password": "nope"})
    assert response.status_code == 401


def test_me_requires_token(api) -> None:
    assert api.get("/auth/me").status_code == 401
    assert api.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_me_returns_own_tenant(api, acme_user) -> None:
    body = api.get("/auth/me", headers=acme_user).json()
    assert body["email"] == "acme.user@example.com"
    assert body["tenant_slug"] == "acme"


def test_token_with_forged_tenant_is_rejected(api, acme_user) -> None:
    """A validly signed token whose tenant_id does not match the user's row is refused."""
    me = api.get("/auth/me", headers=acme_user).json()
    forged = jwt.encode(
        {"sub": me["id"], "tenant_id": str(uuid4()), "role": "admin"},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    response = api.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
