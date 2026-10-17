from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./turnoverops.db"
    jwt_secret: str = "dev-only-change-me-to-a-long-random-string"
    jwt_ttl_minutes: int = 720
    google_client_id: str = ""
    frontend_origin: str = "http://localhost:5173"
    dev_mode: bool = False
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_bot_username: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""
    google_calendar_redirect_uri: str = "http://localhost:5173/integrations/google-calendar"
    max_assignment_attempts: int = 3
    confirmation_timeout_minutes: int = 60
    max_notification_retries: int = 3
    notification_retry_seconds: int = 60
    enable_scheduler: bool = True
    scheduler_interval_seconds: int = 60
    feed_timeout_seconds: float = 10.0
    feed_max_bytes: int = 1_000_000
    invite_ttl_hours: int = 24 * 7


def get_settings() -> Settings:
    return Settings()
