"""Isolated staging multipart upload API, certificate, DNS and bucket CORS."""
from pathlib import Path

from aws_cdk import (
    CfnOutput, CustomResource, Duration, RemovalPolicy, Stack,
    aws_apigateway as apigw, aws_certificatemanager as acm,
    aws_iam as iam, aws_lambda as lambda_, aws_logs as logs,
    aws_route53 as route53, aws_route53_targets as targets,
)
from constructs import Construct


class StagingIntakeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, project_config: dict, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        c = project_config
        if c['bucket_name'] != 'cdp-stg-538783577018-ap-southeast-7':
            raise ValueError('Staging intake must use the staging bucket')
        if c['domain_name'] != 'cdp-intake-dev.buzzsprint.dev':
            raise ValueError('Staging intake must use its own domain')
        if self.account != '538783577018' or self.region != 'ap-southeast-7':
            raise ValueError('Staging intake account/region mismatch')

        bucket_arn = f"arn:aws:s3:::{c['bucket_name']}"
        zone = route53.HostedZone.from_hosted_zone_attributes(
            self, 'Zone', hosted_zone_id=c['hosted_zone_id'], zone_name=c['zone_name'])
        cert = acm.Certificate(self, 'Certificate', domain_name=c['domain_name'],
            validation=acm.CertificateValidation.from_dns(zone))
        fn_name = c['function_name']
        log = logs.LogGroup(self, 'UploadLogs', log_group_name=f'/aws/lambda/{fn_name}',
            retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.RETAIN)
        handler = lambda_.Function(self, 'UploadHandler', function_name=fn_name,
            runtime=lambda_.Runtime.PYTHON_3_11, handler='lambda_function.lambda_handler',
            code=lambda_.Code.from_asset(str(Path(__file__).resolve().parents[1] /
                '_lambda/staging_multipart_upload'), exclude=['**/__pycache__/**', '**/*.pyc']),
            memory_size=128, timeout=Duration.seconds(30),
            environment={'BUCKET_NAME':c['bucket_name'], 'PRESIGNED_URL_EXPIRY':'3600'})
        handler.node.add_dependency(log)
        handler.add_to_role_policy(iam.PolicyStatement(
            actions=['s3:PutObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
            resources=[bucket_arn+'/*']))

        api = apigw.RestApi(self, 'Api', rest_api_name=c['api_name'],
            description='Staging multipart upload into the staging CDP landing bucket',
            endpoint_types=[apigw.EndpointType.REGIONAL],
            deploy_options=apigw.StageOptions(stage_name='staging',
                throttling_rate_limit=10, throttling_burst_limit=20, caching_enabled=False),
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS, allow_methods=['OPTIONS','POST'],
                allow_headers=['Content-Type','Authorization','X-Amz-Date','X-Api-Key','X-Amz-Security-Token']))
        multipart = api.root.add_resource('multipart-upload')
        for action in ['initiate','presign-part','list-parts','complete','abort']:
            multipart.add_resource(action).add_method('POST',apigw.LambdaIntegration(handler))
        domain = apigw.DomainName(self, 'Domain', domain_name=c['domain_name'],
            certificate=cert, endpoint_type=apigw.EndpointType.REGIONAL,
            security_policy=apigw.SecurityPolicy.TLS_1_2)
        apigw.BasePathMapping(self, 'Mapping', domain_name=domain, rest_api=api,
            stage=api.deployment_stage)
        route53.ARecord(self, 'Alias', zone=zone, record_name=c['domain_name'],
            target=route53.RecordTarget.from_alias(targets.ApiGatewayDomain(domain)))

        # The bucket predates this stack. Modify only CORS; retain its objects,
        # notifications, policies and all downstream processing configuration.
        cors_handler = lambda_.Function(self, 'CorsHandler', runtime=lambda_.Runtime.PYTHON_3_11,
            handler='index.handler', memory_size=128, timeout=Duration.seconds(30),
            code=lambda_.Code.from_inline('''import boto3
import cfnresponse

def handler(event, context):
    bucket = event['ResourceProperties']['BucketName']
    try:
        s3 = boto3.client('s3')
        if event['RequestType'] == 'Delete':
            s3.delete_bucket_cors(Bucket=bucket)
        else:
            rules = event['ResourceProperties']['CorsRules']
            for rule in rules:
                rule['MaxAgeSeconds'] = int(rule['MaxAgeSeconds'])
            s3.put_bucket_cors(Bucket=bucket, CORSConfiguration={
                'CORSRules': rules})
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {}, bucket + '-intake-cors')
    except Exception as error:
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, bucket + '-intake-cors',
                         reason=str(error))
'''))
        cors_handler.add_to_role_policy(iam.PolicyStatement(
            actions=['s3:PutBucketCORS'], resources=[bucket_arn]))
        cors = CustomResource(self, 'BucketCors', service_token=cors_handler.function_arn,
            properties={'BucketName':c['bucket_name'], 'CorsRules':[{
                'AllowedHeaders':['*'], 'AllowedMethods':['PUT','GET','HEAD'],
                'AllowedOrigins':['*'], 'ExposeHeaders':['ETag'], 'MaxAgeSeconds':3600}]})
        cors.node.add_dependency(cors_handler.role)

        CfnOutput(self,'BaseUrl',value='https://'+c['domain_name'])
        CfnOutput(self,'ApiId',value=api.rest_api_id)
        CfnOutput(self,'ExecuteApiUrl',value=api.url)
        CfnOutput(self,'CertificateArn',value=cert.certificate_arn)
        CfnOutput(self,'UploadFunctionName',value=handler.function_name)
        CfnOutput(self,'BucketName',value=c['bucket_name'])
