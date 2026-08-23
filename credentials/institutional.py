"""Institutional-email gate for workshop-certificate claims.

The credential's value is that it's tied to an *institutional* email. The lightest
workable rule (A2_SCOPING §4a option 1) is to **reject free/consumer providers**;
anything else is treated as institutional. Editable, and could later be tightened
to an allow-list of known institution domains.

``FREE_EMAIL_DOMAINS`` can be extended from the environment
(``CREDENTIALS_EXTRA_FREE_EMAIL_DOMAINS`` = comma-separated) without a code change.
"""
from __future__ import annotations

import os

FREE_EMAIL_DOMAINS = {
    # global consumer providers
    "gmail.com", "googlemail.com",
    "outlook.com", "outlook.co.uk", "hotmail.com", "hotmail.co.uk",
    "live.com", "live.co.uk", "msn.com",
    "yahoo.com", "yahoo.co.uk", "yahoo.co.in", "ymail.com", "rocketmail.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "aim.com",
    "proton.me", "protonmail.com", "pm.me",
    "gmx.com", "gmx.co.uk", "gmx.de", "mail.com", "zoho.com", "fastmail.com",
    "yandex.com", "yandex.ru",
    "qq.com", "163.com", "126.com", "sina.com", "foxmail.com",
    # common ISP mailboxes
    "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "cox.net",
}


def _extra_domains() -> set[str]:
    raw = (os.environ.get("CREDENTIALS_EXTRA_FREE_EMAIL_DOMAINS") or "").strip()
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def email_domain(email: str) -> str:
    return (email or "").strip().rsplit("@", 1)[-1].lower()


def is_free_provider(email: str) -> bool:
    return email_domain(email) in (FREE_EMAIL_DOMAINS | _extra_domains())


def is_institutional_email(email: str) -> bool:
    """True if the email looks institutional (has a domain and isn't a known
    free/consumer provider)."""
    domain = email_domain(email)
    if not domain or "@" not in (email or "") or "." not in domain:
        return False
    return not is_free_provider(email)
