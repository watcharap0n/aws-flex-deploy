import json
import os

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


REGION = os.getenv("AWS_REGION", "ap-southeast-7")
BUCKET = os.environ["BUCKET_NAME"]
PRESIGNED_URL_EXPIRY = int(os.getenv("PRESIGNED_URL_EXPIRY", "3600"))

s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": "virtual"},
    ),
)


def lambda_handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return response(200, {"message": "OK"})

    try:
        resource = event.get("resource") or event.get("rawPath") or event.get("path")
        body = parse_body(event)

        if resource == "/multipart-upload/initiate":
            return initiate_upload(body)
        if resource == "/multipart-upload/presign-part":
            return presign_part(body)
        if resource == "/multipart-upload/complete":
            return complete_upload(body)
        if resource == "/multipart-upload/abort":
            return abort_upload(body)
        if resource == "/multipart-upload/list-parts":
            return list_parts(body)

        return response(404, {"message": "Endpoint not found"})

    except (ValueError, KeyError, TypeError) as error:
        return response(400, {"message": str(error)})
    except ClientError as error:
        print(json.dumps(error.response, default=str))
        return response(500, {
            "message": "S3 request failed",
            "errorCode": error.response.get("Error", {}).get("Code"),
        })
    except Exception as error:
        print(repr(error))
        return response(500, {"message": "Internal server error"})


def initiate_upload(body):
    key = required(body, "key")

    result = s3.create_multipart_upload(
        Bucket=BUCKET,
        Key=key,
        ContentType=body.get("contentType", "application/octet-stream"),
    )

    return response(200, {
        "uploadId": result["UploadId"],
        "key": key,
        "bucket": BUCKET,
    })


def presign_part(body):
    key = required(body, "key")
    upload_id = required(body, "uploadId")
    part_number = int(required(body, "partNumber"))

    if not 1 <= part_number <= 10000:
        raise ValueError("partNumber must be between 1 and 10000")

    url = s3.generate_presigned_url(
        ClientMethod="upload_part",
        Params={
            "Bucket": BUCKET,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=PRESIGNED_URL_EXPIRY,
        HttpMethod="PUT",
    )

    return response(200, {
        "url": url,
        "partNumber": part_number,
        "expiresIn": PRESIGNED_URL_EXPIRY,
    })


def complete_upload(body):
    key = required(body, "key")
    upload_id = required(body, "uploadId")
    requested_parts = required(body, "parts")

    if not isinstance(requested_parts, list) or not requested_parts:
        raise ValueError("parts must be a non-empty array")

    parts = []
    for part in requested_parts:
        part_number = int(part.get("PartNumber", part.get("partNumber", 0)))
        etag = part.get("ETag", part.get("etag"))
        if not 1 <= part_number <= 10000 or not etag:
            raise ValueError("Each part requires a valid partNumber and ETag")
        parts.append({"PartNumber": part_number, "ETag": etag})

    parts.sort(key=lambda item: item["PartNumber"])
    if len({part["PartNumber"] for part in parts}) != len(parts):
        raise ValueError("Duplicate partNumber found")

    result = s3.complete_multipart_upload(
        Bucket=BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )

    return response(200, {
        "message": "Multipart upload completed",
        "bucket": BUCKET,
        "key": key,
        "location": result.get("Location"),
        "etag": result.get("ETag"),
        "versionId": result.get("VersionId"),
    })


def abort_upload(body):
    key = required(body, "key")
    upload_id = required(body, "uploadId")

    s3.abort_multipart_upload(
        Bucket=BUCKET,
        Key=key,
        UploadId=upload_id,
    )

    return response(200, {"message": "Multipart upload aborted"})


def list_parts(body):
    key = required(body, "key")
    upload_id = required(body, "uploadId")

    result = s3.list_parts(
        Bucket=BUCKET,
        Key=key,
        UploadId=upload_id,
    )

    return response(200, {
        "parts": [
            {
                "partNumber": part["PartNumber"],
                "etag": part["ETag"],
                "size": part["Size"],
            }
            for part in result.get("Parts", [])
        ]
    })


def parse_body(event):
    body = event.get("body", event)
    if body is None or body == "":
        return {}
    if isinstance(body, str):
        return json.loads(body)
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object")
    return body


def required(body, field):
    value = body.get(field)
    if value is None or value == "":
        raise ValueError(f"{field} is required")
    return value


def response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body),
    }
