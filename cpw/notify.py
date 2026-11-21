"""지메일 SMTP(앱 비밀번호) 발송."""
import os
import smtplib
from email.message import EmailMessage


def send(subject: str, body: str, to: str = "") -> None:
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = (to or os.environ.get("MAIL_TO", "") or user).strip()
    if not user or not pw:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD 환경변수가 없습니다(.env 또는 GitHub Secrets).")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
