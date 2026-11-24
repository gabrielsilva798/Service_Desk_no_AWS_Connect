import os
import re
import boto3
import logging
from botocore.exceptions import ClientError, BotoCoreError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

profiles = boto3.client("customer-profiles")


def lambda_handler(event, context):
    estado = event.get("sessionState") or {}
    intent = estado.get("intent") or {}
    slots = intent.get("slots") or {}
    atributos = estado.get("sessionAttributes") or {}

    campo = slots.get("MatriculaFuncionario") or {}
    valor = campo.get("value") or {}
    matricula = str(valor.get("interpretedValue") or "").strip()

    def resposta(acao, mensagem=None):
        resultado = {
            "sessionState": {
                "dialogAction": acao,
                "intent": intent,
                "sessionAttributes": atributos
            }
        }
        if mensagem:
            resultado["messages"] = [{
                "contentType": "PlainText",
                "content": mensagem
            }]
        return resultado

    def pedir_matricula(mensagem):
        slots["MatriculaFuncionario"] = None
        intent["slots"] = slots
        intent["state"] = "InProgress"
        return resposta(
            {
                "type": "ElicitSlot",
                "slotToElicit": "MatriculaFuncionario"
            },
            mensagem
        )

    if not re.fullmatch(r"[0-9]{4}", matricula):
        return pedir_matricula(
            "Informe uma matricula de quatro numeros."
        )

    dominio = os.environ.get("CUSTOMER_PROFILES_DOMAIN")

    if not dominio:
        logger.error("Dominio nao configurado")
        intent["state"] = "Failed"
        return resposta(
            {"type": "Close"},
            "Sistema de validacao indisponivel."
        )

    try:
        consulta = profiles.search_profiles(
            DomainName=dominio,
            KeyName="_account",
            Values=[matricula],
            MaxResults=20
        )
    except (ClientError, BotoCoreError):
        logger.exception("Erro na consulta de perfis")
        intent["state"] = "Failed"
        return resposta(
            {"type": "Close"},
            "Nao foi possivel validar seu cadastro."
        )

    encontrados = [
        perfil for perfil in consulta.get("Items", [])
        if perfil.get("AccountNumber") == matricula
    ]

    if not encontrados:
        return pedir_matricula(
            "Matricula nao encontrada. Tente novamente."
        )

    if len(encontrados) != 1 or consulta.get("NextToken"):
        intent["state"] = "Failed"
        return resposta(
            {"type": "Close"},
            "Cadastro ambiguo. Contate o suporte."
        )

    funcionario = encontrados[0]

    nome = " ".join(filter(None, [
        funcionario.get("FirstName"),
        funcionario.get("MiddleName"),
        funcionario.get("LastName")
    ]))

    atributos["matriculaValidada"] = matricula
    atributos["nomeFuncionario"] = nome
    atributos["perfilId"] = funcionario.get("ProfileId", "")

    return resposta({"type": "Delegate"})
