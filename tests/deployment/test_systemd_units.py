from pathlib import Path


UNITS = Path("deployment/systemd")


def unit(name: str) -> str:
    return (UNITS / name).read_text(encoding="utf-8")


def test_services_are_non_root_bounded_and_journaled() -> None:
    for name in ("smartfarm-flexric.service", "smartfarm-kpm-bridge.service"):
        document = unit(name)
        assert "User=inatel-crr" in document
        assert "Group=inatel-crr" in document
        assert "Restart=on-failure" in document
        assert "StartLimitBurst=3" in document
        assert "StandardOutput=journal" in document
        assert "StandardError=journal" in document
        assert "NoNewPrivileges=true" in document
        assert "ProtectSystem=strict" in document
        assert "CapabilityBoundingSet=\n" in document
        assert "AmbientCapabilities=\n" in document
        assert "sudo" not in document


def test_flexric_uses_only_installed_verified_paths() -> None:
    document = unit("smartfarm-flexric.service")

    assert (
        "ExecStart=/usr/local/bin/nearRT-RIC "
        "-c /usr/local/etc/flexric/flexric.conf "
        "-p /usr/local/lib/flexric/" in document
    )
    assert "StateDirectory=smartfarm-flexric" in document
    # the RIC stdout iApp opens the relative path log.txt: cwd must be writable
    assert "WorkingDirectory=/var/lib/smartfarm-flexric" in document
    assert "ReadWritePaths=/var/lib/smartfarm-flexric" in document


def test_bridge_waits_for_e2_and_is_ordered_after_flexric() -> None:
    document = unit("smartfarm-kpm-bridge.service")

    assert "Requires=smartfarm-flexric.service" in document
    assert "After=smartfarm-flexric.service" in document
    assert "Environment=SMARTFARM_E2_DISCOVERY_TIMEOUT_S=1800" in document
    assert (
        "ExecStart=/usr/local/libexec/smartfarm/smartfarm_kpm_bridge "
        "-c /usr/local/etc/flexric/flexric.conf "
        "-p /usr/local/lib/flexric/" in document
    )
