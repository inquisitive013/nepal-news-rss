import pytest

from newsroom import llm


def test_scrub_empty_credentials_removes_only_blank_values():
    env = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_WORKSPACE_ID": "  ", "OTHER": ""}
    removed = llm.scrub_empty_credentials(env)
    assert set(removed) == {"ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID"}
    assert env == {"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "OTHER": ""}


def test_auth_mode_precedence():
    assert llm.auth_mode({}) == "sdk_default"
    assert llm.auth_mode({"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": "org"}) == "federation"
    assert llm.auth_mode({"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1"}) == "sdk_default"
    assert llm.auth_mode({"ANTHROPIC_API_KEY": "sk-ant", "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": "org"}) == "api_key"


def test_oidc_request_url_appends_audience():
    url = "https://pipelinesghubeus2.actions.githubusercontent.com/abc/idtoken/123?api-version=2.0"
    assert llm.oidc_request_url(url, "https://api.anthropic.com") == url + "&audience=https%3A%2F%2Fapi.anthropic.com"
    assert llm.oidc_request_url("https://x.example/token", "aud") == "https://x.example/token?audience=aud"


def test_github_provider_requires_runner_env():
    assert llm.github_oidc_token_provider({}) is None
    provider = llm.github_oidc_token_provider({"ACTIONS_ID_TOKEN_REQUEST_URL": "https://x.example/token?api-version=2.0", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t"})
    assert callable(provider)


def test_make_credentials_selects_federation_only_when_configured():
    assert llm.make_credentials({"ANTHROPIC_API_KEY": "sk-ant"}) is None
    assert llm.make_credentials({}) is None
    env = {
        "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1",
        "ANTHROPIC_ORGANIZATION_ID": "00000000-0000-0000-0000-000000000000",
        "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_1",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://x.example/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t",
    }
    creds = llm.make_credentials(env)
    from anthropic.lib.credentials import WorkloadIdentityCredentials

    assert isinstance(creds, WorkloadIdentityCredentials)
    with pytest.raises(llm.LLMError):
        llm.make_credentials({"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_1", "ANTHROPIC_ORGANIZATION_ID": "org"})
