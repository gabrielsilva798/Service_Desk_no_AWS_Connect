import hashlib
import hmac
import logging
import os
import re
import secrets
import time

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("OTP_TABLE", "")
SENDER = os.environ.get("SES_FROM_EMAIL", "")
DOMAIN = os.environ.get("CUSTOMER_PROFILES_DOMAIN", "")
HMAC_KEY = os.environ.get("OTP_HMAC_KEY", "")

OTP_LIFETIME_SECONDS = 300
PROFILE_COOLDOWN_SECONDS = 60
MAX_ATTEMPTS = 3


def _clients():
    return (
        boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME),
        boto3.client("customer-profiles", region_name=REGION),
        boto3.client("ses", region_name=REGION),
    )


def _response(status):
    # Resposta simples e compativel com o atributo External de Lambda do Connect.
    return {"status": status}


def _digest(contact_id, profile_id, otp):
    payload = f"{contact_id}:{profile_id}:{otp}".encode("utf-8")
    return hmac.new(HMAC_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _profile(profiles_client, profile_id, matricula):
    # Nao aceita e-mail informado pelo contato: le o perfil oficial da AWS.
    result = profiles_client.batch_get_profile(DomainName=DOMAIN, ProfileIds=[profile_id])
    records = result.get("Profiles", [])
    if len(records) != 1 or records[0].get("ProfileId") != profile_id:
        return None
    profile = records[0]
    if str(profile.get("AccountNumber") or "") != matricula:
        return None
    return profile


def _send(event, table, profiles_client, ses_client, now, contact_id, profile_id, matricula):
    if not re.fullmatch(r"\d{4}", matricula):
        return _response("UNAVAILABLE")

    profile = _profile(profiles_client, profile_id, matricula)
    if not profile:
        return _response("UNAVAILABLE")

    # Preferir e-mail de recuperacao independente da conta corporativa bloqueada.
    address = (profile.get("PersonalEmailAddress") or profile.get("EmailAddress") or "").strip()
    if not address or "@" not in address:
        return _response("UNAVAILABLE")

    # Limite minimo entre envios por funcionario, inclusive em contatos distintos.
    rate_id = f"RATE#{profile_id}"
    try:
        table.put_item(
            Item={"challengeId": rate_id, "expiresAt": now + PROFILE_COOLDOWN_SECONDS},
            ConditionExpression="attribute_not_exists(challengeId) OR expiresAt <= :now",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _response("WAIT")
        raise

    otp = f"{secrets.randbelow(1000000):06d}"
    try:
        # Chave vinculada ao ContactId; um codigo de outro chat nao sera aceito.
        table.put_item(
            Item={
                "challengeId": contact_id,
                "profileId": profile_id,
                "accountNumber": matricula,
                "digest": _digest(contact_id, profile_id, otp),
                "attempts": 0,
                "used": False,
                "expiresAt": now + OTP_LIFETIME_SECONDS,
            },
            ConditionExpression="attribute_not_exists(challengeId) OR expiresAt <= :now",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _response("WAIT")
        raise

    try:
        # Mensagem montada separadamente para evitar erros de chaves aninhadas.
        body_text = (
            f"Seu codigo de verificacao e: {otp}\n\n"
            "Valido por 5 minutos e para uso unico. "
            "Nao compartilhe com atendentes ou terceiros.\n\n"
            "Se voce nao solicitou esta verificacao, ignore este e-mail."
        )
        subject = {
            "Data": "Codigo de verificacao - Service Desk de TI",
            "Charset": "UTF-8",
        }
        body = {
            "Text": {
                "Data": body_text,
                "Charset": "UTF-8",
            }
        }
        message = {"Subject": subject, "Body": body}
        ses_client.send_email(
            Source=SENDER,
            Destination={"ToAddresses": [address]},
            Message=message,
        )
    except ClientError as exc:
        # Diagnostico sem expor e-mail, OTP, evento ou mensagem integral do SES.
        error = exc.response.get("Error", {})
        error_code = str(error.get("Code") or "Unknown")
        description = str(error.get("Message") or "").lower()
        if "not verified" in description or "not verified" in error_code.lower():
            category = "IDENTITY_NOT_VERIFIED"
        elif "not authorized" in description or "access denied" in description or "accessdenied" in error_code.lower():
            category = "IAM_DENIED"
        elif "sandbox" in description:
            category = "SES_SANDBOX"
        elif "sending paused" in description or "account is paused" in description:
            category = "SES_SENDING_PAUSED"
        else:
            category = "SES_OTHER"
        # Logue apenas metadados seguros do AccessDenied, nunca o erro integral.
        # A mensagem do SES pode revelar destinatario ou identidade do remetente.
        reason_keys = (
            ("service control policy", "SCP"),
            ("organization policy", "ORGANIZATION_POLICY"),
            ("permissions boundary", "PERMISSIONS_BOUNDARY"),
            ("session policy", "SESSION_POLICY"),
            ("vpc endpoint policy", "VPC_ENDPOINT_POLICY"),
            ("resource-based policy", "RESOURCE_POLICY"),
            ("identity-based policy", "IDENTITY_POLICY"),
            ("explicit deny", "EXPLICIT_DENY"),
            ("no policy allows", "NO_MATCHING_ALLOW"),
            ("not authorized", "NOT_AUTHORIZED_UNSPECIFIED"),
        )
        reason = next((name for phrase, name in reason_keys if phrase in description), "NOT_STATED")
        denied_action = re.search(r"ses:(SendEmail|SendRawEmail|SendBulkEmail)", description, re.I)
        arn_match = re.search(
            r"arn:aws:ses:([a-z0-9-]+):([0-9]{12}):(identity|configuration-set)/([^\s,;\"']+)",
            description, re.I,
        )
        resource_kind = arn_match.group(3) if arn_match else "UNSTATED"
        # Diagnostico de recurso: so indicadores, nunca valores de e-mail ou OTP.
        # Permite diferenciar ARN do remetente, ARN do destinatario e dominio.
        if arn_match and resource_kind == "identity":
            denied_identity = arn_match.group(4).rstrip(".>)").strip().lower()
            identity_matches_sender = denied_identity == SENDER.strip().lower()
            identity_matches_recipient = denied_identity == address.strip().lower()
            denied_identity_is_domain = "@" not in denied_identity
            denied_region_matches = arn_match.group(1).lower() == REGION.lower()
            denied_account_matches = arn_match.group(2) == "230623978109"
        else:
            identity_matches_sender = "UNKNOWN"
            identity_matches_recipient = "UNKNOWN"
            denied_identity_is_domain = "UNKNOWN"
            denied_region_matches = "UNKNOWN"
            denied_account_matches = "UNKNOWN"
        sender_has_extra_spaces = SENDER != SENDER.strip()
        request_id = (exc.response.get("ResponseMetadata") or {}).get("RequestId", "UNSTATED")
        table.delete_item(Key={"challengeId": contact_id})
        log.warning(
            "SES_SEND_FAILED code=%s category=%s reason=%s denied_action=%s "
            "resource_type=%s matches_sender=%s matches_recipient=%s "
            "is_domain=%s region_ok=%s account_ok=%s sender_spaces=%s request_region=%s aws_request_id=%s",
            error_code, category, reason,
            denied_action.group(0) if denied_action else "UNSTATED",
            resource_kind, identity_matches_sender, identity_matches_recipient,
            denied_identity_is_domain, denied_region_matches, denied_account_matches,
            sender_has_extra_spaces, REGION, request_id,
        )
        return _response("UNAVAILABLE")

    return _response("SENT")


def _verify(params, table, now, contact_id, profile_id, matricula):
    otp = str(params.get("code") or "").strip()
    if not re.fullmatch(r"\d{6}", otp):
        return _response("INVALID")

    result = table.get_item(Key={"challengeId": contact_id}, ConsistentRead=True)
    challenge = result.get("Item")
    if not challenge:
        return _response("UNAVAILABLE")

    if (challenge.get("profileId") != profile_id
            or challenge.get("accountNumber") != matricula
            or challenge.get("used") is True):
        return _response("UNAVAILABLE")

    if int(challenge.get("expiresAt", 0)) <= now:
        return _response("EXPIRED")
    if int(challenge.get("attempts", 0)) >= MAX_ATTEMPTS:
        return _response("LOCKED")

    if hmac.compare_digest(challenge.get("digest", ""), _digest(contact_id, profile_id, otp)):
        try:
            # Atualizacao condicional atomica impede reutilizacao da mesma prova.
            table.update_item(
                Key={"challengeId": contact_id},
                UpdateExpression="SET used = :yes",
                ConditionExpression=(
                    "profileId = :profile AND accountNumber = :account "
                    "AND digest = :digest AND used = :no "
                    "AND attempts < :max AND expiresAt > :now"
                ),
                ExpressionAttributeValues={
                    ":profile": profile_id,
                    ":account": matricula,
                    ":digest": challenge["digest"],
                    ":yes": True,
                    ":no": False,
                    ":max": MAX_ATTEMPTS,
                    ":now": now,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return _response("UNAVAILABLE")
            raise
        return _response("VERIFIED")

    try:
        result = table.update_item(
            Key={"challengeId": contact_id},
            UpdateExpression="ADD attempts :one",
            ConditionExpression=(
                "profileId = :profile AND accountNumber = :account AND used = :no "
                "AND attempts < :max AND expiresAt > :now"
            ),
            ExpressionAttributeValues={
                ":one": 1,
                ":profile": profile_id,
                ":account": matricula,
                ":no": False,
                ":max": MAX_ATTEMPTS,
                ":now": now,
            },
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _response("LOCKED")
        raise
    attempts = int(result.get("Attributes", {}).get("attempts", MAX_ATTEMPTS))
    return _response("LOCKED" if attempts >= MAX_ATTEMPTS else "INVALID")


def lambda_handler(event, context):
    if not (TABLE_NAME and SENDER and DOMAIN and len(HMAC_KEY) >= 32):
        log.error("Configuracao de ambiente incompleta.")
        return _response("UNAVAILABLE")

    details = event.get("Details") or {}
    params = details.get("Parameters") or {}
    contact = details.get("ContactData") or {}
    action = str(params.get("action") or "").upper()
    contact_id = str(contact.get("ContactId") or "").strip()
    profile_id = str(params.get("profileId") or "").strip()
    matricula = str(params.get("matricula") or "").strip()
    if not contact_id or not profile_id or not re.fullmatch(r"\d{4}", matricula):
        return _response("UNAVAILABLE")
    if action not in ("SEND_OTP", "VERIFY_OTP"):
        return _response("UNAVAILABLE")

    now = int(time.time())
    try:
        table, profiles_client, ses_client = _clients()
        if action == "SEND_OTP":
            return _send(event, table, profiles_client, ses_client, now, contact_id, profile_id, matricula)
        return _verify(params, table, now, contact_id, profile_id, matricula)
    except Exception:
        # Nunca imprimir o evento ou codigo recebido no log.
        log.exception("Falha operacional da verificacao OTP.")
        return _response("UNAVAILABLE")
