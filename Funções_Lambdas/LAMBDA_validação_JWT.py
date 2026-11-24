import os
import json
import time
import hmac
import hashlib
import base64

WIDGET_ID = "ba8ef306-8e4e-4101-8b0c-6224fa8288ac"
TOKEN_VALIDITY_SECONDS = 300  # 5 minutos


def encode_base64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_token(secret):
    now = int(time.time())

    header = {
        "alg": "HS256",
        "typ": "JWT"
    }

    payload = {
        "sub": WIDGET_ID,
        "iat": now,
        "exp": now + TOKEN_VALIDITY_SECONDS
    }

    header_encoded = encode_base64url(
        json.dumps(header, separators=(",", ":")).encode()
    )

    payload_encoded = encode_base64url(
        json.dumps(payload, separators=(",", ":")).encode()
    )

    content = f"{header_encoded}.{payload_encoded}"

    signature = hmac.new(
        secret.encode("utf-8"),
        content.encode("ascii"),
        hashlib.sha256
    ).digest()

    return f"{content}.{encode_base64url(signature)}"


def lambda_handler(event, context):
    # Exige que a chamada venha de uma API Gateway
    # configurada com autorizador JWT.
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    jwt_context = authorizer.get("jwt") or {}
    claims = jwt_context.get("claims") or {}

    if not isinstance(claims, dict) or not claims.get("sub"):
        return {
            "statusCode": 401,
            "headers": {"Cache-Control": "no-store"},
            "body": json.dumps({
                "error": "Autenticacao obrigatoria"
            })
        }

    secret = os.environ.get("CONNECT_WIDGET_SECRET")

    if not secret:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Configuracao indisponivel"
            })
        }

    token = create_token(secret)

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store"
        },
        "body": json.dumps({"data": token})
    }
