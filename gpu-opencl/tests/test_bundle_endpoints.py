from hashminer.config import BundleConfig


def test_default_bundle_endpoints_exclude_dead_dns_hosts():
    endpoints = BundleConfig().endpoints

    assert "https://api.securerpc.com/v1" not in endpoints
    assert "https://rpc.rsync-builder.xyz" not in endpoints
    assert endpoints == [
        "https://relay.flashbots.net",
        "https://rpc.beaverbuild.org",
        "https://rpc.titanbuilder.xyz",
    ]
