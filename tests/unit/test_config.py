"""Configuration validation and ``.env.example`` parity."""

from __future__ import annotations

import subprocess
import sys

import pytest
from pydantic import SecretStr, ValidationError

from fde_assessment.common.config import (
    DEV_API_KEY_PEPPER,
    AppEnv,
    Role,
    Settings,
    parse_pairs,
)
from tests.conftest import REPO_ROOT

ENV_EXAMPLE = REPO_ROOT / ".env.example"


class TestEnvExampleParity:
    def _documented_keys(self) -> set[str]:
        return {
            line.split("=", 1)[0].strip()
            for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#") and "=" in line
        }

    def test_every_setting_is_documented(self) -> None:
        expected = {name.upper() for name in Settings.model_fields}
        assert expected - self._documented_keys() == set()

    def test_no_undocumented_or_stale_keys(self) -> None:
        """`extra="ignore"` means a stale key would be silently inert."""
        expected = {name.upper() for name in Settings.model_fields}
        assert self._documented_keys() - expected == set()

    def test_the_generator_is_up_to_date(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/gen_env_example.py", "--check"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


class TestPairParsing:
    def test_parses_a_token_table(self) -> None:
        assert parse_pairs("a:1,b:2") == {"a": "1", "b": "2"}

    def test_tolerates_whitespace_and_trailing_commas(self) -> None:
        assert parse_pairs(" a : 1 , b : 2 , ") == {"a": "1", "b": "2"}

    @pytest.mark.parametrize("raw", ["", "a", "a:", ":1", ",", "a:1,b"])
    def test_rejects_malformed_tables(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_pairs(raw)


class TestValidation:
    def test_defaults_are_the_assessment_values(self) -> None:
        settings = Settings()
        assert settings.rate_limit_tokens == 50_000
        assert settings.rate_limit_window_seconds == 60
        assert settings.primary_timeout_ms == 3_000

    def test_derived_views(self) -> None:
        settings = Settings()
        assert settings.primary_timeout_s == 3.0
        assert settings.token_roles["dev-admin-token"] is Role.admin
        assert settings.tenant_keys["dev-tenant-a-key"] == "tenant-a"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"mcp_downstream_url": "ftp://example.com"},
            {"mcp_downstream_url": "127.0.0.1:9000"},
            {"mcp_gateway_tokens": "not-a-pair"},
            {"llm_gateway_tenants": ""},
            {"rate_limit_tokens": 0},
            {"primary_timeout_ms": 10},
            {"pii_carry_buffer_chars": 8},
            {"rag_chunk_overlap": 512, "rag_chunk_size": 512},
            {"rag_default_top_k": 50, "rag_max_top_k": 10},
            {"mcp_gateway_port": 70_000},
        ],
    )
    def test_invalid_configuration_is_rejected_at_startup(self, overrides: dict) -> None:
        with pytest.raises(ValidationError):
            Settings(**overrides)

    def test_binds_loopback_by_default(self) -> None:
        """A service must not expose itself on every interface by default."""
        assert Settings().bind_host == "127.0.0.1"


class TestProductionSafety:
    def test_production_refuses_development_credentials(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Settings(app_env=AppEnv.production)
        message = str(excinfo.value)
        assert "MCP_GATEWAY_TOKENS" in message
        assert "LLM_GATEWAY_TENANTS" in message
        assert "API_KEY_PEPPER" in message

    def test_production_starts_once_credentials_are_replaced(self) -> None:
        settings = Settings(
            app_env=AppEnv.production,
            mcp_gateway_tokens="prod-admin-abc:admin,prod-viewer-def:viewer",
            llm_gateway_tenants="prod-key-1:acme,prod-key-2:globex",
            api_key_pepper=SecretStr("a-real-pepper-from-a-secret-manager"),
        )
        assert settings.app_env is AppEnv.production

    def test_a_single_leftover_default_still_blocks_production(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Settings(
                app_env=AppEnv.production,
                mcp_gateway_tokens="prod-admin-abc:admin",
                llm_gateway_tenants="prod-key-1:acme",
                api_key_pepper=SecretStr(DEV_API_KEY_PEPPER),
            )
        assert "API_KEY_PEPPER" in str(excinfo.value)

    def test_development_defaults_are_allowed_outside_production(self) -> None:
        assert Settings(app_env=AppEnv.development).app_env is AppEnv.development
        assert Settings(app_env=AppEnv.test).app_env is AppEnv.test


class TestSecretHandling:
    def test_the_pepper_is_not_printed_by_accident(self) -> None:
        settings = Settings()
        assert DEV_API_KEY_PEPPER not in repr(settings)
        assert DEV_API_KEY_PEPPER not in str(settings.api_key_pepper)
