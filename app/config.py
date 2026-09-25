from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    app_secret_key: str
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # Database
    database_url: str

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Google OAuth
    google_client_id: str
    google_client_secret: str

    # JWT
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_api_id: str = ""
    telegram_api_hash: str = ""

    # Instagram (Meta developer app)
    instagram_app_id: str = ""
    instagram_app_secret: str = ""

    # Webhooks
    webhook_secret: str = ""
    # Public origin of this API (no trailing slash). Telegram/Twilio webhooks and every
    # OAuth callback are built from this. Blank in development → http://localhost:{APP_PORT}.
    # Production: https://api.setod.com
    public_base_url: str = ""

    # Connector credential encryption (Fernet key)
    encryption_key: str

    # Resend — platform-owned transactional email for owner notifications.
    # When blank, email notifications raise an error (visible in the UI test).
    resend_api_key: str = ""
    # Sender shown in the From: header. Must match a verified domain in your Resend account.
    # https://resend.com/domains — verify setod.com, then set to "Setod <notifications@setod.com>"
    resend_from_email: str = "Setod <onboarding@resend.dev>"

    # Optional platform-owned OAuth apps for MCP catalog servers that refuse DCR
    # (Slack requires a fixed Slack app). When empty, the user pastes their own.
    slack_mcp_client_id: str = ""
    slack_mcp_client_secret: str = ""
    notion_mcp_client_id: str = ""
    notion_mcp_client_secret: str = ""
    linear_mcp_client_id: str = ""
    linear_mcp_client_secret: str = ""
    atlassian_mcp_client_id: str = ""
    atlassian_mcp_client_secret: str = ""

    # Browser origins allowed to call this API. Comma-separated so it can be set per
    # environment; blank falls back to the development defaults below.
    cors_origins: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def allowed_origins(self) -> list[str]:
        """Origins the browser may call from.

        `localhost` and `127.0.0.1` are the same machine but different origins to the browser,
        and Next serves happily on either. Allowing only one turns a URL the developer typed
        from habit into every request failing with an opaque "Failed to fetch", so both are
        listed in development. Production gets no default — it must be set explicitly.
        """
        if self.cors_origins:
            return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        if self.is_production:
            return []
        return [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]

    @property
    def api_public_origin(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        return f"http://localhost:{self.app_port}"

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.api_public_origin}/auth/google/callback"

    @property
    def connector_mcp_redirect_uri(self) -> str:
        return f"{self.api_public_origin}/connectors/oauth/mcp/callback"

    @property
    def instagram_redirect_uri(self) -> str:
        return f"{self.api_public_origin}/connectors/oauth/instagram/callback"

    @property
    def frontend_origin(self) -> str:
        if self.allowed_origins:
            return self.allowed_origins[0]
        return "http://localhost:3000"

    @property
    def cookie_domain(self) -> str | None:
        """Shared parent domain for the auth cookie in production.

        Setting domain=".setod.com" makes the cookie visible to both setod.com
        (Next.js middleware) and api.setod.com (the API), which is required for
        the session middleware to see the token after the OAuth redirect.
        In development, None lets the browser default to the exact hostname.
        """
        if not self.is_production:
            return None
        from urllib.parse import urlparse
        parsed = urlparse(self.frontend_origin)
        host = parsed.hostname or ""
        # Strip leading www/subdomain to get the registrable domain
        parts = host.split(".")
        if len(parts) >= 2:
            return "." + ".".join(parts[-2:])
        return None

    def mcp_oauth_app(self, catalog_key: str) -> tuple[str, str]:
        """Pre-registered confidential client for a catalog MCP server, if we have one."""
        apps = {
            "slack": (self.slack_mcp_client_id, self.slack_mcp_client_secret),
            "notion": (self.notion_mcp_client_id, self.notion_mcp_client_secret),
            "linear": (self.linear_mcp_client_id, self.linear_mcp_client_secret),
            "atlassian": (self.atlassian_mcp_client_id, self.atlassian_mcp_client_secret),
        }
        client_id, client_secret = apps.get(catalog_key, ("", ""))
        return (client_id.strip(), client_secret.strip())


settings = Settings()
