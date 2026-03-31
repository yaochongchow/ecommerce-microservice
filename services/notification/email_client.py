import os
import boto3
from common.logger import get_logger

logger = get_logger(__name__)

EMAIL_MODE = os.environ.get("EMAIL_MODE", "mock")
SES_FROM_EMAIL = os.environ.get("SES_FROM_EMAIL", "noreply@example.com")

ses_client = boto3.client("ses")


def send_email(to: str, subject: str, body: str):
    if EMAIL_MODE == "ses":
        _send_via_ses(to, subject, body)
    else:
        _send_mock(to, subject, body)


def _send_mock(to: str, subject: str, body: str):
    logger.info("--- [MOCK EMAIL] ---")
    logger.info(f"To:      {to}")
    logger.info(f"Subject: {subject}")
    logger.info(f"Body:    {body}")
    logger.info("--- [END EMAIL] ---")


def _send_via_ses(to: str, subject: str, body: str):
    try:
        ses_client.send_email(
            Source=SES_FROM_EMAIL,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Text": {"Data": body}},
            },
        )
        logger.info(f"SES email sent to {to}")
    except Exception as e:
        logger.error(f"SES send failed: {e}")
        raise
