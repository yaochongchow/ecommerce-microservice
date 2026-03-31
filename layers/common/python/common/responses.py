"""Standard Lambda response helpers."""

def success(body: dict) -> dict:
    return {"statusCode": 200, "body": body}

def error(message: str, status_code: int = 500) -> dict:
    return {"statusCode": status_code, "body": {"error": message}}
