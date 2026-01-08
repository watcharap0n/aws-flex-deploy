from aws_cdk import (
    Stack,
    aws_glue as glue,
    aws_iam as iam,
    aws_s3_assets as s3_assets,
)
from constructs import Construct


class GlueJobStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, project_config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        job_name = project_config.get("job_name", "DynamicGlueJob")
        command_config = project_config.get("command", {})
        command_name = command_config.get("name", "glueetl")
        script_location = command_config.get("script_location")
        local_script_path = command_config.get("local_script_path")
        python_version = command_config.get("python_version")

        role_arn = project_config.get("role_arn")
        if role_arn:
            role = iam.Role.from_role_arn(self, "GlueJobRole", role_arn)
        else:
            role = self._create_role(project_config.get("role", {}), job_name)

        if not script_location:
            if not local_script_path:
                raise ValueError("command.script_location or command.local_script_path is required for the Glue job")
            script_asset = s3_assets.Asset(
                self,
                "GlueJobScriptAsset",
                path=local_script_path
            )
            script_asset.grant_read(role)
            script_location = script_asset.s3_object_url

        connections_config = project_config.get("connections", {})
        connection_names = []
        for conn_conf in connections_config.get("create", []):
            conn_name = conn_conf.get("name")
            conn_type = conn_conf.get("connection_type")
            conn_props = conn_conf.get("connection_properties", {})
            physical = conn_conf.get("physical_connection_requirements", {})
            if not conn_name or not conn_type:
                raise ValueError("Each connection must include name and connection_type")

            glue.CfnConnection(
                self,
                f"GlueConnection{conn_name}",
                catalog_id=self.account,
                connection_input=glue.CfnConnection.ConnectionInputProperty(
                    name=conn_name,
                    connection_type=conn_type,
                    connection_properties=conn_props,
                    physical_connection_requirements=glue.CfnConnection.PhysicalConnectionRequirementsProperty(
                        subnet_id=physical.get("subnet_id"),
                        security_group_id_list=physical.get("security_group_id_list"),
                        availability_zone=physical.get("availability_zone"),
                    ) if physical else None,
                    description=conn_conf.get("description"),
                    match_criteria=conn_conf.get("match_criteria"),
                )
            )
            connection_names.append(conn_name)

        connection_names.extend(connections_config.get("use", []))
        connection_names = [name for name in connection_names if name]

        default_arguments = dict(project_config.get("default_arguments", {}))
        additional_python_modules = project_config.get("additional_python_modules", [])
        extra_py_files = project_config.get("extra_py_files", [])
        extra_jars = project_config.get("extra_jars", [])
        extra_files = project_config.get("extra_files", [])

        if additional_python_modules and "--additional-python-modules" not in default_arguments:
            default_arguments["--additional-python-modules"] = ",".join(additional_python_modules)
        if extra_py_files and "--extra-py-files" not in default_arguments:
            default_arguments["--extra-py-files"] = ",".join(extra_py_files)
        if extra_jars and "--extra-jars" not in default_arguments:
            default_arguments["--extra-jars"] = ",".join(extra_jars)
        if extra_files and "--extra-files" not in default_arguments:
            default_arguments["--extra-files"] = ",".join(extra_files)

        glue.CfnJob(
            self,
            "GlueJob",
            name=job_name,
            role=role.role_arn,
            command=glue.CfnJob.JobCommandProperty(
                name=command_name,
                script_location=script_location,
                python_version=python_version,
            ),
            default_arguments=default_arguments if default_arguments else None,
            connections=glue.CfnJob.ConnectionsListProperty(
                connections=connection_names
            ) if connection_names else None,
            max_retries=project_config.get("max_retries"),
            timeout=project_config.get("timeout"),
            glue_version=project_config.get("glue_version"),
            worker_type=project_config.get("worker_type"),
            number_of_workers=project_config.get("number_of_workers"),
            execution_class=project_config.get("execution_class"),
            execution_property=glue.CfnJob.ExecutionPropertyProperty(
                max_concurrent_runs=project_config.get("max_concurrent_runs")
            ) if project_config.get("max_concurrent_runs") is not None else None,
            description=project_config.get("description"),
            tags=project_config.get("tags"),
        )

    def _create_role(self, role_config: dict, job_name: str) -> iam.Role:
        assumed_by_value = role_config.get("assumed_by", "glue.amazonaws.com")
        assumed_by = iam.ServicePrincipal(assumed_by_value)
        role_name = role_config.get("role_name", f"GlueJobRole{job_name}")
        ids = role_config.get("ids", ["PolicyBasic"])
        managed_policy_arns = role_config.get(
            "managed_policy_arns",
            ["arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"]
        )
        role = iam.Role(
            self,
            "GLUE_JOB_ROLE",
            assumed_by=assumed_by,
            role_name=role_name
        )

        if len(ids) != len(managed_policy_arns):
            raise ValueError("ids and managed_policy_arns must have the same length")
        for id_val, managed_policy_arn in zip(ids, managed_policy_arns):
            role.add_managed_policy(
                iam.ManagedPolicy.from_managed_policy_arn(
                    self,
                    id_val,
                    managed_policy_arn
                )
            )

        inline_policies = role_config.get("inline_policies")
        if inline_policies:
            for policy_name, policy_statements in inline_policies.items():
                policy = iam.Policy(self, policy_name)
                for statement in policy_statements:
                    actions = statement.get("actions", [])
                    resources = statement.get("resources", ["*"])
                    effect = statement.get("effect", iam.Effect.ALLOW)
                    policy.add_statements(iam.PolicyStatement(
                        actions=actions,
                        resources=resources,
                        effect=effect
                    ))
                role.attach_inline_policy(policy)

        return role
