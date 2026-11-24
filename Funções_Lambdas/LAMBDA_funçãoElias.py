import re
import unicodedata

import boto3
import cfnresponse

iam = boto3.client("iam")
account = boto3.client("account")


def slug(texto):
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    texto = re.sub(r"[^a-z0-9]+", "-", texto.lower())
    return texto.strip("-")


def montar_alias(prefixo, nome, account_id, com_id):
    base = "-".join(p for p in (slug(prefixo), slug(nome)) if p)
    if com_id or len(base) < 3:
        base = base[:50].strip("-") or "conta"
        return base + "-" + account_id
    return base[:63].strip("-")


def handler(event, context):
    tipo = event["RequestType"]
    props = event.get("ResourceProperties", {})
    physical_id = event.get("PhysicalResourceId") or context.log_stream_name
    account_id = context.invoked_function_arn.split(":")[4]
    try:
        existentes = iam.list_account_aliases().get("AccountAliases", [])

        if tipo == "Delete":
            if physical_id in existentes:
                iam.delete_account_alias(AccountAlias=physical_id)
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, physical_id)
            return

        nome = ""
        if props.get("UsarNomeDaConta") == "true":
            if not hasattr(account, "get_account_information"):
                raise RuntimeError("boto3 do runtime nao tem account.get_account_information")
            nome = account.get_account_information()["AccountName"]

        prefixo = props.get("Prefixo", "")
        forcar_id = props.get("AnexarId") == "true"
        alias = montar_alias(prefixo, nome, account_id, forcar_id)
        alias_com_id = montar_alias(prefixo, nome, account_id, True)

        # se a conta ja tem um dos dois, mantem
        atual = next((a for a in (alias, alias_com_id) if a in existentes), None)
        if atual:
            alias = atual
        else:
            # a conta so pode ter 1 alias: remove o atual (se houver) antes de criar
            for velho in existentes:
                iam.delete_account_alias(AccountAlias=velho)
            try:
                iam.create_account_alias(AccountAlias=alias)
            except iam.exceptions.EntityAlreadyExistsException:
                # alias ja usado por outra conta no mundo: tenta com o ID da conta
                alias = alias_com_id
                iam.create_account_alias(AccountAlias=alias)

        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Alias": alias}, alias)
    except Exception as e:
        print("Erro:", repr(e))
        # no Delete nunca falha, para nao travar a remocao da stack
        status = cfnresponse.SUCCESS if tipo == "Delete" else cfnresponse.FAILED
        cfnresponse.send(event, context, status, {"Error": str(e)}, physical_id)
