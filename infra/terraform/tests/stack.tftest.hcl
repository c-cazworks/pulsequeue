# Uses a mocked provider: needs no AWS credentials and creates nothing.
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-2a", "us-east-2b", "us-east-2c"] }
  }

  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }

  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }

  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/oidc.example.com/id/ABC" }
  }

  mock_resource "aws_launch_template" {
    defaults = { id = "lt-0123456789abcdef0", latest_version = 1 }
  }

  mock_resource "aws_db_instance" {
    defaults = {
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-2:123456789012:secret:mock" }]
    }
  }

  mock_resource "aws_eks_cluster" {
    defaults = {
      identity = [{ oidc = [{ issuer = "https://oidc.example.com/id/ABC" }] }]
    }
  }
}

variables {
  api_allowed_cidrs = ["203.0.113.4/32"]
}

run "stack" {
  assert {
    condition     = length(module.vpc.private_subnet_ids) == 2
    error_message = "Expected private subnets in two AZs."
  }

  assert {
    condition     = toset(keys(module.ecr.repository_urls)) == toset(["pulsequeue/api", "pulsequeue/web"])
    error_message = "Expected one ECR repository per image."
  }

  assert {
    condition     = output.oidc_issuer == "oidc.example.com/id/ABC"
    error_message = "IRSA conditions need the issuer without https://."
  }
}

run "rds_is_private_and_disposable" {
  module {
    source = "./modules/rds"
  }

  variables {
    name                     = "test"
    vpc_id                   = "vpc-1"
    subnet_ids               = ["subnet-1", "subnet-2"]
    client_security_group_id = "sg-nodes"
  }

  assert {
    condition     = !aws_db_instance.this.publicly_accessible && aws_vpc_security_group_ingress_rule.postgres.referenced_security_group_id == "sg-nodes"
    error_message = "Only the cluster may reach Postgres."
  }

  assert {
    condition     = aws_db_instance.this.skip_final_snapshot && aws_db_instance.this.delete_automated_backups && !aws_db_instance.this.deletion_protection
    error_message = "Destroy must succeed and leave no backups."
  }
}

run "rejects_open_api_endpoint" {
  command = plan

  variables {
    api_allowed_cidrs = ["0.0.0.0/0"]
  }

  expect_failures = [var.api_allowed_cidrs]
}

run "rejects_empty_api_allowlist" {
  command = plan

  variables {
    api_allowed_cidrs = []
  }

  expect_failures = [var.api_allowed_cidrs]
}

run "rejects_instance_type_outside_free_plan" {
  command = plan

  variables {
    node_instance_types = ["t3.medium"]
  }

  expect_failures = [var.node_instance_types]
}

run "irsa_trusts_one_service_account" {
  module {
    source = "./modules/irsa"
  }

  variables {
    role_name         = "test"
    oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.example.com/id/ABC"
    oidc_issuer       = "oidc.example.com/id/ABC"
    namespace         = "ns"
    service_account   = "sa"
    policy_arns       = {}
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Condition.StringEquals["oidc.example.com/id/ABC:sub"] == "system:serviceaccount:ns:sa" &&
      jsondecode(aws_iam_role.this.assume_role_policy).Statement[0].Condition.StringEquals["oidc.example.com/id/ABC:aud"] == "sts.amazonaws.com"
    )
    error_message = "Trust policy must pin both the service account and the audience."
  }
}
