from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./turnoverops.db"
    jwt_secret: str = "dev-only-change-me-to-a-long-random-string"
    jwt_ttl_minutes: int = 720
    google_client_id: str = ""
    frontend_origin: str = "http://localhost:5173"
    public_frontend_url: str = ""
    dev_mode: bool = False
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_uri: str = "http://localhost:5173/integrations/google-calendar"
    cleaner_google_calendar_redirect_uri: str = "http://localhost:8000/public/cleaner-calendar/callback"
    max_assignment_attempts: int = 3
    confirmation_timeout_minutes: int = 60
    max_notification_retries: int = 3
    notification_retry_seconds: int = 60
    enable_scheduler: bool = True
    scheduler_interval_seconds: int = 60
    feed_timeout_seconds: float = 10.0
    feed_max_bytes: int = 1_000_000
    invite_ttl_hours: int = 24 * 7
    scheduling_model: str = "gemini-2.5-flash"
    scheduling_max_output_tokens: int = 2048
    scheduling_max_repair_attempts: int = 2
    scheduling_temperature: float = 0
    planning_horizon_hours: int = 24 * 90
    planning_lock_minutes: int = 15
    email_from: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    app_base_url: str = "http://localhost:8000"
    app_timezone: str = "Africa/Addis_Ababa"
    rejection_token_ttl_hours: int = 24 * 14
    scheduling_input_usd_per_million: float | None = None
    scheduling_output_usd_per_million: float | None = None

    def resolved_scheduling_model(self) -> str:
        return self.scheduling_model or self.gemini_model or "gemini-2.5-flash"


def get_settings() -> Settings:
    return Settings()


def frontend_base_url(settings: Settings) -> str:
    configured = settings.public_frontend_url.strip().rstrip("/")
    if configured:
        return configured
    origins = [origin.strip().rstrip("/") for origin in settings.frontend_origin.split(",") if origin.strip()]
    for origin in origins:
        if urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return origin
    return origins[0] if origins else "http://localhost:5173"
