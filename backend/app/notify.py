"""Send the owner digest. Console by default; SMTP or WhatsApp Cloud API when configured.

WhatsApp note: business-initiated messages outside a 24-hour customer window must use a
pre-approved template. Set WHATSAPP_TEMPLATE to an approved template whose single body
parameter is the digest text, or the API will refuse the message.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import httpx

from .config import settings

log = logging.getLogger("aeternix.notify")


def send(channel: str, to: str, subject: str, text: str) -> dict:
    provider = channel or settings.notify_provider
    if provider == "whatsapp":
        if not (settings.whatsapp_token and settings.whatsapp_phone_number_id):
            return {"ok": False, "error": "WhatsApp is not configured (WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID)."}
        url = f"https://graph.facebook.com/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}/messages"
        template = os.getenv("WHATSAPP_TEMPLATE")
        if template:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "template",
                       "template": {"name": template, "language": {"code": os.getenv("WHATSAPP_TEMPLATE_LANG", "en")},
                                    "components": [{"type": "body", "parameters": [{"type": "text", "text": text[:1000]}]}]}}
        else:
            payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}}
        r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {settings.whatsapp_token}"}, timeout=30)
        return {"ok": r.status_code < 300, "status": r.status_code, "response": r.text[:500]}
    if provider == "smtp":
        if not settings.smtp_host:
            return {"ok": False, "error": "SMTP is not configured (SMTP_HOST)."}
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, settings.smtp_from, to
        msg.set_content(text)
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password or "")
            s.send_message(msg)
        return {"ok": True}
    log.info("DIGEST to %s\n%s", to, text)
    return {"ok": True, "provider": "console"}
