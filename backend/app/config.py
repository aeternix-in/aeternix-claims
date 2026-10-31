"""Runtime settings, read from environment variables.

Every integration that needs a key (OCR vendor, LLM, WhatsApp, SMTP) is OFF by
default, so the product runs end to end on a laptop with no accounts at all.
"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    def __init__(self) -> None:
        self.database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/aeternix.db")
        self.data_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve()
        self.secret_key: str = os.getenv("SECRET_KEY", "dev-insecure-change-me")
        self.token_ttl_hours: int = int(os.getenv("TOKEN_TTL_HOURS", "12"))
        # Fernet key (base64, 32 bytes). When set, every stored file is encrypted at rest.
        self.file_encryption_key: str | None = os.getenv("FILE_ENCRYPTION_KEY") or None

        # Document intelligence
        self.ocr_provider: str = os.getenv("OCR_PROVIDER", "auto")  # auto | none
        self.llm_provider: str = os.getenv("LLM_PROVIDER", "none")  # none | anthropic
        self.anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
        self.anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")

        # Notifications (owner digest)
        self.notify_provider: str = os.getenv("NOTIFY_PROVIDER", "console")  # console | smtp | whatsapp
        self.whatsapp_token: str | None = os.getenv("WHATSAPP_TOKEN") or None
        self.whatsapp_phone_number_id: str | None = os.getenv("WHATSAPP_PHONE_NUMBER_ID") or None
        self.whatsapp_api_version: str = os.getenv("WHATSAPP_API_VERSION", "v21.0")
        self.smtp_host: str | None = os.getenv("SMTP_HOST") or None
        self.smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user: str | None = os.getenv("SMTP_USER") or None
        self.smtp_password: str | None = os.getenv("SMTP_PASSWORD") or None
        self.smtp_from: str = os.getenv("SMTP_FROM", "digest@aeternix.in")

        # Business rules that are Aeternix choices, not scheme rules
        self.high_value_threshold_paise: int = int(os.getenv("HIGH_VALUE_THRESHOLD_PAISE", str(1_00_000 * 100)))
        self.recon_tolerance_paise: int = int(os.getenv("RECON_TOLERANCE_PAISE", "100"))  # Rs 1
        self.recon_date_window_days: int = int(os.getenv("RECON_DATE_WINDOW_DAYS", "10"))
        self.bank_credit_expected_days: int = int(os.getenv("BANK_CREDIT_EXPECTED_DAYS", "7"))
        self.claim_window_warning_days: int = int(os.getenv("CLAIM_WINDOW_WARNING_DAYS", "3"))

        self.timezone: str = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
        self.digest_hour: int = int(os.getenv("DIGEST_HOUR", "8"))
        self.demo_mode: bool = _bool("DEMO_MODE", True)
        self.cors_origins: list[str] = [o for o in os.getenv(
            "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o]
        self.web_dist: Path = Path(os.getenv("WEB_DIST", str(Path(__file__).resolve().parents[2] / "web" / "dist")))

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"


settings = Settings()
