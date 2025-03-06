from aws_cdk import (
    Stack,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
)
from constructs import Construct

class ApiGwStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, project_config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Stage-level settings
        self.stage_name           = project_config["stage_name"]
        self.rest_api_name        = project_config["rest_api_name"]
        self.rest_api_description = project_config["rest_api_description"]
        self.throttling_rate_limit  = project_config["throttling_rate_limit"]
        self.throttling_burst_limit = project_config["throttling_burst_limit"]
        self.caching_enabled        = project_config["caching_enabled"]
        self.resources_methods      = project_config["resources_methods"]

        # API Key configuration group
        api_key_config = project_config.get("api_key_config", {})
        self.enable_api_key = api_key_config.get("enable_api_key", False)
        if self.enable_api_key:
            self.api_key_id    = api_key_config.get("api_key_id")
            self.api_key_name  = api_key_config.get("api_key_name")
            self.api_key_value = api_key_config.get("api_key_value")
            usage_plan_config = api_key_config.get("usage_plan", {})
            self.usage_plan_name        = usage_plan_config["usage_plan_name"]
            self.usage_plan_description = usage_plan_config["usage_plan_description"]
            self.plan_rate_limit        = usage_plan_config["plan_rate_limit"]
            self.plan_burst_limit       = usage_plan_config["plan_burst_limit"]
            self.quota_limit            = usage_plan_config["quota_limit"]
            self.quota_period           = usage_plan_config["quota_period"]
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

        # Create API resources and methods based on configuration.
        # Each entry in resources_methods can be defined in one of two ways:
        # 1. List format: [ "path", "HTTP_METHOD" ] => default Lambda integration.
        # 2. Dictionary format for custom integration:
        #    {
        #       path: "v1/custom",
        #       method: "HTTP_METHOD",
        #       integration_type: "http_proxy",  # or "lambda"
        #       http_proxy_url: "http://example.com/api"  # required if integration_type is http_proxy
        #    }
        for resource_method in self.resources_methods:
            if isinstance(resource_method, dict):
                path = resource_method["path"]
                method = resource_method["method"]
                integration_type = resource_method.get("integration_type", "lambda")
                if integration_type == "http_proxy":
                    # For HTTP proxy, the configuration must include the URL.
                    http_proxy_url = resource_method["http_proxy_url"]
                    self.create_proxy_api_resource(path, api, method, http_proxy_url, api_key_required_flag)
                else:
                    lambda_function_arn = resource_method.get("lambda_function_arn", project_config.get("lambda_function_arn"))
                    self.create_api_resource(path, api, method, "lambda", lambda_function_arn, api_key_required_flag)
            else:
                # List format for default Lambda integration.
                path, method = resource_method
                lambda_function_arn = project_config.get("lambda_function_arn")
                self.create_api_resource(path, api, method, "lambda", lambda_function_arn, api_key_required_flag)

    def create_api_resource(self, resource_name: str, api: apigw.RestApi, method: str, integration_type: str, integration_value: str, api_key_required: bool):
        # Create resource from fixed path parts.
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
        self.add_methods(stack_id, api_resource, method, integration_type, integration_value, api_key_required)

    def create_proxy_api_resource(self, resource_name: str, api: apigw.RestApi, method: str, http_proxy_url: str,
                                  api_key_required: bool):
        """
        If resource_name already includes {proxy+}, we create it as-is.
        Otherwise, we create a child resource with {proxy+}.
        """
        parts = resource_name.split('/')
        api_resource = api.root

        for part in parts:
            # Build up the path
            existing_resource = api_resource.get_resource(part)
            if existing_resource:
                api_resource = existing_resource
            else:
                api_resource = api_resource.add_resource(part)

        # Check if user’s YAML path already ends with {proxy+}
        if parts[-1] == "{proxy+}":
            # The user already gave us a path with {proxy+}, so just integrate here
            stack_id = f"HTTP_PROXY_{resource_name.upper().replace('/', '_')}"
            self.add_methods(stack_id, api_resource, method, "http_proxy", http_proxy_url, api_key_required)
        else:
            # We add {proxy+} child ourselves
            proxy_resource = api_resource.add_resource("{proxy+}")
            stack_id = f"HTTP_PROXY_{resource_name.upper().replace('/', '_')}_CHILD"
            self.add_methods(stack_id, proxy_resource, method, "http_proxy", http_proxy_url, api_key_required)

    def add_methods(
            self,
            id: str,
            resource: apigw.IResource,
            method: str,
            integration_type: str,
            integration_value: str,
            api_key_required: bool
    ):
        if integration_type == "http_proxy":
            integration = apigw.HttpIntegration(
                integration_value,  # e.g. "https://tipgcore-dev.example.com/collections/{proxy}"
                http_method=method,
                proxy=True,
                options=apigw.IntegrationOptions(
                    # 1) Map the path param from the method request to the integration request
                    request_parameters={
                        "integration.request.path.proxy": "method.request.path.proxy"
                    },
                    integration_responses=[
                        apigw.IntegrationResponse(
                            status_code="200",
                            response_parameters={
                                "method.response.header.Access-Control-Allow-Headers":
                                    "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'",
                                "method.response.header.Access-Control-Allow-Origin": "'*'",
                                "method.response.header.Access-Control-Allow-Methods":
                                    "'OPTIONS,GET,POST,PUT,DELETE'"
                            }
                        )
                    ]
                )
            )
        else:
            # Lambda integration example (unchanged)
            integration = apigw.LambdaIntegration(
                handler=_lambda.Function.from_function_arn(self, id, function_arn=integration_value),
                proxy=True
            )

        # 2) In the primary method, declare that 'method.request.path.proxy' is required
        resource.add_method(
            method,
            api_key_required=api_key_required,
            integration=integration,
            request_parameters={
                # 'True' means it's a required path parameter
                "method.request.path.proxy": True
            },
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

        # OPTIONS method for CORS (unchanged)
        resource.add_method(
            "OPTIONS",
            integration=apigw.MockIntegration(
                integration_responses=[
                    apigw.IntegrationResponse(
                        status_code="200",
                        response_parameters={
                            "method.response.header.Access-Control-Allow-Headers":
                                "'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token'",
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