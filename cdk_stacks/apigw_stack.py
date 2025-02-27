from aws_cdk import (
    Stack,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
)
from constructs import Construct

class ApiGwStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, project_config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Load stage-level settings from the provided dictionary
        self.lambda_function_arn  = project_config["lambda_function_arn"]
        self.stage_name           = project_config["stage_name"]
        self.rest_api_name        = project_config["rest_api_name"]
        self.rest_api_description = project_config["rest_api_description"]
        self.throttling_rate_limit  = project_config["throttling_rate_limit"]
        self.throttling_burst_limit = project_config["throttling_burst_limit"]
        self.caching_enabled        = project_config["caching_enabled"]
        self.resources_methods      = project_config["resources_methods"]

        # Load API key configuration group
        api_key_config = project_config.get("api_key_config", {})
        self.enable_api_key = api_key_config.get("enable_api_key", False)
        if self.enable_api_key:
            self.api_key_id    = api_key_config.get("api_key_id")
            self.api_key_name  = api_key_config.get("api_key_name")
            self.api_key_value = api_key_config.get("api_key_value")
            usage_plan_config  = api_key_config.get("usage_plan", {})
            self.usage_plan_name        = usage_plan_config["usage_plan_name"]
            self.usage_plan_description = usage_plan_config["usage_plan_description"]
            self.plan_rate_limit        = usage_plan_config["plan_rate_limit"]
            self.plan_burst_limit       = usage_plan_config["plan_burst_limit"]
            self.quota_limit            = usage_plan_config["quota_limit"]
            self.quota_period           = usage_plan_config["quota_period"]  # e.g., "WEEK", "DAY", etc.
        else:
            self.api_key_id = None

        # Create the API Gateway REST API using stage-level settings
        api = apigw.RestApi(
            self,
            "ApiGateway",
            rest_api_name=self.rest_api_name,
            description=self.rest_api_description,
            deploy_options=apigw.StageOptions(
                stage_name=self.stage_name,
                throttling_rate_limit=self.throttling_rate_limit,
                throttling_burst_limit=self.throttling_burst_limit,
                caching_enabled=self.caching_enabled,
            )
        )

        # If API keys are enabled, create (or import) the API key and attach a usage plan.
        if self.enable_api_key:
            if self.api_key_id:
                api_key = apigw.ApiKey.from_api_key_id(self, "ExistingAPIKey", api_key_id=self.api_key_id)
            else:
                if not self.api_key_name:
                    raise ValueError("api_key_name must be provided if enable_api_key is True and no api_key_id is provided")
                api_key = apigw.ApiKey(
                    self,
                    "NewAPIKey",
                    api_key_name=self.api_key_name,
                    value=self.api_key_value if self.api_key_value else None
                )

            plan = api.add_usage_plan(
                "UsagePlan",
                name=self.usage_plan_name,
                description=self.usage_plan_description,
                api_stages=[apigw.UsagePlanPerApiStage(api=api, stage=api.deployment_stage)],
                throttle=apigw.ThrottleSettings(
                    rate_limit=self.plan_rate_limit,
                    burst_limit=self.plan_burst_limit
                ),
                quota=apigw.QuotaSettings(
                    limit=self.quota_limit,
                    period=apigw.Period[self.quota_period.upper()]
                )
            )
            plan.add_api_key(api_key)
            api_key_required_flag = True
        else:
            api_key_required_flag = False

        # Create API resources and methods from the configuration
        for resource_method in self.resources_methods:
            resource_name, method = resource_method
            self.create_api_resource(resource_name, api, method, self.lambda_function_arn, api_key_required_flag)

    def create_api_resource(self, resource_name: str, api: apigw.RestApi, method: str, lambda_function_arn: str, api_key_required: bool):
        parts = resource_name.split('/')
        api_resource = api.root
        for part in parts:
            try:
                existing_resource = api_resource.get_resource(part)
            except Exception:
                existing_resource = None
            if existing_resource:
                api_resource = existing_resource
            else:
                api_resource = api_resource.add_resource(part)
        stack_id = "LAMBDA_" + resource_name.upper().replace("/", "_")
        self.add_methods(stack_id, api_resource, method, lambda_function_arn, api_key_required)

    def add_methods(self, id: str, resource: apigw.IResource, method: str, function_arn: str, api_key_required: bool):
        # Add the primary method with Lambda integration
        resource.add_method(
            method,
            api_key_required=api_key_required,
            integration=apigw.LambdaIntegration(
                handler=_lambda.Function.from_function_arn(self, id, function_arn=function_arn),
                proxy=True
            )
        )
        # Add the OPTIONS method for CORS support
        resource.add_method(
            "OPTIONS",
            integration=apigw.MockIntegration(
                integration_responses=[
                    apigw.IntegrationResponse(
                        status_code="200",
                        response_parameters={
                            "method.response.header.Access-Control-Allow-Headers": "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'",
                            "method.response.header.Access-Control-Allow-Origin": "'*'",
                            "method.response.header.Access-Control-Allow-Methods": "'OPTIONS,GET,POST,PUT,DELETE'"
                        }
                    )
                ],
                request_templates={"application/json": '{"statusCode": 200}'}
            ),
            method_responses=[
                apigw.MethodResponse(
                    status_code="200",
                    response_parameters={
                        "method.response.header.Access-Control-Allow-Headers": True,
                        "method.response.header.Access-Control-Allow-Origin": True,
                        "method.response.header.Access-Control-Allow-Methods": True
                    }
                )
            ]
        )