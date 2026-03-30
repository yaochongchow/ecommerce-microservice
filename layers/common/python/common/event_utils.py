"""Helpers for parsing EventBridge events, including SQS-wrapped delivery."""

import json


def unwrap_event(event: dict) -> dict:
    """Extract the EventBridge event from an SQS record wrapper, or return as-is.

    When EventBridge delivers to SQS and SQS triggers Lambda, the Lambda receives:
      { "Records": [{ "body": "<EventBridge event as JSON string>", ... }] }

    This handles both that format and direct EventBridge invocations.
    """
    if "Records" in event:
        return json.loads(event["Records"][0]["body"])
    return event


def get_detail_type(event: dict) -> str:
    return event.get("detail-type", "")


def get_detail(event: dict) -> dict:
    return event.get("detail", {})


def get_source(event: dict) -> str:
    return event.get("source", "")
