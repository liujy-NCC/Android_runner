"""Controller bind-address policy (DESIGN.md §10.2).

The confirmed contract:

* the default is loopback-only;
* ``0.0.0.0`` and ``::`` (and any other wildcard form) are ALWAYS refused, even
  when the operator opts in -- no flag may expose the controller on every
  interface;
* opting in permits only one explicitly named private interface address; public
  addresses and hostnames stay refused;
* a policy violation fails at service startup, before a socket is bound.

Network isolation itself is not verified here (see IMPLEMENTATION-CONTRACT §12).
"""

from __future__ import annotations

import socket

from harness import Harness

from runner.transports.server import (
    BindPolicyError,
    LOOPBACK_HOSTS,
    WILDCARD_HOSTS,
    make_server,
    validate_bind_address,
)


def _config_with_opt_in(h: Harness, opt_in: bool):
    limits = dict(h.config.limits)
    if opt_in:
        limits["allow_non_loopback_bind"] = 1
    return type(h.config)(**{**h.config.__dict__, "limits": limits})


def _verdict(host: str, config) -> str:
    try:
        validate_bind_address(host, config)
    except BindPolicyError:
        return "refused"
    return "allowed"


def test_loopback_is_the_default_and_is_allowed() -> None:
    h = Harness()
    try:
        config = _config_with_opt_in(h, opt_in=False)
        for host in LOOPBACK_HOSTS:
            assert _verdict(host, config) == "allowed", host
        assert config.limits.get("allow_non_loopback_bind") in (None, 0)
    finally:
        h.close()


def test_wildcard_addresses_are_always_refused_by_default() -> None:
    h = Harness()
    try:
        config = _config_with_opt_in(h, opt_in=False)
        for host in WILDCARD_HOSTS:
            assert _verdict(host, config) == "refused", host
        # The two specific shapes the requirement calls out.
        assert _verdict("0.0.0.0", config) == "refused"
        assert _verdict("::", config) == "refused"
    finally:
        h.close()


def test_wildcard_addresses_are_still_refused_even_with_the_operator_opt_in() -> None:
    """This is the security core: an opt-in must not authorise a wildcard bind."""

    h = Harness()
    try:
        config = _config_with_opt_in(h, opt_in=True)
        for host in WILDCARD_HOSTS:
            assert _verdict(host, config) == "refused", host
        assert _verdict("0.0.0.0", config) == "refused"
        assert _verdict("::", config) == "refused"
    finally:
        h.close()


def test_non_loopback_is_refused_without_an_explicit_opt_in() -> None:
    h = Harness()
    try:
        config = _config_with_opt_in(h, opt_in=False)
        for host in ("192.168.1.50", "10.0.0.7", "172.16.4.4", "8.8.8.8", "example.com"):
            assert _verdict(host, config) == "refused", host
    finally:
        h.close()


def test_opt_in_allows_only_a_single_private_interface_address() -> None:
    h = Harness()
    try:
        config = _config_with_opt_in(h, opt_in=True)
        # RFC1918 private ranges are permitted...
        for host in ("192.168.1.50", "10.0.0.7", "172.16.4.4"):
            assert _verdict(host, config) == "allowed", host
        # ...but public addresses and hostnames never are.
        for host in ("8.8.8.8", "1.1.1.1", "example.com", "runner.internal"):
            assert _verdict(host, config) == "refused", host
    finally:
        h.close()


def test_make_server_refuses_wildcard_before_binding_a_socket() -> None:
    """A policy violation must fail at startup, not silently serve."""

    h = Harness()
    try:
        config = _config_with_opt_in(h, opt_in=True)
        for host in ("0.0.0.0", "::"):
            try:
                make_server(h.api, config, host=host, port=0)
            except BindPolicyError as exc:
                assert "wildcard" in str(exc), exc
            else:  # pragma: no cover
                raise AssertionError(f"wildcard bind {host!r} should have been refused")
    finally:
        h.close()


def test_make_server_refuses_non_loopback_without_opt_in() -> None:
    h = Harness()
    try:
        config = _config_with_opt_in(h, opt_in=False)
        try:
            make_server(h.api, config, host="192.168.1.50", port=0)
        except BindPolicyError as exc:
            assert "non-loopback" in str(exc), exc
        else:  # pragma: no cover
            raise AssertionError("non-loopback bind should have been refused")
    finally:
        h.close()


def test_loopback_server_still_binds_and_serves() -> None:
    """The policy must not break the supported path."""

    h = Harness()
    try:
        server = make_server(h.api, h.config, host="127.0.0.1", port=0)
        try:
            host, port = server.server_address[0], server.server_address[1]
            assert host == "127.0.0.1"
            assert port > 0
            # The socket is genuinely listening on loopback.
            with socket.create_connection((host, port), timeout=5):
                pass
        finally:
            server.server_close()
    finally:
        h.close()


def test_wildcard_host_list_covers_the_required_forms() -> None:
    for host in ("0.0.0.0", "::", "*", "", None):
        if host is None:
            # ``None`` is not a str, so it cannot live in the tuple; it is
            # rejected explicitly by validate_bind_address instead.
            h = Harness()
            try:
                assert _verdict(host, h.config) == "refused"
            finally:
                h.close()
        else:
            assert host in WILDCARD_HOSTS, host
