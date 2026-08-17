########################################
# Lookups
########################################

data "aws_caller_identity" "current" {}

# Latest Amazon Linux 2023 arm64 AMI (t4g.nano is Graviton, so arm64 is required).
data "aws_ami" "al2023_arm64" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

########################################
# 1. Networking - hand-built VPC
########################################

resource "aws_vpc" "seed" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.name_prefix}-vpc"
  }
}

resource "aws_subnet" "public" {
  vpc_id            = aws_vpc.seed.id
  cidr_block        = "10.42.1.0/24"
  availability_zone = "${var.region}a"

  # INTENTIONAL FINDING: this subnet has no Name tag on purpose.
  # Expected finding: "untagged / unidentifiable network resource".
  # Do not add a Name tag here - it is the point of this resource.
  #
  # No public IP on launch: a public IPv4 address costs ~$3.65/mo on its own
  # and would blow the cost ceiling for this stack. Reachability is not needed
  # for any of the findings below.
  map_public_ip_on_launch = false
}

resource "aws_internet_gateway" "seed" {
  vpc_id = aws_vpc.seed.id

  tags = {
    Name = "${var.name_prefix}-igw"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.seed.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.seed.id
  }

  tags = {
    Name = "${var.name_prefix}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

########################################
# 2. Security group - deliberately open
########################################

resource "aws_security_group" "web" {
  name        = "${var.name_prefix}-web"
  description = "Seed security group with a deliberately open SSH ingress rule."
  vpc_id      = aws_vpc.seed.id

  # INTENTIONAL MISCONFIGURATION: SSH (22) open to 0.0.0.0/0.
  # Expected finding: "security group allows unrestricted SSH from the internet".
  # This is deliberate demo bait. DO NOT narrow this CIDR.
  ingress {
    description = "Deliberately unrestricted SSH - intentional finding, do not fix"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.name_prefix}-web"
  }
}

########################################
# 3. EC2 - one tiny, deliberately untagged instance
########################################

resource "aws_instance" "orphan" {
  ami                    = data.aws_ami.al2023_arm64.id
  instance_type          = "t4g.nano"
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]

  # Cost guard: an associated public IPv4 address is billed hourly. Keep false.
  associate_public_ip_address = false

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    delete_on_termination = true
  }

  # INTENTIONAL FINDING: no tags whatsoever.
  # Expected finding: "compute instance with no owner/name/cost-center tags".
  # Deliberate. Do not add a Name tag or an Owner tag to this resource.
}

########################################
# 4. S3 - two buckets, deliberately inconsistent naming
########################################

# Style A: "<prefix>-<purpose>-<env>". S3 names must be lowercase with no
# underscores, so the inconsistency is expressed through word order and a
# date suffix rather than casing.
resource "aws_s3_bucket" "assets_prod" {
  bucket = "${var.name_prefix}-assets-prod-${data.aws_caller_identity.current.account_id}"

  # INTENTIONAL FINDING: no tags at all (no Owner, no DataClass).
}

# Style B: purpose-first, hyphenated year suffix, no environment token - the
# same account, a totally different convention.
# INTENTIONAL FINDING: "inconsistent resource naming convention across buckets".
resource "aws_s3_bucket" "backups_2024" {
  bucket = "backups-${var.name_prefix}-2024-${data.aws_caller_identity.current.account_id}"

  # INTENTIONAL FINDING: no tags at all.
}

resource "aws_s3_bucket_versioning" "assets_prod" {
  bucket = aws_s3_bucket.assets_prod.id

  versioning_configuration {
    status = "Enabled"
  }
}

# INTENTIONAL FINDING: aws_s3_bucket.backups_2024 has NO versioning resource.
# A bucket literally named "backups" without versioning is the finding.
# Do not add an aws_s3_bucket_versioning block for it.

########################################
# 5. Lambda - created, never invoked
########################################

data "archive_file" "noop" {
  type        = "zip"
  output_path = "${path.module}/build/noop.zip"

  source {
    filename = "handler.py"
    content  = <<-PY
      def handler(event, context):
          return {"ok": True}
    PY
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name_prefix}-noop-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# INTENTIONAL FINDING: this function has no trigger, no event source mapping
# and is never invoked. Expected finding: "idle / abandoned Lambda function".
resource "aws_lambda_function" "noop" {
  function_name    = "${var.name_prefix}-noop"
  role             = aws_iam_role.lambda.arn
  handler          = "handler.handler"
  runtime          = "python3.13"
  memory_size      = 128
  timeout          = 3
  filename         = data.archive_file.noop.output_path
  source_code_hash = data.archive_file.noop.output_base64sha256
}

# INTENTIONAL OMISSION: no aws_cloudwatch_log_group for the Lambda above.
# Lambda will create /aws/lambda/<name> implicitly on first invoke, outside
# Terraform state. Expected finding: "log group not managed by IaC / drift
# between declared and live inventory". Do not add the log group here.

########################################
# 6. CloudWatch - log group that never expires
########################################

# INTENTIONAL MISCONFIGURATION: retention_in_days is omitted, so logs are
# retained forever. Expected finding: "log group with no retention policy".
# Do not add retention_in_days.
resource "aws_cloudwatch_log_group" "forever" {
  name = "/${var.name_prefix}/app"

  tags = {
    Name = "${var.name_prefix}-app-logs"
  }
}
