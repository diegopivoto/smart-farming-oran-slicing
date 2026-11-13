def test_package_has_version() -> None:
    import smartfarm

    assert smartfarm.__version__ == "0.1.0"
