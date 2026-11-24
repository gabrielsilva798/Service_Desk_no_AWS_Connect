import os
import re
import time

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

cases = boto3.client(
    "connectcases",
    region_name="us-east-1",
    config=Config(
        connect_timeout=1,
        read_timeout=2,
        retries={"max_attempts": 1}
    )
)

def lambda_handler(event, context):
    params = event.get("Details", {}).get("Parameters", {})
    case_id = params.get("caseId") or event.get("caseId")
    domain_id = os.environ.get("CASES_DOMAIN_ID", "")

    if not case_id or not re.fullmatch(
        r"[0-9a-fA-F-]{36}", str(case_id)
    ):
        return {"status": "ERROR", "referenceNumber": ""}

    if not domain_id:
        return {"status": "CONFIG_ERROR", "referenceNumber": ""}

    for attempt in range(3):
        try:
            response = cases.get_case(
                domainId=domain_id,
                caseId=case_id,
                fields=[{"id": "reference_number"}]
            )

            for field in response.get("fields", []):
                if field.get("id") == "reference_number":
                    number = field.get("value", {}).get(
                        "stringValue", ""
                    )
                    if number:
                        return {
                            "status": "SUCCESS",
                            "referenceNumber": str(number)
                        }

            return {"status": "NOT_FOUND", "referenceNumber": ""}

        except cases.exceptions.ResourceNotFoundException:
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
                continue
            return {"status": "NOT_FOUND", "referenceNumber": ""}

        except ClientError as error:
            print("GetCase error:", error.response[
                "Error"
            ].get("Code", "Unknown"))
            return {"status": "ERROR", "referenceNumber": ""}

    return {"status": "NOT_FOUND", "referenceNumber": ""}
