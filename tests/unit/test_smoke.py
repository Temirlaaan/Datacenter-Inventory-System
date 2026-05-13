"""Verifies the project imports cleanly. Replaced with real tests as features land."""


def test_app_main_imports() -> None:
    import app.main  # noqa: F401


def test_app_packages_import() -> None:
    import app
    import app.api
    import app.api.v1
    import app.auth
    import app.db
    import app.domain
    import app.netbox
    import app.observability
    import app.services
    import app.web

    assert app is not None
    assert app.api is not None
    assert app.api.v1 is not None
    assert app.auth is not None
    assert app.db is not None
    assert app.domain is not None
    assert app.netbox is not None
    assert app.observability is not None
    assert app.services is not None
    assert app.web is not None
