"""Host scoping, declared targets, and the private-address policy (ENG-03, SEC-03)."""

import ipaddress

import pytest

from src.core.network_policy import (
    AddressPolicy,
    address_block_reason,
    navigation_block_reason,
    parse_domain_rule,
    parse_target,
    url_matches_targets,
)


class TestHostRules:
    def test_www_domain_covers_the_site_and_its_subdomains(self):
        rule = parse_domain_rule("www.hydroone.com")
        assert rule.allows("www.hydroone.com", 443)
        assert rule.allows("hydroone.com", 443)
        assert rule.allows("www.myaccount.hydroone.com", 443)
        assert not rule.allows("hydroone.com.evil.example", 443)
        assert not rule.allows("nothydroone.com", 443)

    def test_ip_and_port_must_match_exactly(self):
        rule = parse_domain_rule("127.0.0.1:8798")
        assert rule.allows("127.0.0.1", 8798)
        assert not rule.allows("127.0.0.1", 8799)
        assert not rule.allows("127.0.0.2", 8798)

    def test_single_label_hosts_match_exactly(self):
        rule = parse_domain_rule("localhost:8080")
        assert rule.allows("localhost", 8080)
        assert not rule.allows("foo.localhost", 8080)

    @pytest.mark.parametrize("bad", ["", "https://x.example", "x.example/path", "user@x.example", "*.127.0.0.1"])
    def test_rejects_anything_but_host_and_port(self, bad):
        with pytest.raises(ValueError):
            parse_domain_rule(bad)


class TestNavigation:
    RULES = [parse_domain_rule("bank.example")]

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "chrome://settings",
            "data:text/html,<script>alert(1)</script>",
            "javascript:alert(1)",
            "about:blank",
            "ftp://bank.example/x",
        ],
    )
    def test_only_http_is_ever_allowed(self, url):
        assert navigation_block_reason(url, self.RULES) is not None
        assert navigation_block_reason(url, ()) is not None

    def test_other_domains_are_refused(self):
        assert navigation_block_reason("https://evil.example/login", self.RULES)
        assert navigation_block_reason("http://169.254.169.254/latest/meta-data/", self.RULES)
        assert navigation_block_reason("https://login.bank.example/", self.RULES) is None

    def test_credentials_in_urls_are_refused(self):
        assert "credentials" in navigation_block_reason("https://bank.example@evil.example/", self.RULES)
        assert navigation_block_reason("https://user:pw@bank.example/", self.RULES)


class TestTargets:
    RULES = [parse_domain_rule("www.bank.example")]

    def test_relative_targets_match_on_the_blueprints_hosts(self):
        targets = [parse_target("/login")]
        assert url_matches_targets("https://www.bank.example/login?next=/home", targets, self.RULES)
        assert url_matches_targets("https://secure.bank.example/login", targets, self.RULES)
        assert not url_matches_targets("https://evil.example/login", targets, self.RULES)
        assert not url_matches_targets("https://www.bank.example/transfer", targets, self.RULES)

    def test_absolute_targets_pin_the_host(self):
        targets = [parse_target("https://auth.bank.example/pkmslogin.form")]
        assert url_matches_targets("https://auth.bank.example/pkmslogin.form", targets, self.RULES)
        assert not url_matches_targets("https://www.bank.example/pkmslogin.form", targets, self.RULES)

    def test_wildcards(self):
        targets = [parse_target("/pkmslogout*")]
        assert url_matches_targets("https://www.bank.example/pkmslogout?filename=x", targets, self.RULES)
        assert not url_matches_targets("https://www.bank.example/close-account", targets, self.RULES)

    @pytest.mark.parametrize("bad", ["", "login", "ftp://bank.example/x", "https://u:p@bank.example/x"])
    def test_invalid_targets(self, bad):
        with pytest.raises(ValueError):
            parse_target(bad)


class TestAddressBlocking:
    @pytest.mark.parametrize(
        "address",
        [
            "10.0.0.5",
            "172.16.4.2",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "168.63.129.16",
            "100.100.100.200",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fd00::1",
            "fe80::1",
            "::ffff:10.0.0.1",
            "64:ff9b::a9fe:a9fe",
            "2002:a9fe:a9fe::1",
        ],
    )
    def test_non_public_addresses_are_blocked(self, address):
        assert address_block_reason(ipaddress.ip_address(address)) is not None

    @pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
    def test_public_addresses_pass(self, address):
        assert address_block_reason(ipaddress.ip_address(address)) is None

    def test_loopback_exemption_is_only_loopback(self):
        assert address_block_reason(ipaddress.ip_address("127.0.0.1"), allow_loopback=True) is None
        assert address_block_reason(ipaddress.ip_address("::1"), allow_loopback=True) is None
        assert address_block_reason(ipaddress.ip_address("10.0.0.1"), allow_loopback=True) is not None
        assert address_block_reason(ipaddress.ip_address("169.254.169.254"), allow_loopback=True) is not None


class TestAddressPolicy:
    @pytest.mark.asyncio
    async def test_resolves_hostnames_on_each_check(self):
        policy = AddressPolicy(block_private=True)

        async def resolve(host):
            return (
                [ipaddress.ip_address("10.1.2.3")]
                if host == "intranet.example"
                else [ipaddress.ip_address("93.184.216.34")]
            )

        policy._resolve = resolve
        assert await policy.url_block_reason("https://intranet.example/admin")
        assert await policy.url_block_reason("https://www.example.com/") is None
        assert await policy.url_block_reason("wss://intranet.example/socket")

    @pytest.mark.asyncio
    async def test_localhost_names_count_as_loopback_without_dns(self):
        policy = AddressPolicy(block_private=True, allow_loopback=False)
        assert await policy.url_block_reason("http://localhost:8080/")
        assert await policy.url_block_reason("http://anything.localhost/")
        assert (
            await AddressPolicy(block_private=True, allow_loopback=True).url_block_reason("http://localhost/") is None
        )

    @pytest.mark.asyncio
    async def test_ip_literals_and_mapped_forms(self):
        policy = AddressPolicy(block_private=True)
        assert await policy.url_block_reason("http://169.254.169.254/latest/meta-data/")
        assert await policy.url_block_reason("http://[::ffff:127.0.0.1]/")
        assert await policy.url_block_reason("http://[fd00:ec2::254]/")

    @pytest.mark.asyncio
    async def test_disabled_policy_allows_everything(self):
        assert await AddressPolicy(block_private=False).url_block_reason("http://10.0.0.1/") is None
