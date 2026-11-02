from tests.conftest import PASSWORD


def users(api, headers):
    return {u["email"]: u for u in api.get("/admin/users", headers=headers).json()}


def test_list_is_tenant_scoped_and_admin_only(api, acme_user, acme_admin) -> None:
    assert set(users(api, acme_admin)) == {"acme.user@example.com", "acme.admin@example.com"}
    assert api.get("/admin/users", headers=acme_user).status_code == 403
    assert api.get("/admin/users").status_code == 401


def test_create_user_who_can_log_in(api, acme_admin) -> None:
    created = api.post(
        "/admin/users",
        headers=acme_admin,
        json={"email": "New.Person@Example.com", "password": "longenough1", "role": "customer"},
    )
    assert created.status_code == 201 and created.json()["email"] == "new.person@example.com"
    login = api.post("/auth/login", json={"email": "new.person@example.com", "password": "longenough1"})
    assert login.status_code == 200
    assert login.json()["user"]["tenant_slug"] == "acme"  # always the creating admin's tenant


def test_create_validation_and_unique_email(api, acme_admin, globex_admin) -> None:
    assert (
        api.post(
            "/admin/users", headers=acme_admin, json={"email": "x@example.com", "password": "short"}
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/admin/users", headers=acme_admin, json={"email": "not-an-email", "password": "longenough1"}
        ).status_code
        == 422
    )
    same_tenant = api.post(
        "/admin/users", headers=acme_admin, json={"email": "acme.user@example.com", "password": "longenough1"}
    )
    other_tenant = api.post(
        "/admin/users",
        headers=globex_admin,
        json={"email": "acme.user@example.com", "password": "longenough1"},
    )
    assert same_tenant.status_code == 409 and other_tenant.status_code == 409  # login is by email alone


def test_deactivated_user_cannot_login_and_token_dies(api, acme_user, acme_admin) -> None:
    target = users(api, acme_admin)["acme.user@example.com"]
    assert api.get("/auth/me", headers=acme_user).status_code == 200
    assert (
        api.patch(f"/admin/users/{target['id']}", headers=acme_admin, json={"is_active": False}).status_code
        == 200
    )
    assert api.get("/auth/me", headers=acme_user).status_code == 401  # existing token revoked at once
    assert (
        api.post("/auth/login", json={"email": "acme.user@example.com", "password": PASSWORD}).status_code
        == 401
    )
    api.patch(f"/admin/users/{target['id']}", headers=acme_admin, json={"is_active": True})
    assert (
        api.post("/auth/login", json={"email": "acme.user@example.com", "password": PASSWORD}).status_code
        == 200
    )


def test_role_change_takes_effect(api, acme_user, acme_admin) -> None:
    target = users(api, acme_admin)["acme.user@example.com"]
    assert api.get("/admin/documents", headers=acme_user).status_code == 403
    api.patch(f"/admin/users/{target['id']}", headers=acme_admin, json={"role": "admin"})
    # Role is read from the database, not the (stale) token claim.
    assert api.get("/admin/documents", headers=acme_user).status_code == 200


def test_cannot_change_own_role_or_deactivate_self(api, acme_admin) -> None:
    me = users(api, acme_admin)["acme.admin@example.com"]
    assert (
        api.patch(f"/admin/users/{me['id']}", headers=acme_admin, json={"role": "customer"}).status_code
        == 409
    )
    assert (
        api.patch(f"/admin/users/{me['id']}", headers=acme_admin, json={"is_active": False}).status_code
        == 409
    )
    assert users(api, acme_admin)["acme.admin@example.com"]["role"] == "admin"


def test_last_active_admin_is_protected(api, acme_admin) -> None:
    """With two admins, one cannot strip the other once only one would remain... but never zero."""
    second = api.post(
        "/admin/users",
        headers=acme_admin,
        json={"email": "second@example.com", "password": "longenough1", "role": "admin"},
    ).json()
    from tests.conftest import login

    second_headers = login(api, "second@example.com", "longenough1")
    me = users(api, acme_admin)["acme.admin@example.com"]
    # second demotes the original admin: allowed, because second remains an active admin
    assert (
        api.patch(f"/admin/users/{me['id']}", headers=second_headers, json={"role": "customer"}).status_code
        == 200
    )
    # now second is the only admin; the (now customer) original cannot act, and second cannot self-demote
    assert (
        api.patch(
            f"/admin/users/{second['id']}", headers=second_headers, json={"role": "customer"}
        ).status_code
        == 409
    )


def test_last_admin_rule_blocks_deactivation_via_service(api, acme_admin) -> None:
    from sqlalchemy import select

    from app.db.models import User
    from app.db.session import get_session_factory
    from app.services.users import UserRuleViolation, update_user

    with get_session_factory()() as db:
        admin = db.scalars(select(User).where(User.email == "acme.admin@example.com")).one()
        stranger = db.scalars(select(User).where(User.email == "globex.admin@example.com")).one()
        # an actor other than the target (e.g. a future system job) still cannot remove the last admin
        try:
            update_user(db, actor=stranger, target=admin, role=None, is_active=False, password=None)
            raise AssertionError("expected the last-admin rule to fire")
        except UserRuleViolation as exc:
            assert "last active admin" in str(exc)


def test_admin_can_reset_a_password(api, acme_admin) -> None:
    target = users(api, acme_admin)["acme.user@example.com"]
    api.patch(f"/admin/users/{target['id']}", headers=acme_admin, json={"password": "brand-new-pass"})
    assert (
        api.post("/auth/login", json={"email": "acme.user@example.com", "password": PASSWORD}).status_code
        == 401
    )
    assert (
        api.post(
            "/auth/login", json={"email": "acme.user@example.com", "password": "brand-new-pass"}
        ).status_code
        == 200
    )


def test_cannot_touch_other_tenants_users(api, acme_admin, globex_admin) -> None:
    victim = users(api, globex_admin)["globex.user@example.com"]
    assert (
        api.patch(f"/admin/users/{victim['id']}", headers=acme_admin, json={"is_active": False}).status_code
        == 404
    )
    assert users(api, globex_admin)["globex.user@example.com"]["is_active"] is True
