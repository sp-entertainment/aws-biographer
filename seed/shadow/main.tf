# ---------------------------------------------------------------------------
# seed/shadow -- "unmanaged-looking" half of the contest demo account.
#
# Everything here is Terraform-created (so `terraform destroy` cleans it up),
# but this stack keeps its OWN state file which the drift analyzer never reads.
# That is the entire point: these resources are live in the account and absent
# from the state the analyzer inspects, so they show up as untracked. See
# README.md before "fixing" anything in this file.
# ---------------------------------------------------------------------------

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_vpc" "default" {
  default = true
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  az         = data.aws_availability_zones.available.names[0]

  # INTENTIONAL: three deliberately inconsistent bucket naming conventions --
  # a date suffix, an environment word, and a project codename. Produces the
  # "inconsistent S3 naming / no tagging standard" inventory finding. Do not
  # normalize these names; the inconsistency IS the fixture.
  buckets = {
    dated    = "${var.name_prefix}-logs-2019-11-04-${local.account_id}"
    env_word = "${var.name_prefix}-staging-assets-${local.account_id}"
    codename = "${var.name_prefix}-bluefinch-${local.account_id}"
  }
}

# ---------------------------------------------------------------------------
# 1. Unattached Elastic IP
# ---------------------------------------------------------------------------

# INTENTIONAL WASTE: this EIP is allocated and associated with nothing. An
# idle public IPv4 address bills $0.005/hr (~$3.60/month) and is the flagship
# "unattached Elastic IP" cost finding. Do NOT attach it to anything.
resource "aws_eip" "orphaned" {
  domain = "vpc"

  tags = {
    Name = "${var.name_prefix}-orphaned-eip"
  }
}

# ---------------------------------------------------------------------------
# 2. Unattached EBS volumes
# ---------------------------------------------------------------------------

# INTENTIONAL WASTE: 1 GiB gp3 volume attached to no instance. Produces the
# "unattached EBS volume" finding. This one has a Name tag.
resource "aws_ebs_volume" "orphaned_tagged" {
  availability_zone = local.az
  size              = 1
  type              = "gp3"

  tags = {
    Name = "${var.name_prefix}-orphaned-vol-tagged"
  }
}

# INTENTIONAL WASTE + INTENTIONAL MISSING TAGS: same unattached-volume finding,
# but with no tags at all, so the
# inventory report shows both the tagged and untagged shapes of the same waste.
resource "aws_ebs_volume" "orphaned_untagged" {
  availability_zone = local.az
  size              = 1
  type              = "gp3"
}

# ---------------------------------------------------------------------------
# 3. Sloppily named S3 buckets
# ---------------------------------------------------------------------------

# INTENTIONAL: names are inconsistent on purpose (see locals.buckets). No tags
# at all -- feeds the "untagged resource" finding.
resource "aws_s3_bucket" "sloppy" {
  for_each = local.buckets

  bucket = each.value
}

# NOT a deliberate misconfiguration: these buckets stay private. The demo needs
# naming/tagging drift, not an actually-public bucket.
resource "aws_s3_bucket_public_access_block" "sloppy" {
  for_each = aws_s3_bucket.sloppy

  bucket = each.value.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------------------
# 4. Over-permissive security group
# ---------------------------------------------------------------------------

# INTENTIONAL MISCONFIGURATION: RDP (3389) open to 0.0.0.0/0 in the default
# VPC. Produces the "security group open to the world on an admin port"
# finding. Nothing is attached to this SG, so nothing is actually reachable --
# do not narrow the CIDR, the finding is the point.
resource "aws_security_group" "rdp_open_to_world" {
  name        = "${var.name_prefix}-rdp-open"
  description = "Seed fixture: deliberately over-permissive RDP ingress"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "INTENTIONAL FINDING: RDP from anywhere"
    from_port   = 3389
    to_port     = 3389
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.name_prefix}-rdp-open"
  }
}

# ---------------------------------------------------------------------------
# 5. Lambda function that is never invoked
# ---------------------------------------------------------------------------

data "archive_file" "never_invoked" {
  type        = "zip"
  output_path = "${path.module}/lambda.zip"

  source {
    filename = "index.py"
    content  = <<-PY
      def handler(event, context):
          return {"ok": True}
    PY
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "never_invoked" {
  name               = "${var.name_prefix}-never-invoked-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "never_invoked_basic" {
  role       = aws_iam_role.never_invoked.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# INTENTIONAL: no trigger, no event source, no schedule. Produces the "idle /
# never-invoked Lambda" finding. Free at rest -- billing is per invocation.
resource "aws_lambda_function" "never_invoked" {
  function_name    = "${var.name_prefix}-never-invoked"
  role             = aws_iam_role.never_invoked.arn
  handler          = "index.handler"
  runtime          = "python3.13"
  memory_size      = 128
  timeout          = 3
  filename         = data.archive_file.never_invoked.output_path
  source_code_hash = data.archive_file.never_invoked.output_base64sha256
}

# ---------------------------------------------------------------------------
# 6. Orphaned messaging infrastructure
# ---------------------------------------------------------------------------

# INTENTIONAL: queue with no producer, no consumer, no subscription. Produces
# the "orphaned SQS queue" finding. Free while empty and idle.
resource "aws_sqs_queue" "unwired" {
  name = "${var.name_prefix}-unwired-queue"
}

# INTENTIONAL: topic with zero subscriptions and no publisher. Produces the
# "orphaned SNS topic" finding. Deliberately NOT subscribed to the queue above.
resource "aws_sns_topic" "unwired" {
  name = "${var.name_prefix}-unwired-topic"
}

# ---------------------------------------------------------------------------
# 7. Empty DynamoDB table
# ---------------------------------------------------------------------------

# INTENTIONAL: on-demand table with no items and no readers. Produces the
# "unused DynamoDB table" finding. PAY_PER_REQUEST is mandatory here -- a
# provisioned table would carry an hourly floor and blow the cost budget.
# No tags at all, on purpose.
resource "aws_dynamodb_table" "unused" {
  name         = "${var.name_prefix}-session-cache"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
}
