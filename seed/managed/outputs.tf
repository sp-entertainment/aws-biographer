output "vpc_id" {
  description = "ID of the seeded VPC."
  value       = aws_vpc.seed.id
}

output "vpc_arn" {
  description = "ARN of the seeded VPC."
  value       = aws_vpc.seed.arn
}

output "subnet_id" {
  description = "ID of the (deliberately unnamed) public subnet."
  value       = aws_subnet.public.id
}

output "internet_gateway_id" {
  description = "ID of the internet gateway."
  value       = aws_internet_gateway.seed.id
}

output "route_table_id" {
  description = "ID of the public route table."
  value       = aws_route_table.public.id
}

output "security_group_id" {
  description = "ID of the deliberately open security group."
  value       = aws_security_group.web.id
}

output "security_group_arn" {
  description = "ARN of the deliberately open security group."
  value       = aws_security_group.web.arn
}

output "instance_id" {
  description = "ID of the deliberately untagged t4g.nano instance."
  value       = aws_instance.orphan.id
}

output "instance_arn" {
  description = "ARN of the deliberately untagged t4g.nano instance."
  value       = aws_instance.orphan.arn
}

output "assets_bucket_id" {
  description = "Name of the versioned assets bucket."
  value       = aws_s3_bucket.assets_prod.id
}

output "assets_bucket_arn" {
  description = "ARN of the versioned assets bucket."
  value       = aws_s3_bucket.assets_prod.arn
}

output "backups_bucket_id" {
  description = "Name of the unversioned backups bucket."
  value       = aws_s3_bucket.backups_2024.id
}

output "backups_bucket_arn" {
  description = "ARN of the unversioned backups bucket."
  value       = aws_s3_bucket.backups_2024.arn
}

output "lambda_function_name" {
  description = "Name of the never-invoked Lambda function."
  value       = aws_lambda_function.noop.function_name
}

output "lambda_function_arn" {
  description = "ARN of the never-invoked Lambda function."
  value       = aws_lambda_function.noop.arn
}

output "lambda_role_name" {
  description = "Name of the Lambda execution role."
  value       = aws_iam_role.lambda.name
}

output "lambda_role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.lambda.arn
}

output "log_group_name" {
  description = "Name of the log group with no retention policy."
  value       = aws_cloudwatch_log_group.forever.name
}

output "log_group_arn" {
  description = "ARN of the log group with no retention policy."
  value       = aws_cloudwatch_log_group.forever.arn
}
