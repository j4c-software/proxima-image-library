"""Configuration management for Asset Library."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root regardless of working directory
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# On Azure App Service, the deployed directory is read-only when running
# from a package, so keys saved via the maintenance UI persist to /home
# instead. Loaded second — load_dotenv never overrides an already-set var.
if Path("/home").exists() and os.getenv("WEBSITE_INSTANCE_ID"):
    load_dotenv("/home/proxima_stock_keys.env")


class Config:
    """Configuration class for Asset Library."""

    # Claude AI
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

    # Paths
    IMAGE_FOLDER = os.getenv("IMAGE_FOLDER", "./assets")
    LOCAL_INGEST_FOLDER = os.getenv("LOCAL_INGEST_FOLDER", "")
    SUPPORTED_FORMATS = os.getenv("SUPPORTED_FORMATS", ".jpg,.jpeg,.png,.gif,.webp").split(",")
    TAG_LIBRARY_PATH = os.getenv(
        "TAG_LIBRARY_PATH",
        "~/Applications/Image-Library/Config/tag_library.json",
    )

    # Test mode — uses local JSON store instead of SharePoint List
    TEST_MODE = os.getenv("TEST_MODE", "").lower() in ("1", "true", "yes")

    # Local development auth bypass.
    # Enabled by default in TEST_MODE so the app is usable without MSAL setup.
    DEV_AUTH_BYPASS = os.getenv(
        "DEV_AUTH_BYPASS",
        "true" if TEST_MODE else "false",
    ).lower() in ("1", "true", "yes")

    # Storage mode — "local" uses IMAGE_FOLDER; "sharepoint" uses Microsoft Graph API
    STORAGE_MODE = os.getenv("STORAGE_MODE", "local").lower()

    # Flask session
    FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-in-prod")
    SESSION_COOKIE_SECURE = os.getenv(
        "SESSION_COOKIE_SECURE",
        "false" if TEST_MODE else "true",
    ).lower() in ("1", "true", "yes")
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(20 * 1024 * 1024)))
    ADMIN_MAX_UPLOAD_BYTES = int(os.getenv("ADMIN_MAX_UPLOAD_BYTES", str(500 * 1024 * 1024)))
    MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(8 * MAX_UPLOAD_BYTES)))
    RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    RATE_LIMIT_AUTH_REQUESTS = int(os.getenv("RATE_LIMIT_AUTH_REQUESTS", "20"))
    RATE_LIMIT_STREAM_REQUESTS = int(os.getenv("RATE_LIMIT_STREAM_REQUESTS", "8"))

    # MSAL user authentication
    MSAL_CLIENT_ID = os.getenv("MSAL_CLIENT_ID", "")
    MSAL_CLIENT_SECRET = os.getenv("MSAL_CLIENT_SECRET", "")
    MSAL_TENANT_ID = os.getenv("MSAL_TENANT_ID", os.getenv("SHAREPOINT_TENANT_ID", ""))
    MSAL_REDIRECT_URI = os.getenv("MSAL_REDIRECT_URI", "http://localhost:5000/auth/callback")
    MSAL_AUTHORITY = f"https://login.microsoftonline.com/{os.getenv('MSAL_TENANT_ID', os.getenv('SHAREPOINT_TENANT_ID', ''))}"
    MSAL_SCOPES = ["User.Read"]

    # Admin identities allowed to access /maintenance and /api/maintenance/*
    # Values should match an MSAL claim such as email, preferred_username, upn, or unique_name.
    MAINTENANCE_ADMIN_USERS = [
        u.strip().lower()
        for u in os.getenv("MAINTENANCE_ADMIN_USERS", "").split(",")
        if u.strip()
    ]

    # CORS — comma-separated list of allowed origins for /api/* routes
    # e.g. https://yoursite.webflow.io,https://yourdomain.com
    CORS_ORIGINS = [
        o.strip()
        for o in os.getenv("CORS_ORIGINS", "http://localhost:5000").split(",")
        if o.strip()
    ]

    # SharePoint / Microsoft Graph API
    SHAREPOINT_TENANT_ID = os.getenv("SHAREPOINT_TENANT_ID", "")
    SHAREPOINT_CLIENT_ID = os.getenv("SHAREPOINT_CLIENT_ID", "")
    SHAREPOINT_CLIENT_SECRET = os.getenv("SHAREPOINT_CLIENT_SECRET", "")
    SHAREPOINT_DRIVE_ID = os.getenv("SHAREPOINT_DRIVE_ID", "")
    SHAREPOINT_SITE_ID = os.getenv("SHAREPOINT_SITE_ID", "")
    SHAREPOINT_LIST_NAME = os.getenv("SHAREPOINT_LIST_NAME", "Assets")
    SHAREPOINT_IMAGE_FOLDER = os.getenv("SHAREPOINT_IMAGE_FOLDER", "Images")
    SHAREPOINT_INGEST_FOLDER = os.getenv("SHAREPOINT_INGEST_FOLDER", "")
    INGEST_POLL_INTERVAL_SECONDS = int(os.getenv("INGEST_POLL_INTERVAL_SECONDS", "300"))

    # Base URL for generating links back to this app (used by MCP server)
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000")

    # MCP internal API secret
    MCP_INTERNAL_SECRET = os.getenv("MCP_INTERNAL_SECRET", "")

    # Stock photo API keys
    UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")

    @staticmethod
    def validate_runtime() -> None:
        """Validate runtime safety constraints that must hold in every mode."""
        if Config.DEV_AUTH_BYPASS and not Config.TEST_MODE:
            raise ValueError(
                "Invalid configuration: DEV_AUTH_BYPASS=true is only allowed when TEST_MODE=true"
            )
        if Config.MAX_UPLOAD_BYTES <= 0 or Config.MAX_REQUEST_BYTES < Config.MAX_UPLOAD_BYTES:
            raise ValueError("Invalid upload limits: MAX_REQUEST_BYTES must be >= MAX_UPLOAD_BYTES > 0")
        if (
            Config.RATE_LIMIT_WINDOW_SECONDS <= 0
            or Config.RATE_LIMIT_AUTH_REQUESTS <= 0
            or Config.RATE_LIMIT_STREAM_REQUESTS <= 0
        ):
            raise ValueError("Invalid rate limit configuration: all rate limit values must be > 0")
        if not Config.TEST_MODE:
            secret = Config.FLASK_SECRET_KEY or ""
            if secret == "dev-secret-change-in-prod" or len(secret) < 32:
                raise ValueError(
                    "Invalid configuration: FLASK_SECRET_KEY must be set to a strong random secret in non-test mode"
                )

    @staticmethod
    def validate():
        """Validate that all required configuration is set."""
        if Config.TEST_MODE:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise ValueError("Missing required environment variable: ANTHROPIC_API_KEY")
            return

        required = [
            "ANTHROPIC_API_KEY",
            "SHAREPOINT_TENANT_ID",
            "SHAREPOINT_CLIENT_ID",
            "SHAREPOINT_CLIENT_SECRET",
            "SHAREPOINT_SITE_ID",
            "SHAREPOINT_DRIVE_ID",
            "MCP_INTERNAL_SECRET",
        ]
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
