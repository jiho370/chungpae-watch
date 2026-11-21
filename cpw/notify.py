"""지메일 SMTP(앱 비밀번호) 발송."""
import os
import smtplib
from email.message import EmailMessage


def send(subject: str, body: str, to: str = "") -> None:
    env = os.environ.get
    user = (env("SMTP_USER") or env("GMAIL_USER") or "").strip()
    pw = (env("SMTP_PASS") or env("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    to = (to or env("EMAIL_TO") or env("MAIL_TO") or user).strip()
    if not user or not pw:
        raise RuntimeError("SMTP_USER / SMTP_PASS 환경변수가 없습니다(.env 또는 GitHub Secrets).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
